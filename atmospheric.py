"""Editable profiles for atmospheric beat pulses."""

ATMOSPHERIC_DB = {
    # Each pulse tuple: (strength, attack, hold, release)
    # strength: multiplier on the beat's brightness spike (0–1)
    # attack/hold/release: fractions of beat_duration; must sum to ≤ 1
    #
    # Design intent:
    #   Low energy  → slow sweeping rises/falls, high strength (long, dramatic pulses)
    #   High energy → short snappy flashes, lower strength (rapid, tight hits)
    0: [  # Very calm — wide, sweeping pulses
        {
            "pulses": [
                (0.75, 0.88, 0.07, 0.05),
                (0.55, 0.80, 0.05, 0.10),
                (0.85, 0.90, 0.05, 0.05),
                (0.60, 0.78, 0.08, 0.09),
                (0.80, 0.85, 0.10, 0.05),
                (0.50, 0.75, 0.05, 0.15),
                (0.70, 0.88, 0.07, 0.05),
                (0.65, 0.82, 0.08, 0.05),
            ],
            "downbeat_boost": 1.05,
        },
        {
            "pulses": [
                (0.80, 0.88, 0.07, 0.05),
                (0.45, 0.72, 0.05, 0.13),
                (0.78, 0.85, 0.10, 0.05),
                (0.45, 0.72, 0.05, 0.13),
            ],
            "downbeat_boost": 1.05,
        },
    ],
    1: [  # Calm — slow, gentle pulses
        {
            "pulses": [
                (0.55, 0.65, 0.10, 0.20),
                (0.72, 0.60, 0.10, 0.25),
                (0.88, 0.55, 0.10, 0.30),
                (0.68, 0.60, 0.10, 0.25),
                (0.58, 0.65, 0.05, 0.25),
                (0.78, 0.60, 0.08, 0.27),
                (0.48, 0.55, 0.10, 0.30),
                (0.62, 0.60, 0.05, 0.25),
            ],
            "downbeat_boost": 1.10,
        },
        {
            "pulses": [
                (0.82, 0.55, 0.10, 0.30),
                (0.28, 0.40, 0.05, 0.30),
                (0.68, 0.60, 0.08, 0.27),
                (0.28, 0.40, 0.05, 0.30),
            ],
            "downbeat_boost": 1.10,
        },
    ],
    2: [  # Medium — balanced pulses
        {
            "pulses": [
                (0.58, 0.45, 0.05, 0.40),
                (0.72, 0.40, 0.10, 0.45),
                (0.88, 0.35, 0.10, 0.50),
                (0.68, 0.40, 0.05, 0.45),
            ],
            "downbeat_boost": 1.15,
        },
        {
            "pulses": [
                (0.92, 0.30, 0.10, 0.50),
                (0.32, 0.20, 0.05, 0.40),
                (0.58, 0.35, 0.05, 0.50),
                (0.82, 0.25, 0.10, 0.55),
            ],
            "downbeat_boost": 1.15,
        },
    ],
    3: [  # Energetic — faster, smaller pulses
        {
            "pulses": [
                (0.48, 0.25, 0.05, 0.55),
                (0.62, 0.20, 0.10, 0.60),
                (0.78, 0.15, 0.10, 0.65),
                (0.52, 0.20, 0.05, 0.60),
            ],
            "downbeat_boost": 1.20,
        },
        {
            "pulses": [
                (0.82, 0.08, 0.12, 0.55),
                (0.22, 0.05, 0.05, 0.40),
                (0.72, 0.10, 0.10, 0.55),
                (0.22, 0.05, 0, 0.40),
                (0.82, 0.08, 0.12, 0.55),
                (0.22, 0.05, 0.05, 0.40),
                (0.58, 0.20, 0.05, 0.55),
                (0.22, 0.05, 0, 0.40),
            ],
            "downbeat_boost": 1.20,
        },
    ],
    4: [  # Very energetic — snappy, tight, small pulses
        {
            "pulses": [
                (0.38, 0.10, 0.05, 0.65),
                (0.52, 0.08, 0.07, 0.70),
                (0.42, 0.10, 0.05, 0.65),
                (0.58, 0.07, 0.08, 0.70),
            ],
            "downbeat_boost": 1.25,
        },
        {
            "pulses": [
                (0.62, 0.05, 0.10, 0.55),
                (0.22, 0.03, 0, 0.35),
                (0.52, 0.08, 0.10, 0.55),
                (0.22, 0.03, 0, 0.35),
                (0.62, 0.05, 0.12, 0.58),
                (0.22, 0.03, 0, 0.35),
                (0.52, 0.15, 0.15, 0.55),
                (0.28, 0.05, 0, 0.40),
            ],
            "downbeat_boost": 1.25,
        },
    ],
}


def profile_for(energy: float, bar_index: int) -> tuple[int, dict]:
    level = max(0, min(4, round(energy * 4)))
    profiles = ATMOSPHERIC_DB[level]
    index = (bar_index // 4) % len(profiles)
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
            assert profile["downbeat_boost"] >= 1
    assert profile_for(0.0, 0) == (0, ATMOSPHERIC_DB[0][0])
    assert profile_for(1.0, 4) == (1, ATMOSPHERIC_DB[4][1])
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
