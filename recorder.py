#!/usr/bin/env python3
"""Record human brightness and colour cues against the Spotify track playing.

  space  brightness pulse here
  enter  hold for a colour transition: press = it starts, release = it ends
  q      quit

Each track take goes to recordings/<unix time>_<track id>.jsonl: a header line
with the track metadata and the full audio analysis, then one line per cue
with its song-progress time in seconds. Colour cues carry both the press time
(`t`) and the release time (`t_end`); a plain tap is an instant change
(`t_end == t`). Cues also drive the lights so you see what you are producing.

A tty never reports key releases, so the hold is read off keyboard auto-repeat:
the first Enter opens it and it closes once the repeat stream stops.

Examples:
  python3 recorder.py --esphome 192.168.1.50
  python3 recorder.py --ip 192.168.1.20 --no-lan-reset
"""

import argparse
import asyncio
import json
import os
import sys
import termios
import time
import tty

from dotenv import load_dotenv

from device_manager import DeviceManager
from lan_reset import load_tokens
from models import EventAdjustProgressTime, EventSongChanged, EventStop
from spotify_listener import SpotifyChangesListener
from utils import get_vibrant_color, setup_logging

RECORDINGS_DIR = "recordings"
FLASH_PEAK = 100
FLASH_FLOOR = 15
FLASH_ATTACK = 0.04
FLASH_RELEASE = 0.35
HOLD_FADE = 3.0           # fade pace while Enter is held; the release snaps it to the target
RELEASE_FADE = 0.15
# Auto-repeat timing: the first repeat follows the press after the OS initial
# delay (250–660ms depending on console/X/Wayland), later ones every 25–40ms.
HOLD_FIRST_GAP = 1.0
HOLD_REPEAT_GAP = 0.2


class Recorder:
    def __init__(self, devices):
        self.devices = devices
        self.take = None
        self.song = ""
        self.progress = 0.0
        self.progress_mono = time.monotonic()
        self.counts = {"brightness": 0, "color": 0}
        self.hue, self.saturation = get_vibrant_color(0)
        self._flash_task: asyncio.Task | None = None
        self.hold_start: float | None = None   # song time of the Enter press
        self.hold_end = 0.0                     # song time of the latest Enter repeat
        self.hold_wall = 0.0
        self.hold_last_mono = 0.0
        self.hold_repeats = 0
        self._hold_task: asyncio.Task | None = None

    def song_time(self) -> float:
        return self.progress + time.monotonic() - self.progress_mono

    def open_take(self, event: EventSongChanged):
        self.close_take()
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        path = os.path.join(RECORDINGS_DIR, f"{int(time.time())}_{event.track_id}.jsonl")
        self.take = open(path, "w")
        self.song = f"{event.song_name} — {event.artist_names}"
        self.progress = event.progress_time_ms
        self.progress_mono = time.monotonic()
        self.counts = {"brightness": 0, "color": 0}
        self._write({
            "type": "track",
            "track_id": event.track_id,
            "song_name": event.song_name,
            "artist_names": event.artist_names,
            "album_name": event.album_name,
            "recorded_at": time.time(),
            "start_progress": event.progress_time_ms,
            "analysis": event.analysis,
        })

    def close_take(self):
        if self.take is not None:
            if self.hold_start is not None:
                self._hold_task.cancel()
                self._write_hold()
                self.hold_start = None
            self.take.close()
            self.take = None
            self.song = ""

    def _write(self, record: dict):
        self.take.write(json.dumps(record) + "\n")
        self.take.flush()

    async def brightness_tap(self):
        # Cues outside a take (nothing playing) still drive the lights, they
        # just have no song timeline to land on.
        if self.take is not None:
            self._write({"type": "brightness", "t": round(self.song_time(), 4), "wall": time.time()})
            self.counts["brightness"] += 1
        if self._flash_task is not None and not self._flash_task.done():
            self._flash_task.cancel()
        self._flash_task = asyncio.create_task(self._flash())

    async def color_press(self):
        """One Enter character: either the press that opens a hold or an auto-repeat inside it."""
        self.hold_last_mono = time.monotonic()
        self.hold_end = self.song_time()
        if self.hold_start is not None:
            self.hold_repeats += 1
            return
        self.hold_start = self.hold_end
        self.hold_wall = time.time()
        self.hold_repeats = 0
        self._hold_task = asyncio.create_task(self._hold_watch())
        self.hue, self.saturation = get_vibrant_color(self.hue)
        await asyncio.gather(*(
            d.set_hsv(self.hue, self.saturation, FLASH_FLOOR, duration=HOLD_FADE) for d in self.devices
        ))

    async def _hold_watch(self):
        while True:
            gap = HOLD_REPEAT_GAP if self.hold_repeats else HOLD_FIRST_GAP
            remaining = self.hold_last_mono + gap - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(remaining)
        if self.take is not None:
            self._write_hold()
        self.hold_start = None
        await asyncio.gather(*(
            d.set_hsv(self.hue, self.saturation, FLASH_FLOOR, duration=RELEASE_FADE) for d in self.devices
        ))

    def _write_hold(self):
        self._write({
            "type": "color",
            "t": round(self.hold_start, 4),
            "t_end": round(self.hold_end, 4),
            "wall": self.hold_wall,
        })
        self.counts["color"] += 1

    async def _flash(self):
        await asyncio.gather(*(d.set_brightness(FLASH_PEAK, duration=FLASH_ATTACK) for d in self.devices))
        await asyncio.sleep(FLASH_ATTACK)
        await asyncio.gather(*(d.set_brightness(FLASH_FLOOR, duration=FLASH_RELEASE) for d in self.devices))

    def draw(self):
        if self.take is None:
            line = "  waiting for a track…  [space] brightness  [hold enter] colour transition  [q] quit"
        else:
            t = self.song_time()
            line = (
                f"  ● REC  {self.song}  {int(t // 60)}:{t % 60:05.2f}  "
                f"bri:{self.counts['brightness']}  col:{self.counts['color']}"
            )
            if self.hold_start is not None:
                line += f"  ▶ colour {t - self.hold_start:.2f}s"
        sys.stderr.write("\r\033[2K" + line)
        sys.stderr.flush()


async def _consume(recorder: Recorder, events: asyncio.Queue):
    while True:
        event = await events.get()
        if isinstance(event, EventSongChanged):
            recorder.open_take(event)
        elif isinstance(event, EventAdjustProgressTime):
            recorder.progress = event.progress_time_ms
            recorder.progress_mono = time.monotonic()
        elif isinstance(event, EventStop):
            recorder.close_take()


async def _redraw(recorder: Recorder):
    while True:
        recorder.draw()
        await asyncio.sleep(0.1)


async def main(args):
    load_dotenv(".env")
    setup_logging("WARNING")

    tokens = load_tokens(args.tokens) if not args.no_lan_reset and os.path.exists(args.tokens) else {}
    device_manager = DeviceManager(enable_music_mode=not args.no_music_mode, tokens=tokens)
    devices = []
    if args.ips:
        devices += device_manager.connect_to_ips(args.ips)
    elif not (args.tasmota_ips or args.esphome_ips):
        devices += device_manager.discover_devices()
    if args.tasmota_ips:
        devices += device_manager.connect_to_tasmota(args.tasmota_ips)
    if args.esphome_ips:
        devices += device_manager.connect_to_esphome(
            args.esphome_ips,
            noise_psk=args.esphome_key or os.getenv("ESPHOME_KEY"),
            password=args.esphome_password or os.getenv("ESPHOME_PASSWORD"),
        )

    loop = asyncio.get_running_loop()
    for d in devices:
        d.start(loop)

    recorder = Recorder(devices)
    await asyncio.gather(*(d.turn_on() for d in devices))
    await asyncio.gather(*(d.set_hsv(recorder.hue, recorder.saturation, FLASH_FLOOR) for d in devices))

    events = asyncio.Queue()
    listener = SpotifyChangesListener(os.getenv("USER_ID"), os.getenv("CLIENT_ID"), os.getenv("CLIENT_SECRET"), events)

    keys: asyncio.Queue[str] = asyncio.Queue()
    old_settings = termios.tcgetattr(sys.stdin.fileno())
    tty.setcbreak(sys.stdin.fileno())
    loop.add_reader(sys.stdin.fileno(), lambda: keys.put_nowait(sys.stdin.read(1)))

    tasks = [
        asyncio.create_task(listener.listen()),
        asyncio.create_task(_consume(recorder, events)),
        asyncio.create_task(_redraw(recorder)),
    ]
    try:
        while True:
            ch = await keys.get()
            if ch == "q":
                break
            if ch == " ":
                await recorder.brightness_tap()
            elif ch == "\n":
                await recorder.color_press()
    finally:
        loop.remove_reader(sys.stdin.fileno())
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
        sys.stderr.write("\n")
        for task in tasks:
            task.cancel()
        recorder.close_take()
        await asyncio.gather(*(d.stop() for d in devices))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Record human brightness pulses and colour holds against Spotify playback")
    parser.add_argument("--ip", metavar="IP", action="append", dest="ips", help="Yeelight bulb IP (skip discovery); repeatable")
    parser.add_argument("--tasmota", metavar="IP", action="append", dest="tasmota_ips", help="Tasmota light IP; repeatable")
    parser.add_argument("--esphome", metavar="IP", action="append", dest="esphome_ips", help="ESPHome light IP; repeatable")
    parser.add_argument("--esphome-key", metavar="KEY", help="ESPHome API encryption key; defaults to $ESPHOME_KEY")
    parser.add_argument("--esphome-password", metavar="PASSWORD", help="ESPHome legacy API password; defaults to $ESPHOME_PASSWORD")
    parser.add_argument("--tokens", metavar="FILE", default="tokens.json", help="{ip: token_hex} JSON for LAN reset")
    parser.add_argument("--no-lan-reset", action="store_true", help="Skip the LAN control off→on cycle")
    parser.add_argument("--no-music-mode", action="store_true", help="Disable Yeelight music mode")
    asyncio.run(main(parser.parse_args()))
