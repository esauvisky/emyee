#!/usr/bin/env python3
"""
brightness_tester.py — Find perceptible brightness steps on your Yeelight bulbs.

Controls:
  ↑ / ↓       adjust brightness by 10
  m            mark current level as a "good" step
  u            unmark current level
  c            compare: alternate between last two marked levels (1s each)
  s            sweep through all marked levels in order (1.5s each)
  q / Ctrl+C   quit and print the final marked step list
"""

import asyncio
import sys
import tty
import termios
import fcntl
import os
from loguru import logger
from device_manager import DeviceManager
from lan_reset import load_tokens
from utils import setup_logging

# ──────────────────────────────────────────────
TRANSITION_MS = 150       # ms for each brightness command
COMPARE_HOLD  = 1.0       # seconds per level in compare/sweep mode
TOKENS_FILE   = "tokens.json"
# ──────────────────────────────────────────────

marked: list[int] = []
current_bri: int = 50   # starts at 50, moves in steps of 10


def _draw(message: str = ""):
    bar_filled = current_bri // 5
    bar = "█" * bar_filled + "░" * (20 - bar_filled)
    marked_str = ", ".join(str(v) for v in sorted(marked)) if marked else "(none)"
    sys.stderr.write(
        "\033[2J\033[H"                          # clear screen
        f"  Brightness tester\n"
        f"  {'─' * 40}\n\n"
        f"  Current: {current_bri:3d}%  [{bar}]\n\n"
        f"  Marked:  {marked_str}\n\n"
        f"  ↑/↓ ±10   m=mark   u=unmark\n"
        f"  c=compare last 2   s=sweep   q=quit\n"
    )
    if message:
        sys.stderr.write(f"\n  {message}\n")
    sys.stderr.flush()


async def _set(devices, bri: int):
    await asyncio.gather(*(d.set_brightness(bri, duration=TRANSITION_MS / 1000) for d in devices))


async def _compare(devices):
    if len(marked) < 2:
        return
    a, b = sorted(marked)[-2], sorted(marked)[-1]
    for _ in range(4):
        await _set(devices, a)
        _draw(f"comparing {a}% ↔ {b}%  (Ctrl-C to stop)")
        await asyncio.sleep(COMPARE_HOLD)
        await _set(devices, b)
        _draw(f"comparing {a}% ↔ {b}%  (Ctrl-C to stop)")
        await asyncio.sleep(COMPARE_HOLD)
    await _set(devices, current_bri)
    _draw()


async def _sweep(devices):
    steps = sorted(marked) if marked else list(range(10, 101, 10))
    for bri in steps:
        await _set(devices, bri)
        _draw(f"sweeping — {bri}%")
        await asyncio.sleep(COMPARE_HOLD * 1.5)
    await _set(devices, current_bri)
    _draw()


async def main():
    global current_bri

    setup_logging("WARNING")

    tokens = load_tokens(TOKENS_FILE) if os.path.exists(TOKENS_FILE) else {}
    dm = DeviceManager(enable_music_mode=True, tokens=tokens)
    devices = dm.discover_devices()
    if not devices:
        sys.stderr.write("No devices found.\n")
        return

    for d in devices:
        d.start()

    await _set(devices, current_bri)
    _draw()

    old_settings = termios.tcgetattr(sys.stdin.fileno())
    tty.setcbreak(sys.stdin.fileno())

    loop = asyncio.get_event_loop()

    def _read_key() -> str:
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
            return '\x1b' + rest
        return ch

    pending_task: asyncio.Task | None = None

    async def _handle(ch: str):
        global current_bri
        nonlocal pending_task

        if pending_task and not pending_task.done():
            pending_task.cancel()
            try:
                await pending_task
            except asyncio.CancelledError:
                pass
            pending_task = None

        if ch == '\x1b[A':    # up
            current_bri = min(100, current_bri + 10)
        elif ch == '\x1b[B':  # down
            current_bri = max(1, current_bri - 10)
        elif ch == 'm':
            if current_bri not in marked:
                marked.append(current_bri)
        elif ch == 'u':
            if current_bri in marked:
                marked.remove(current_bri)
        elif ch == 'c':
            pending_task = loop.create_task(_compare(devices))
            return
        elif ch == 's':
            pending_task = loop.create_task(_sweep(devices))
            return
        elif ch in ('q', '\x03', '\x04'):
            raise KeyboardInterrupt

        await _set(devices, current_bri)
        _draw()

    key_queue: asyncio.Queue[str] = asyncio.Queue()

    def _on_key():
        key_queue.put_nowait(_read_key())

    loop.add_reader(sys.stdin.fileno(), _on_key)

    try:
        while True:
            ch = await key_queue.get()
            await _handle(ch)
    except KeyboardInterrupt:
        pass
    finally:
        loop.remove_reader(sys.stdin.fileno())
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
        sys.stderr.write("\033[2J\033[H")

        final = sorted(marked)
        sys.stdout.write("\nFinal marked brightness steps:\n")
        sys.stdout.write(f"  {final}\n\n")
        if final:
            sys.stdout.write("Copy into light_controller.py as BRI_STEPS:\n")
            sys.stdout.write(f"  BRI_STEPS = {final}\n\n")

        await asyncio.gather(*(d.stop() for d in devices), return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(main())
