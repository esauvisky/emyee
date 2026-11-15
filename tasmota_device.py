"""Tasmota light backend driven over the HTTP /cm command endpoint."""

import asyncio
import json
from typing import Optional

import requests
from loguru import logger

from light_device import BaseLightDevice, DesiredState

# The ESP8266 HTTP stack answers in ~30ms at best, so commands issued faster
# than this only pile up in the queue. Far below the Yeelight non-music limit.
MIN_COMMAND_INTERVAL = 0.035
REQUEST_TIMEOUT = 2.0
# Tasmota treats brightness 0 as "power off", and with SetOption20 off (the
# default) a later Dimmer/HsbColor command will not switch the light back on —
# a pulse trough would leave it dark for good. Floor brightness at 1 instead;
# visually that is off, but the light stays powered and keeps taking commands.
MIN_BRIGHTNESS = 1


class TasmotaDevice(BaseLightDevice):
    """A Tasmota RGB(W) light.

    Exposes the same surface as LightDevice so LightsController can drive a
    mixed fleet without caring which backend a device uses. Tasmota's own
    fading (Fade/Speed) has 0.5s granularity — far coarser than the pulse
    durations here — so it stays off and `duration` is ignored rather than
    approximated.
    """

    def __init__(
        self,
        ip: str,
        port: int = 80,
        user: Optional[str] = None,
        password: Optional[str] = None,
        min_command_interval: float = MIN_COMMAND_INTERVAL,
        disable_wifi_sleep: bool = False,
    ):
        super().__init__(ip)
        self.port = port
        self.user = user
        self.password = password
        self.min_command_interval = min_command_interval
        self.disable_wifi_sleep = disable_wifi_sleep
        # No 1cmd/s ceiling like a Yeelight outside music mode; the panel reads
        # this flag to show the device as freely drivable.
        self.music_mode = True
        self._session = requests.Session()
        self._base_url = f"http://{ip}:{port}/cm"

    # ---- HTTP plumbing ----

    def _send(self, command: str) -> dict:
        """Issue one Tasmota command. Blocking — always called via to_thread."""
        params = {"cmnd": command}
        if self.user is not None:
            params["user"] = self.user
            params["password"] = self.password or ""
        response = self._session.get(self._base_url, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        body = response.text.strip()
        if not body:
            return {}
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"non-JSON reply to {command!r}: {body[:120]!r}")
        if isinstance(payload, dict) and "WARNING" in payload:
            raise RuntimeError(f"{command!r} rejected: {payload['WARNING']}")
        return payload

    # ---- Connection ----

    def connect(self) -> "TasmotaDevice":
        """Identify the device and put it in a state fit for beat sync.

        Raises if the endpoint is unreachable or is not a Tasmota light.
        """
        status = self._send("Status 0")
        info = status.get("Status") or {}
        self.model = info.get("DeviceName") or info.get("FriendlyName", ["unknown"])[0]

        firmware = (status.get("StatusFWR") or {}).get("Version", "unknown")
        state = self._send("Status 11").get("StatusSTS") or {}
        if "HSBColor" not in state:
            raise RuntimeError(
                f"{self.ip} is a Tasmota device ({self.model}) but reports no colour channels — "
                "not a light this controller can drive"
            )

        channels = state.get("Channel") or []
        logger.info(
            f"Tasmota {self.model} at {self.ip} — firmware {firmware}, {len(channels)} channels"
        )

        # Fading would smear the pulses; the controller does its own shaping.
        self._send("Fade 0")
        if self.disable_wifi_sleep:
            # Halves command latency at the cost of idle power. Persisted to flash.
            self._send("Sleep 0")
            logger.info(f"[{self.ip}] wifi sleep disabled for lower command latency")
        return self

    async def stop(self, final: Optional[DesiredState] = None):
        await super().stop(final)
        self._session.close()

    # ---- Backend hooks ----

    def _post_command_delay(self) -> float:
        return self.min_command_interval

    async def _apply(self, desired: DesiredState):
        loop_time = asyncio.get_event_loop().time

        if desired.mode == "hsv":
            hue = int(desired.hue) % 360
            sat = max(0, min(100, int(desired.saturation)))
            bri = max(MIN_BRIGHTNESS, min(100, int(desired.brightness)))
            logger.trace(f"[{self.ip}] apply HSB ({hue},{sat},{bri})")
            command = f"HsbColor {hue},{sat},{bri}"

        elif desired.mode == "brightness":
            bri = max(MIN_BRIGHTNESS, min(100, int(desired.brightness)))
            logger.trace(f"[{self.ip}] apply Dimmer {bri}")
            command = f"Dimmer {bri}"

        elif desired.mode == "ct":
            bri = max(MIN_BRIGHTNESS, min(100, int(desired.brightness)))
            mireds = round(1_000_000 / desired.kelvin)
            logger.trace(f"[{self.ip}] apply CT {mireds} Dimmer {bri}")
            command = f"Backlog Power ON; CT {mireds}; Dimmer {bri}"

        elif desired.mode == "on":
            logger.trace(f"[{self.ip}] turn_on")
            command = "Power ON"

        elif desired.mode == "off":
            logger.trace(f"[{self.ip}] turn_off")
            command = "Power OFF"

        else:
            return

        t0 = loop_time()
        await asyncio.to_thread(self._send, command)
        self._record_latency(loop_time() - t0)
