"""Browser control panel: live colour, song timeline and log, plus switches
for every rule in light_controller.RULES, per-light selection and numeric
settings for the rules."""

import asyncio
import json
import os
from collections import deque

from aiohttp import WSMsgType, web
from loguru import logger

import atmospheric
import light_controller
import sections
from light_controller import ANALYSIS_RULES, DROP_HITS, DROP_LEADS, RULES, LightsController
from spotify_listener import SpotifyChangesListener

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
LIGHT_KINDS = {"LightDevice": "Yeelight", "ESPHomeDevice": "ESPHome", "TasmotaDevice": "Tasmota"}
# Log lines a freshly opened page gets, so it is not blank until the next one.
LOG_BACKLOG = 300
# State updates per second; the page interpolates the song clock between them.
STATE_INTERVAL_S = 0.05

# Numeric rule settings the page can tune: (group, module, constant, label,
# unit, min, max, step, when it applies). The constants are module globals
# read where they are used, so setting them on the module changes the rule.
# "song" settings only feed the analysis run when a track loads; the page can
# re-run it for the current track.
SETTINGS = [
    ("Silence", light_controller, "SILENCE_TRACK_DB", "Under the loud bars", "dB", 6, 48, 1, "song"),
    ("Silence", light_controller, "SILENCE_LOCAL_DB", "Under the bars before", "dB", 3, 40, 1, "song"),
    ("Silence", light_controller, "SILENCE_LOOKBACK_BARS", "Bars looked back", "bars", 1, 16, 1, "song"),
    ("Silence", light_controller, "SILENCE_MIN_S", "Shortest gap", "s", 0.05, 2, 0.05, "song"),
    ("Silence", light_controller, "SILENCE_DIM_BRIGHTNESS", "Dim level", "%", 0, 30, 0.5, "now"),
    ("Silence", light_controller, "SILENCE_FADE_S", "Fade to dim", "s", 0, 1, 0.01, "now"),
    ("Silence", light_controller, "SILENCE_DARK_MIN_S", "Go dark after", "s", 0.1, 5, 0.1, "now"),
    ("Silence", light_controller, "SILENCE_DARK_FADE_S", "Fade to dark", "s", 0, 2, 0.05, "now"),
    ("Quiet bars", light_controller, "QUIET_BAR_DB", "Under the bars before", "dB", 1, 20, 0.5, "song"),
    ("Quiet bars", light_controller, "QUIET_DARK_DB", "Deeper than that is dark", "dB", 1, 40, 0.5, "song"),
    ("Quiet bars", light_controller, "QUIET_LOOKBACK_BARS", "Bars looked back", "bars", 1, 32, 1, "song"),
    ("Flow", light_controller, "FLOW_MAX_ONSET_DB", "Onset rise under", "dB", 1, 20, 0.5, "song"),
    ("Flow", light_controller, "FLOW_MAX_BEAT_CONFIDENCE", "Beat confidence under", "", 0, 1, 0.05, "song"),
    ("Flow", light_controller, "FLOW_QUIET_DB", "Under the loud bars", "dB", 0, 30, 0.5, "song"),
    ("Character", light_controller, "CALM_DRIVE", "Calm style under drive", "", 0, 1, 0.05, "song"),
    ("Character", light_controller, "CALM_ENERGY_SCALE", "Calm energy scale", "×", 0.2, 1, 0.05, "song"),
    ("Bars", sections, "BACKBEAT_MIN_T", "Backbeat decides when t is over", "", 2, 20, 0.5, "song"),
    ("Bars", sections, "DOWNBEAT_MIN_SHARE", "Move beat 1 when a beat gets", "share", 0.25, 0.8, 0.01, "song"),
    ("Parts", sections, "BOUNDARY_PENALTY", "Cost of a boundary", "", 2, 30, 0.5, "song"),
    ("Parts", sections, "SNAP_BARS", "Snap to the change within", "bars", 0, 3, 1, "song"),
    ("Parts", light_controller, "PHRASE_GRID_MARGIN", "Count phrases from the end when", "×", 1, 3, 0.05, "song"),
    ("Drops", light_controller, "BREAK_DB", "Break: under the bars on both sides", "dB", 2, 20, 0.5, "song"),
    ("Drops", light_controller, "BREAK_RETURN_DB", "Break: music back within", "dB", 0, 10, 0.5, "song"),
    ("Drops", sections, "DROP_MAX_RATIO", "Peak distance shrinks to", "×", 0.1, 1, 0.05, "song"),
    ("Drops", sections, "DROP_MIN_FALL", "Peak distance falls by", "", 0, 5, 0.05, "song"),
    ("Drops", sections, "DROP_MAX_ARRIVAL", "Lands within", "", 0.5, 10, 0.1, "song"),
    ("Calm brightness", atmospheric, "CALM_TOP_LEVEL", "Calm band top level", "", 0.2, 1, 0.01, "now"),
    ("Calm brightness", atmospheric, "CALM_BOTTOM_SHARE", "Calm band bottom share", "", 0, 1, 0.01, "now"),
]
SETTING_MODULES = {name: module for _, module, name, *_ in SETTINGS}
# Captured at import, before anything is changed, for the Reset button.
SETTING_DEFAULTS = {name: getattr(module, name) for _, module, name, *_ in SETTINGS}


class WebApp:
    def __init__(self, controller: LightsController, listener: SpotifyChangesListener):
        self.controller = controller
        self.listener = listener
        self.log_lines: deque[str] = deque(maxlen=LOG_BACKLOG)
        self.sockets: set[web.WebSocketResponse] = set()
        self.loop = asyncio.get_event_loop()
        self.timeline_payload = ""
        self.timeline_sent_for = 0               # controller.timeline_id the payload was built from
        logger.add(self._log_sink, level="DEBUG", format="{time:HH:mm:ss.SSS} | {level:1.1} | {message}")

    def _log_sink(self, message):
        # Loguru calls sinks on the logging thread; sockets belong to the loop.
        self.loop.call_soon_threadsafe(self._publish_log, str(message).rstrip("\n"))

    def _publish_log(self, line: str):
        self.log_lines.append(line)
        payload = json.dumps({"type": "log", "line": line})
        for socket in list(self.sockets):
            self.loop.create_task(socket.send_str(payload))

    def _lights(self) -> list[dict]:
        return [
            {
                "ip": device.ip,
                "name": device.model,
                "kind": LIGHT_KINDS[type(device).__name__],
                "latency_ms": round(device.estimated_latency * 1000),
                "enabled": device.ip not in self.controller.disabled_devices,
            }
            for device in self.controller.devices
        ]

    def _settings(self) -> list[dict]:
        return [
            {
                "group": group, "name": name, "label": label, "unit": unit,
                "min": low, "max": high, "step": step, "applies": applies,
                "value": getattr(module, name), "default": SETTING_DEFAULTS[name],
            }
            for group, module, name, label, unit, low, high, step, applies in SETTINGS
        ]

    def _state(self) -> dict:
        hue, saturation, brightness = self.controller.panel_state()
        return {
            "type": "state",
            "hue": hue,
            "saturation": saturation,
            "brightness": brightness,
            "max_brightness": self.controller.max_brightness,
            "sync_offset_ms": round(self.controller.sync_offset * 1000),
            "song": self.controller.current_song,
            "artists": self.controller.current_artists,
            "playing": self.controller.analysis is not None,
            "song_time": self.controller.song_time(),
            "mode": self.controller.mode,
            "rules": self.controller.rules,
            "character": {
                "drive": round(self.controller.drive, 2),
                "calm": self.controller.calm,
                "override": self.controller.character_overrides.get(self.controller.current_track_id, "auto"),
            },
            "drop_lead": self.controller.drop_lead,
            "drop_hit": self.controller.drop_hit,
            "lights": self._lights(),
        }

    def _set_setting(self, name: str, value):
        default = SETTING_DEFAULTS[name]
        # Bar counts are list indexes; they stay integers.
        setattr(SETTING_MODULES[name], name, int(value) if isinstance(default, int) else float(value))
        logger.info("Setting {} set to {} from the web app (default {})", name, getattr(SETTING_MODULES[name], name), default)

    async def _send_settings(self):
        payload = json.dumps({"type": "settings", "settings": self._settings()})
        await asyncio.gather(*(socket.send_str(payload) for socket in list(self.sockets)), return_exceptions=True)

    async def _handle(self, command: dict):
        kind = command["type"]
        if kind == "rule":
            self.controller.rules[command["name"]] = bool(command["on"])
            logger.info("Rule {} switched {} from the web app", command["name"], "on" if command["on"] else "off")
            if command["name"] in ANALYSIS_RULES and self.controller.analysis is not None:
                self.controller.reanalyse()
        elif kind == "character":
            self.controller.set_character_override(command["value"])
        elif kind == "drop_effect":
            effects = {"lead": DROP_LEADS, "hit": DROP_HITS}[command["stage"]]
            if command["name"] not in effects:
                raise ValueError(f"Unknown drop {command['stage']} effect {command['name']!r}")
            setattr(self.controller, f"drop_{command['stage']}", command["name"])
            logger.info("Drop {} effect set to {} from the web app", command["stage"], command["name"])
        elif kind == "max_brightness":
            self.controller.max_brightness = max(5, min(100, int(command["value"])))
        elif kind == "sync_offset":
            self.controller.sync_offset = float(command["value"]) / 1000
        elif kind == "light":
            self.controller.set_device_enabled(command["ip"], bool(command["on"]))
        elif kind == "identify":
            await self.controller.identify(command["ip"])
        elif kind == "setting":
            self._set_setting(command["name"], command["value"])
            await self._send_settings()
        elif kind == "reset_settings":
            for name, default in SETTING_DEFAULTS.items():
                setattr(SETTING_MODULES[name], name, default)
            logger.info("Settings reset to their defaults from the web app")
            await self._send_settings()
        elif kind == "seek":
            await self.listener.seek(float(command["position"]))
        elif kind == "reanalyse":
            self.controller.reanalyse()
        else:
            raise ValueError(f"Unknown web app command {kind!r}")

    async def _index(self, request):
        return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))

    async def _socket(self, request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_str(json.dumps({
            "type": "hello",
            "rules": [{"name": name, "description": text} for name, text in RULES.items()],
            "drop_effects": {
                stage: [{"name": name, "description": text} for name, text in effects.items()]
                for stage, effects in (("lead", DROP_LEADS), ("hit", DROP_HITS))
            },
            "settings": self._settings(),
            "log": list(self.log_lines),
        }))
        # A payload older than the loaded track goes out with the next state tick.
        if self.controller.analysis is not None and self.timeline_sent_for == self.controller.timeline_id:
            await socket.send_str(self.timeline_payload)
        self.sockets.add(socket)
        try:
            async for message in socket:
                if message.type == WSMsgType.TEXT:
                    # Logged so the failure shows in the page's log, then raised.
                    try:
                        await self._handle(json.loads(message.data))
                    except Exception:
                        logger.exception("Web app command failed: {}", message.data)
                        raise
        finally:
            self.sockets.discard(socket)
        return socket

    async def _broadcast_state(self):
        while True:
            await asyncio.sleep(STATE_INTERVAL_S)
            if self.controller.analysis is not None and self.timeline_sent_for != self.controller.timeline_id:
                self.timeline_payload = json.dumps({"type": "timeline", **self.controller.timeline()})
                self.timeline_sent_for = self.controller.timeline_id
                await asyncio.gather(
                    *(socket.send_str(self.timeline_payload) for socket in list(self.sockets)), return_exceptions=True,
                )
            if not self.sockets:
                continue
            payload = json.dumps(self._state())
            await asyncio.gather(*(socket.send_str(payload) for socket in list(self.sockets)), return_exceptions=True)

    async def serve(self, host: str, port: int):
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._socket)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        logger.info("Web app on http://{}:{}/", host, port)
        await self._broadcast_state()
