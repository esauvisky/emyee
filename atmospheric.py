"""Editable profiles for atmospheric beat pulses, and the brightness model they play on.

Brightness is planned in perceptual *levels* (0–1, equal steps look equal)
and only converted to device brightness at the end. Each section gets a band
of levels from its activity: calm sections swing gently in a narrow band up
high, active ones swing from near black to full. Pulses then place each
beat's peak inside that band and release back to its bottom.
"""

# Perceived brightness grows roughly with the square root of light output,
# so a 10%→20% step looks huge while 80%→90% barely registers. Levels are
# squared on the way out so equal level steps read as equal changes.
PERCEPTUAL_GAMMA = 2.0

# Brightness 0–1 reads as off, and a swing down to off looks far bigger than
# one down to 2–4%. Off is saved for the most intense sections: below
# OFF_MIN_ACTIVITY the band never drops under VISIBLE_MIN.
VISIBLE_MIN = 2.0
OFF_MIN_ACTIVITY = 0.9


def band_for(activity: float) -> tuple[float, float]:
    """(bottom, top) levels a section with this activity (0–1) swings between."""
    top = 1.0 - 0.15 * (1 - activity)
    bottom = top * 0.85 * (1 - activity)
    if activity < OFF_MIN_ACTIVITY:
        bottom = max(bottom, (VISIBLE_MIN / 100) ** (1 / PERCEPTUAL_GAMMA))
    return bottom, top


def to_brightness(level: float) -> float:
    return 100.0 * max(0.0, min(1.0, level)) ** PERCEPTUAL_GAMMA


ATMOSPHERIC_DB = {
    # Each pulse tuple: (strength, attack, hold, release)
    # strength: where the beat peaks inside the section's band (0 = bottom, 1 = top)
    # attack/hold/release: fractions of beat_duration; must sum to ≤ 1
    #
    # The band already carries the energy contrast, so profiles only shape
    # rhythm: calm levels breathe with long ramps, active levels hit hard on
    # the beat and fall back fast so the dark between hits stays visible.
    0: [  # Very calm — slow breathing
        {
            "pulses": [
                (1.00, 0.45, 0.10, 0.45),
                (0.55, 0.45, 0.05, 0.50),
                (0.85, 0.45, 0.10, 0.45),
                (0.55, 0.45, 0.05, 0.50),
            ],
        },
        {
            "pulses": [
                (1.00, 0.50, 0.15, 0.35),
                (0.60, 0.45, 0.05, 0.50),
                (0.80, 0.45, 0.10, 0.45),
                (0.50, 0.45, 0.05, 0.50),
                (0.90, 0.50, 0.10, 0.40),
                (0.60, 0.45, 0.05, 0.50),
                (0.75, 0.45, 0.10, 0.45),
                (0.50, 0.45, 0.05, 0.50),
            ],
        },
    ],
    1: [  # Calm — gentle pulses
        {
            "pulses": [
                (1.00, 0.30, 0.10, 0.55),
                (0.50, 0.30, 0.05, 0.60),
                (0.80, 0.30, 0.10, 0.55),
                (0.50, 0.30, 0.05, 0.60),
            ],
        },
        {
            "pulses": [
                (1.00, 0.25, 0.15, 0.55),
                (0.40, 0.30, 0.05, 0.60),
                (0.70, 0.25, 0.10, 0.60),
                (0.40, 0.30, 0.05, 0.60),
                (0.90, 0.25, 0.15, 0.55),
                (0.40, 0.30, 0.05, 0.60),
                (0.70, 0.25, 0.10, 0.60),
                (0.55, 0.30, 0.05, 0.60),
            ],
        },
    ],
    2: [  # Medium — clear beats, softer offbeats
        {
            "pulses": [
                (1.00, 0.15, 0.10, 0.60),
                (0.55, 0.15, 0.05, 0.60),
                (0.85, 0.15, 0.10, 0.60),
                (0.55, 0.15, 0.05, 0.60),
            ],
        },
        {
            "pulses": [
                (1.00, 0.10, 0.15, 0.60),
                (0.35, 0.10, 0.05, 0.55),
                (0.75, 0.10, 0.10, 0.60),
                (0.35, 0.10, 0.05, 0.55),
                (1.00, 0.10, 0.15, 0.60),
                (0.35, 0.10, 0.05, 0.55),
                (0.75, 0.10, 0.10, 0.60),
                (0.60, 0.25, 0.05, 0.60),
            ],
        },
    ],
    3: [  # Energetic — sharp hits, quick falloff
        {
            "pulses": [
                (1.00, 0.06, 0.12, 0.55),
                (0.65, 0.06, 0.08, 0.55),
                (0.90, 0.06, 0.12, 0.55),
                (0.65, 0.06, 0.08, 0.55),
            ],
        },
        {
            "pulses": [
                (1.00, 0.04, 0.15, 0.50),
                (0.30, 0.04, 0.00, 0.40),
                (0.85, 0.04, 0.12, 0.50),
                (0.30, 0.04, 0.00, 0.40),
                (1.00, 0.04, 0.15, 0.50),
                (0.30, 0.04, 0.00, 0.40),
                (0.85, 0.04, 0.12, 0.50),
                (0.60, 0.04, 0.10, 0.50),
            ],
        },
    ],
    4: [  # Peak — full-swing flashes out of the dark
        {
            "pulses": [
                (1.00, 0.03, 0.12, 0.50),
                (0.80, 0.03, 0.10, 0.50),
                (1.00, 0.03, 0.12, 0.50),
                (0.80, 0.03, 0.10, 0.50),
            ],
        },
        {
            "pulses": [
                (1.00, 0.02, 0.18, 0.45),
                (0.55, 0.02, 0.05, 0.35),
                (1.00, 0.02, 0.15, 0.45),
                (0.55, 0.02, 0.05, 0.35),
                (1.00, 0.02, 0.18, 0.45),
                (0.55, 0.02, 0.05, 0.35),
                (1.00, 0.02, 0.15, 0.45),
                (1.00, 0.02, 0.30, 0.50),
            ],
        },
    ],
}


def profile_for(energy: float, section_beats: int) -> tuple[int, dict]:
    """Pick the profile whose pulse cycle fits the section's beat count best.

    Fewest leftover beats at the section end wins; ties go to the longer cycle.
    """
    level = max(0, min(4, round(energy * 4)))
    profiles = ATMOSPHERIC_DB[level]
    index = min(
        range(len(profiles)),
        key=lambda i: (section_beats % len(profiles[i]["pulses"]), -len(profiles[i]["pulses"])),
    )
    return index, profiles[index]


def pulses_for(
    profile: dict, accent_beats: int | None, subtle_beats: int | None
) -> list[tuple]:
    if accent_beats is None and subtle_beats is None:
        return profile["pulses"]
    sharp = (1.0, 0.05, 0.10, 0.55)
    subtle = (0.25, 0.05, 0, 0.35)
    return [sharp] * (accent_beats or 2) + [subtle] * (
        subtle_beats if subtle_beats is not None else 2
    )


def _check():
    assert set(ATMOSPHERIC_DB) == set(range(5))
    for profiles in ATMOSPHERIC_DB.values():
        for profile in profiles:
            assert profile["pulses"]
            for strength, attack, hold, release in profile["pulses"]:
                assert 0 <= strength <= 1
                assert all(0 <= part <= 1 for part in (attack, hold, release))
                assert attack + hold + release <= 1
    assert profile_for(0.0, 16) == (1, ATMOSPHERIC_DB[0][1])
    assert profile_for(1.0, 4) == (0, ATMOSPHERIC_DB[4][0])
    assert profile_for(1.0, 24) == (1, ATMOSPHERIC_DB[4][1])
    assert band_for(1.0) == (0.0, 1.0)
    assert to_brightness(band_for(0.75)[0]) >= VISIBLE_MIN
    assert all(
        band_for(a / 4)[1] - band_for(a / 4)[0] < band_for((a + 1) / 4)[1] - band_for((a + 1) / 4)[0]
        for a in range(4)
    )
    assert (
        pulses_for(ATMOSPHERIC_DB[0][0], None, None) is ATMOSPHERIC_DB[0][0]["pulses"]
    )
    assert [pulse[0] for pulse in pulses_for(ATMOSPHERIC_DB[0][0], 1, 2)] == [
        1.0,
        0.25,
        0.25,
    ]


if __name__ == "__main__":
    _check()
    print("atmospheric profiles: ok")
