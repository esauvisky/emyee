"""Editable brightness choreography.

Each step is ``(transition_beats, delay_beats, target_brightness)``. The delay
holds the target after its transition without sending another command. The old
two-field ``(duration_beats, target_brightness)`` form means no delay.
Long sections choose another pattern after each complete pattern cycle.
"""

from math import isclose

ENERGY_RANGES = {
    0: (5, 25),
    1: (5, 40),
    2: (10, 60),
    3: (15, 80),
    4: (20, 100),
}

ENERGY_DB_CUTOFFS = (2, 4, 6, 9)
ALLOWED_PATTERN_BEATS = (4, 8, 16)
FAST_BEATS = 0.10
SEGMENT_VARIATION = 0.10
STAGE_STRENGTH = {
    "opening": 0.55,
    "build": 0.15,
    "release": 0.15,
    "ending": 0.75,
}


def wave(length: float = 16, steps: int = 8):
    """Build one smooth rise and fall."""
    levels = [i / (steps - 1) for i in range(steps)]
    return [(length / (2 * steps - 2), 0, level) for level in levels + levels[-2:0:-1]]


def rise_and_hits(fast: float = FAST_BEATS):
    """Four beats: slow rise, two quick hits, slow rise, quick release."""
    if not 0 < fast < 0.5:
        raise ValueError("fast must be between 0 and half a beat")
    hold = 1 - 2 * fast
    return [
        (1, 0, 1.00),
        (fast, 0, 0.10),
        (0, hold, 0.10),
        (fast, 0, 1.00),
        (fast, 0, 0.10),
        (0, hold, 0.10),
        (1, 0, 1.00),
        (fast, 0, 0.00),
    ]

PATTERNS = {
    # -------------------------------------------------------------------------
    # ENERGY 0 — Ambient / Slow Swells (4 Bars = 16 Beats)
    # -------------------------------------------------------------------------
    0: [
        # Variant 0.1: Continuous 2-phase slow breath (8b rise, 8b fall)
        [
            (8.0, 0.40),  # Bars 1-2: 8.0 beats
            (8.0, 0.05),  # Bars 3-4: 8.0 beats
        ],  # Total: 16.0 beats
        # Variant 0.2: Deep swell with resting floor (6b ramp + 2b hold each)
        [
            (6.0, 2.0, 0.45),  # Bars 1-2: 6.0 + 2.0 = 8.0 beats
            (6.0, 2.0, 0.00),  # Bars 3-4: 6.0 + 2.0 = 8.0 beats
        ],  # Total: 16.0 beats
        # Variant 0.3: Two evolving 8-beat waves
        [
            (3.0, 1.0, 0.25),  # Bar 1: 4.0 beats
            (4.0, 0.05),  # Bar 2: 4.0 beats
            (4.0, 1.0, 0.45),  # Bar 3: 5.0 beats
            (3.0, 0.00),  # Bar 4: 3.0 beats
        ],  # Total: 16.0 beats
    ],
    # -------------------------------------------------------------------------
    # ENERGY 1 — Low-Medium Groove (4 Bars = 16 Beats)
    # -------------------------------------------------------------------------
    1: [
        # Variant 1.1: Rolling 2-beat sways across 4 bars
        [
            (1.5, 0.50),
            (0.5, 0.15),
            (1.5, 0.45),
            (0.5, 0.10),  # Bar 1: 4.0 beats
            (1.5, 0.55),
            (0.5, 0.20),
            (1.5, 0.50),
            (0.5, 0.15),  # Bar 2: 4.0 beats
            (1.5, 0.65),
            (0.5, 0.25),
            (1.5, 0.60),
            (0.5, 0.20),  # Bar 3: 4.0 beats
            (2.0, 0.40),
            (2.0, 0.05),  # Bar 4: 4.0 beats
        ],  # Total: 16.0 beats
        # Variant 1.2: 4-bar alternating two-step pulse
        [
            (2.0, 0.60),
            (2.0, 0.20),  # Bar 1: 4.0 beats
            (2.0, 0.55),
            (2.0, 0.15),  # Bar 2: 4.0 beats
            (2.0, 0.70),
            (2.0, 0.25),  # Bar 3: 4.0 beats
            (3.0, 0.40),
            (1.0, 0.05),  # Bar 4: 4.0 beats
        ],  # Total: 16.0 beats
        # Variant 1.3: Backbeat pulse with sustained holds
        [
            (1.0, 1.0, 0.55),
            (2.0, 0.15),  # Bar 1: 2.0 + 2.0 = 4.0 beats
            (1.0, 1.0, 0.60),
            (2.0, 0.15),  # Bar 2: 2.0 + 2.0 = 4.0 beats
            (1.0, 1.0, 0.65),
            (2.0, 0.20),  # Bar 3: 2.0 + 2.0 = 4.0 beats
            (2.0, 0.40),
            (2.0, 0.05),  # Bar 4: 2.0 + 2.0 = 4.0 beats
        ],  # Total: 16.0 beats
    ],
    # -------------------------------------------------------------------------
    # ENERGY 2 — Driving Groove / 4-Bar Build (4 Bars = 16 Beats)
    # -------------------------------------------------------------------------
    2: [
        # Variant 2.1: 3 bars four-on-the-floor (12b) + Bar 4 turnaround (4b)
        [
            # Bars 1-3: 12 pairs of 1.0b = 12.0 beats
            *[(0.5, 0.80), (0.5, 0.20)] * 12,
            # Bar 4: Turnaround fill = 4.0 beats
            (1.0, 0.95),  # Beat 13 (1.0b)
            (1.0, 0.20),  # Beat 14 (1.0b)
            (1.5, 0.5, 1.00),  # Beats 15-16 (2.0b)
        ],  # Total: 16.0 beats
        # Variant 2.2: 4-bar syncopated Tresillo groove (3+3+2 + 3+3+2 beats)
        [
            # Bars 1-2 (8.0 beats)
            (1.0, 0.85),
            (2.0, 0.15),  # Hit 1 (3.0b)
            (1.0, 0.85),
            (2.0, 0.15),  # Hit 2 (3.0b)
            (0.75, 0.25, 0.90),
            (1.0, 0.10),  # Hit 3 (2.0b)
            # Bars 3-4 (8.0 beats)
            (1.0, 0.95),
            (2.0, 0.20),  # Hit 4 (3.0b)
            (1.0, 0.95),
            (2.0, 0.20),  # Hit 5 (3.0b)
            (1.0, 1.0, 1.00),  # Hit 6 (2.0b)
        ],  # Total: 16.0 beats
        # Variant 2.3: Progressive 4-bar ramp
        [
            (1.0, 0.40),
            (1.0, 0.20),
            (1.0, 0.45),
            (1.0, 0.20),  # Bar 1 (4.0b)
            (1.0, 0.60),
            (1.0, 0.25),
            (1.0, 0.65),
            (1.0, 0.25),  # Bar 2 (4.0b)
            (0.5, 0.80),
            (0.5, 0.30),
            (0.5, 0.85),
            (0.5, 0.30),  # Bar 3 part 1 (2.0b)
            (0.5, 0.90),
            (0.5, 0.30),
            (0.5, 0.95),
            (0.5, 0.30),  # Bar 3 part 2 (2.0b)
            (2.0, 1.0, 1.00),  # Bar 4 flare (3.0b)
            (1.0, 0.10),  # Bar 4 reset (1.0b)
        ],  # Total: 16.0 beats
    ],
    # -------------------------------------------------------------------------
    # ENERGY 3 — High Energy / Drop (4 Bars = 16 Beats)
    # -------------------------------------------------------------------------
    3: [
        # Variant 3.1: 3.5 bars heavy pump (14b) + 2-beat drop silence (2b)
        [
            # Bars 1-3.5: 14 pairs of 1.0b = 14.0 beats
            *[(0.5, 1.00), (0.5, 0.15)] * 14,
            # Bar 4 finish: 2-beat blackout fakeout (0.5b fade + 1.5b hold) = 2.0 beats
            (0.5, 1.5, 0.00),
        ],  # Total: 16.0 beats
        # Variant 3.2: Call & response syncopation across 4 bars (CORRECTED)
        [
            # Bar 1: 0.5 + 0.5 + 1.0 + 0.5 + 0.5 + 1.0 = 4.0 beats
            (0.5, 1.00),
            (0.5, 0.20),
            (0.5, 0.5, 0.90),
            (0.5, 1.00),
            (0.5, 0.20),
            (0.5, 0.5, 0.20),
            # Bar 2: 0.5 + 0.5 + 1.0 + 0.5 + 0.5 + 1.0 = 4.0 beats
            (0.5, 1.00),
            (0.5, 0.20),
            (0.5, 0.5, 0.90),
            (0.5, 1.00),
            (0.5, 0.20),
            (0.5, 0.5, 0.20),
            # Bar 3: 4 pairs of 1.0b = 4.0 beats
            *[(0.5, 1.00), (0.5, 0.20)] * 4,
            # Bar 4: Sustained flash (2.0b) & dark rest (2.0b) = 4.0 beats
            (0.5, 1.5, 1.00),
            (0.5, 1.5, 0.05),
        ],  # Total: 16.0 beats
        # Variant 3.3: Tension & release (2-bar pump -> 1-bar build -> 1-bar blast)
        [
            # Bars 1-2: 8 pairs of 1.0b = 8.0 beats
            *[(0.5, 1.00), (0.5, 0.20)] * 8,
            # Bar 3: 4 pairs of 1.0b = 4.0 beats
            *[(0.25, 0.25, 1.00), (0.25, 0.25, 0.30)] * 4,
            # Bar 4: 2.0b flash + 2.0b silence = 4.0 beats
            (0.5, 1.5, 1.00),
            (0.5, 1.5, 0.00),
        ],  # Total: 16.0 beats
    ],
    # -------------------------------------------------------------------------
    # ENERGY 4 — Peak / Climax (4 Bars = 16 Beats)
    # -------------------------------------------------------------------------
    4: [
        # Variant 4.1: 3 bars strobe-hold (12b) + Bar 4 full whiteout drop (4b)
        [
            # Bars 1-3: 12 pairs of 1.0b = 12.0 beats
            *[(0.25, 0.25, 1.00), (0.25, 0.25, 0.15)] * 12,
            # Bar 4: 2.0b hold + 2.0b blackout = 4.0 beats
            (0.50, 1.50, 1.00),
            (0.50, 1.50, 0.00),
        ],  # Total: 16.0 beats
        # Variant 4.2: 4-bar syncopated high-impact blast
        [
            # Bars 1-2 (8.0 beats): 4 groups of 2.0b
            (0.25, 0.25, 1.00),
            (0.25, 0.25, 1.00),
            (0.50, 0.50, 0.10),
            (0.25, 0.25, 1.00),
            (0.25, 0.25, 1.00),
            (0.50, 0.50, 0.10),
            (0.25, 0.25, 1.00),
            (0.25, 0.25, 1.00),
            (0.50, 0.50, 0.10),
            (0.25, 0.25, 1.00),
            (0.25, 0.25, 1.00),
            (0.50, 0.50, 0.10),
            # Bar 3 (4.0 beats): 4 pairs of 1.0b
            *[(0.25, 0.25, 1.00), (0.25, 0.25, 0.20)] * 4,
            # Bar 4 (4.0 beats): 3.0b flare + 1.0b cut
            (0.50, 2.50, 1.00),
            (1.00, 0.00),
        ],  # Total: 16.0 beats
        # Variant 4.3: 4-bar progressive speed-up
        [
            # Bar 1 (4.0 beats): 4 pairs of 1.0b
            *[(0.50, 1.00), (0.50, 0.20)] * 4,
            # Bars 2-3 (8.0 beats): 8 pairs of 1.0b
            *[(0.25, 0.25, 1.00), (0.25, 0.25, 0.20)] * 8,
            # Bar 4 (4.0 beats): 1.0b + 1.0b + 2.0b
            (0.25, 0.75, 1.00),
            (0.25, 0.75, 1.00),
            (0.50, 1.50, 0.00),
        ],  # Total: 16.0 beats
    ],
}

def energy_level(loudness: float, loudest: float) -> int:
    """Classify a section by how many dB it sits below the song's loudest."""
    difference = loudest - loudness
    for level, cutoff in zip((4, 3, 2, 1), ENERGY_DB_CUTOFFS):
        if difference < cutoff:
            return level
    return 0


def validate():
    if set(PATTERNS) != set(ENERGY_RANGES) or set(PATTERNS) != set(range(5)):
        raise ValueError("patterns and brightness ranges must define energy levels 0 through 4")
    for energy, patterns in PATTERNS.items():
        for index, pattern in enumerate(patterns):
            if not pattern:
                raise ValueError(f"invalid energy {energy} pattern {index}")
            total = 0.0
            for step in pattern:
                if len(step) == 2:
                    transition, delay, level = step[0], 0, step[1]
                elif len(step) == 3:
                    transition, delay, level = step
                else:
                    raise ValueError(f"invalid energy {energy} pattern {index} step {step!r}")
                if transition < 0 or delay < 0 or transition + delay <= 0 or not 0 <= level <= 1:
                    raise ValueError(f"invalid energy {energy} pattern {index} step {step!r}")
                total += transition + delay
            if not any(isclose(total, allowed) for allowed in ALLOWED_PATTERN_BEATS):
                raise ValueError(f"energy {energy} pattern {index} totals {total:g} beats; expected 4, 8, or 16")


validate()
