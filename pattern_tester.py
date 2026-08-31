#!/usr/bin/env python3
"""Play brightness patterns directly on the discovered bulbs.

Examples:
  python3 pattern_tester.py --energy 4 --pattern 0
  python3 pattern_tester.py --energy 2 --bpm 120 --cycles 2
  python3 pattern_tester.py --list
"""

import argparse
import asyncio
import os

from device_manager import DeviceManager
from lan_reset import load_tokens
from patterns import ENERGY_RANGES, PATTERNS


def _brightness(energy: int, level: float) -> int:
    floor, ceiling = ENERGY_RANGES[energy]
    value = floor + (ceiling - floor) * level
    return max(1, min(100, round(value / 10) * 10 or 1))


def _step_parts(step):
    if len(step) == 2:
        return step[0], 0.0, step[1]
    return step


async def _play(devices, energy: int, pattern_index: int, bpm: float, cycles: int):
    beat_seconds = 60.0 / bpm
    pattern = PATTERNS[energy][pattern_index]
    print(f"energy={energy} pattern={pattern_index} cycles={cycles} bpm={bpm:g}")
    cycle = 0
    while cycles == 0 or cycle < cycles:
        cycle += 1
        for step_index, step in enumerate(pattern):
            transition, delay, level = _step_parts(step)
            brightness = _brightness(energy, level)
            print(
                f"  cycle={cycle} step={step_index} "
                f"brightness={brightness}% transition={transition:g} beats delay={delay:g} beats"
            )
            await asyncio.gather(*(
                device.set_brightness(brightness, duration=transition * beat_seconds)
                for device in devices
            ))
            await asyncio.sleep((transition + delay) * beat_seconds)


async def _run(args):
    tokens = load_tokens(args.tokens) if os.path.exists(args.tokens) else {}
    devices = DeviceManager(enable_music_mode=True, tokens=tokens).discover_devices()
    if not devices:
        raise RuntimeError("No bulbs found")
    for device in devices:
        device.start()
    try:
        indexes = range(len(PATTERNS[args.energy])) if args.pattern is None else [args.pattern]
        cycles = args.cycles if args.pattern is not None else max(1, args.cycles)
        for pattern_index in indexes:
            await _play(devices, args.energy, pattern_index, args.bpm, cycles)
    finally:
        await asyncio.gather(*(device.stop() for device in devices), return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description="Audition emyee brightness patterns")
    parser.add_argument("--energy", type=int, choices=range(5), help="energy level to audition")
    parser.add_argument("--pattern", type=int, help="pattern index; default: all patterns for the energy")
    parser.add_argument("--bpm", type=float, default=120.0)
    parser.add_argument("--cycles", type=int, default=0, help="repetitions; default: infinite for a selected pattern")
    parser.add_argument("--tokens", default="tokens.json")
    parser.add_argument("--list", action="store_true", help="list patterns without connecting")
    args = parser.parse_args()

    if args.list:
        for energy, patterns in PATTERNS.items():
            print(f"energy {energy}: {len(patterns)} pattern(s)")
        return
    if args.energy is None:
        parser.error("--energy is required unless --list is used")
    if args.pattern is not None and not 0 <= args.pattern < len(PATTERNS[args.energy]):
        parser.error(f"pattern must be between 0 and {len(PATTERNS[args.energy]) - 1}")
    if args.bpm <= 0 or args.cycles < 0:
        parser.error("--bpm must be positive and --cycles cannot be negative")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
