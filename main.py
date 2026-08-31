#!/usr/bin/env python3
import argparse
import asyncio
import colorsys
import os
import sys
import tty
import termios
from dotenv import load_dotenv
from loguru import logger
from spotify_listener import SpotifyChangesListener
from light_controller import LightsController, Mode
from device_manager import DeviceManager
from utils import setup_logging
from lan_reset import load_tokens, reset_lan_control

_PANEL_LINES = 5


def _get_rows() -> int:
    try:
        return os.get_terminal_size().lines
    except OSError:
        return 24


def _draw_panel(controller: LightsController, rows: int) -> None:
    h, s, b = controller.panel_state()

    dim = b / 100
    r, g, bc = colorsys.hsv_to_rgb(h / 360, s / 100, 1.0)
    ri, gi, bi = int(r * 255 * dim), int(g * 255 * dim), int(bc * 255 * dim)

    sw = 34
    swatch = f"\033[48;2;{ri};{gi};{bi}m" + " " * sw + "\033[0m"
    filled = max(0, min(10, b // 10))
    bar = "▓" * filled + "░" * (10 - filled)

    modes = list(Mode)
    keys = "  " + "  ".join(f"[{i + 1}] {m.value}" for i, m in enumerate(modes))
    song = controller.current_song or "–"
    status = f"  \033[1m► {controller.mode.value.upper()}\033[0m  |  ♫ {song}   {bar}  {b}%"

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
    sys.stderr.write(out)
    sys.stderr.flush()


def main():
    parser = argparse.ArgumentParser(description="Spotify-synced Yeelight controller")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in Mode],
        default="atmospheric",
        help="Lighting mode (default: atmospheric)",
    )
    parser.add_argument(
        "--min-confidence", type=float, default=0.5, metavar="0-1",
        help="Minimum confidence for beats/bars/segments/tatums (default: 0.5)",
    )
    parser.add_argument(
        "--sync-offset", type=float, default=0.0, metavar="MS",
        help="Fire light commands this many ms early (+) or late (-) relative to beat/segment (default: 0)",
    )
    parser.add_argument(
        "--breathe-period", type=int, default=4, metavar="BARS",
        help="Number of bars per breathe cycle (default: 4)",
    )
    parser.add_argument(
        "--strobe-duty", type=float, default=0.15, metavar="0-1",
        help="Strobe on-time as a fraction of beat duration (default: 0.15)",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=100.0, metavar="MS",
        help="Progress event interval in ms — lower = more responsive, higher = less CPU (default: 100)",
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
    args = parser.parse_args()

    load_dotenv(".env")
    user_id = os.getenv('USER_ID')
    client_id = os.getenv('CLIENT_ID')
    client_secret = os.getenv('CLIENT_SECRET')

    if not args.no_lan_reset:
        if os.path.exists(args.tokens):
            tokens = load_tokens(args.tokens)
            logger.info(f"Resetting LAN control for {len(tokens)} bulb(s) via miIO...")
            reset_lan_control(tokens)
        else:
            logger.warning(f"tokens file not found at {args.tokens!r} — skipping LAN reset (use --no-lan-reset to suppress)")

    device_manager = DeviceManager(enable_music_mode=not args.no_music_mode)
    devices = device_manager.discover_devices()

    events_queue = asyncio.Queue()
    spotify_listener = SpotifyChangesListener(user_id, client_id, client_secret, events_queue)
    spotify_listener.poll_interval = args.poll_interval / 1000

    light_controller = LightsController(devices, events_queue)
    light_controller.mode = Mode[args.mode.upper()]
    light_controller.min_confidence = args.min_confidence
    light_controller.sync_offset = args.sync_offset / 1000
    light_controller.breathe_period = args.breathe_period
    light_controller.strobe_duty = args.strobe_duty

    logger.info(
        f"Mode: {args.mode} | confidence≥{args.min_confidence} | "
        f"sync offset: {args.sync_offset:+.0f}ms | poll: {args.poll_interval:.0f}ms"
    )

    rows = _get_rows()
    is_tty = sys.stdin.isatty() and sys.stderr.isatty()
    old_settings = None

    if is_tty:
        # Reserve bottom 5 lines for the panel; move cursor to top of scroll region
        sys.stderr.write(f"\033[1;{rows - _PANEL_LINES}r\033[1;1H")
        sys.stderr.flush()

        def _panel_sink(msg):
            sys.stderr.write(str(msg))
            _draw_panel(light_controller, rows)

        setup_logging("TRACE", sink=_panel_sink)
        _draw_panel(light_controller, rows)
    else:
        setup_logging("TRACE")

    loop = asyncio.get_event_loop()

    for d in devices:
        d.start(loop)

    if is_tty:
        old_settings = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
        _modes = list(Mode)

        def _on_key():
            ch = sys.stdin.read(1)
            if ch.isdigit():
                idx = int(ch) - 1
                if 0 <= idx < len(_modes):
                    light_controller.mode = _modes[idx]
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
