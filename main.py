#!/usr/bin/env python3
import argparse
import asyncio
import colorsys
import fcntl
import math
import os
import sys
import tty
import termios
import threading
from dotenv import load_dotenv
from loguru import logger
from spotify_listener import SpotifyChangesListener
from light_controller import LightsController
from device_manager import DeviceManager
from utils import setup_logging
from lan_reset import load_tokens

_PANEL_LINES = 5
_terminal_lock = threading.RLock()


def _get_rows() -> int:
    try:
        return os.get_terminal_size().lines
    except OSError:
        return 24


def _draw_panel(controller: LightsController, rows: int) -> None:
    h, s, b = controller.panel_state()

    dim = math.log1p(b) / math.log1p(100) if b > 0 else 0
    r, g, bc = colorsys.hsv_to_rgb(h / 360, s / 100, 1.0)
    ri, gi, bi = int(r * 255 * dim), int(g * 255 * dim), int(bc * 255 * dim)

    sw = 34
    swatch = f"\033[48;2;{ri};{gi};{bi}m" + " " * sw + "\033[0m"
    filled = max(0, min(10, b // 10))
    bar = "▓" * filled + "░" * (10 - filled)

    keys = "  [↑↓] max brightness"
    song = controller.current_song or "–"
    status = f"  \033[1m► ATMOSPHERIC\033[0m  |  ♫ {song}   {bar}  {b}%  |  max:{controller.max_brightness}%"

    top = rows - _PANEL_LINES + 1
    out = (
        "\0337"
        + f"\033[{top};1H\033[2K ┌" + "─" * sw + "┐"
        + f"\033[{top + 1};1H\033[2K │{swatch}│"
        + f"\033[{top + 2};1H\033[2K └" + "─" * sw + "┘"
        + f"\033[{top + 3};1H\033[2K" + keys
        + f"\033[{top + 4};1H\033[2K" + status
        + "\0338"
    )
    with _terminal_lock:
        sys.stderr.write(out)
        sys.stderr.flush()


def main():
    parser = argparse.ArgumentParser(description="Spotify-synced Yeelight controller")
    parser.add_argument(
        "--min-confidence", type=float, default=0.0, metavar="0-1",
        help="Minimum confidence for beats/bars/segments/tatums (default: 0)",
    )
    parser.add_argument(
        "--sync-offset", type=float, default=0.0, metavar="MS",
        help="Fire light commands this many ms early (+) or late (-) relative to beat/segment (default: 0)",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=20.0, metavar="MS",
        help="Progress event interval in ms — lower = more responsive, higher = less CPU (default: 20)",
    )
    parser.add_argument(
        "--no-music-mode", action="store_true",
        help="Disable Yeelight music mode (use if getting connection errors)",
    )
    parser.add_argument(
        "--tokens", metavar="FILE", default="tokens.json",
        help="Path to {ip: token_hex} JSON for LAN reset (default: tokens.json)",
    )
    parser.add_argument(
        "--no-lan-reset", action="store_true",
        help="Skip the LAN control off→on cycle before discovery",
    )
    parser.add_argument(
        "--ip", metavar="IP", action="append", dest="ips",
        help="Connect directly to this bulb IP (skip discovery); repeat for multiple bulbs",
    )
    parser.add_argument(
        "--tasmota", metavar="IP", action="append", dest="tasmota_ips",
        help="Drive a Tasmota RGB light over HTTP at this IP; repeat for multiple lights",
    )
    parser.add_argument(
        "--esphome", metavar="IP", action="append", dest="esphome_ips",
        help="Drive an ESPHome light over the native API at this IP; repeat for multiple lights",
    )
    parser.add_argument(
        "--esphome-key", metavar="KEY",
        help="ESPHome API encryption key (noise PSK); defaults to $ESPHOME_KEY",
    )
    parser.add_argument(
        "--esphome-password", metavar="PASSWORD",
        help="ESPHome legacy API password; defaults to $ESPHOME_PASSWORD",
    )
    parser.add_argument(
        "--tasmota-no-sleep", action="store_true",
        help="Set Sleep 0 on each Tasmota light (halves command latency, more idle power, persisted to flash)",
    )
    parser.add_argument(
        "--atmospheric-pulse-on", type=int, metavar="N",
        help="Override the atmospheric DB's accented-beat count",
    )
    parser.add_argument(
        "--atmospheric-pulse-off", type=int, metavar="N",
        help="Override the atmospheric DB's subtle-beat count",
    )
    args = parser.parse_args()
    if args.atmospheric_pulse_on is not None and args.atmospheric_pulse_on < 1:
        parser.error("--atmospheric-pulse-on must be at least 1")
    if args.atmospheric_pulse_off is not None and args.atmospheric_pulse_off < 0:
        parser.error("--atmospheric-pulse-off cannot be negative")

    load_dotenv(".env")
    user_id = os.getenv('USER_ID')
    client_id = os.getenv('CLIENT_ID')
    client_secret = os.getenv('CLIENT_SECRET')

    rows = _get_rows()
    is_tty = sys.stdin.isatty() and sys.stderr.isatty()
    setup_logging("TRACE")
    old_settings = None

    tokens = {}
    if not args.no_lan_reset:
        if os.path.exists(args.tokens):
            tokens = load_tokens(args.tokens)
            logger.info(f"LAN reset enabled for {len(tokens)} bulb(s) — will reset each before connecting")
        else:
            logger.warning(f"tokens file not found at {args.tokens!r} — skipping LAN reset (use --no-lan-reset to suppress)")

    device_manager = DeviceManager(enable_music_mode=not args.no_music_mode, tokens=tokens)
    devices = []
    if args.ips:
        devices += device_manager.connect_to_ips(args.ips)
    elif not (args.tasmota_ips or args.esphome_ips):
        devices += device_manager.discover_devices()
    if args.tasmota_ips:
        devices += device_manager.connect_to_tasmota(
            args.tasmota_ips, disable_wifi_sleep=args.tasmota_no_sleep
        )
    if args.esphome_ips:
        devices += device_manager.connect_to_esphome(
            args.esphome_ips,
            noise_psk=args.esphome_key or os.getenv("ESPHOME_KEY"),
            password=args.esphome_password or os.getenv("ESPHOME_PASSWORD"),
        )

    events_queue = asyncio.Queue()
    spotify_listener = SpotifyChangesListener(user_id, client_id, client_secret, events_queue)
    spotify_listener.poll_interval = args.poll_interval / 1000

    light_controller = LightsController(devices, events_queue)
    light_controller.min_confidence = args.min_confidence
    light_controller.sync_offset = args.sync_offset / 1000
    light_controller.atmospheric_pulse_on = args.atmospheric_pulse_on
    light_controller.atmospheric_pulse_off = args.atmospheric_pulse_off

    logger.info(
        f"confidence≥{args.min_confidence} | "
        f"sync offset: {args.sync_offset:+.0f}ms | poll: {args.poll_interval:.0f}ms"
    )

    if is_tty:
        # Reserve bottom 5 lines for the panel; move cursor to top of scroll region
        sys.stderr.write(f"\033[1;{rows - _PANEL_LINES}r\033[1;1H")
        sys.stderr.flush()

        def _panel_sink(msg):
            with _terminal_lock:
                sys.stderr.write(str(msg))
                sys.stderr.flush()
                _draw_panel(light_controller, rows)

        setup_logging("TRACE", sink=_panel_sink)
        _draw_panel(light_controller, rows)

    loop = asyncio.get_event_loop()

    for d in devices:
        d.start(loop)

    if is_tty:
        old_settings = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
        def _on_key():
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                fd = sys.stdin.fileno()
                old_fl = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, old_fl | os.O_NONBLOCK)
                try:
                    rest = sys.stdin.read(2)
                except (BlockingIOError, IOError):
                    rest = ''
                finally:
                    fcntl.fcntl(fd, fcntl.F_SETFL, old_fl)
                if rest == '[A':  # up arrow
                    light_controller.max_brightness = min(100, light_controller.max_brightness + 5)
                    _draw_panel(light_controller, rows)
                elif rest == '[B':  # down arrow
                    light_controller.max_brightness = max(5, light_controller.max_brightness - 5)
                    _draw_panel(light_controller, rows)

        loop.add_reader(sys.stdin.fileno(), _on_key)

    async def _periodic_redraw():
        while True:
            await asyncio.sleep(0.05)  # 20fps for smooth color animation
            if is_tty:
                _draw_panel(light_controller, rows)

    try:
        loop.run_until_complete(
            asyncio.gather(
                spotify_listener.listen(),
                light_controller.control_lights(),
                _periodic_redraw(),
            )
        )
    except KeyboardInterrupt:
        logger.info("Stopping...")
    finally:
        if is_tty:
            loop.remove_reader(sys.stdin.fileno())
            if old_settings is not None:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
            sys.stderr.write("\033[r\033[J")
            sys.stderr.flush()
        loop.run_until_complete(asyncio.gather(*(d.stop() for d in devices), return_exceptions=True))


if __name__ == '__main__':
    main()
