"""Editable profiles for atmospheric beat pulses."""

ATMOSPHERIC_DB = {
    0: [
        {
            "pulses": [
                (0.20, 0.80, 0, 0.20),
                (0.35, 0.75, 0, 0.25),
                (0.50, 0.70, 0, 0.30),
                (0.70, 0.65, 0, 0.35),
                (1.00, 0.55, 0.10, 0.35),
                (0.70, 0.45, 0.10, 0.45),
                (0.50, 0.40, 0.10, 0.50),
                (0.35, 0.35, 0.10, 0.55),
            ],
            "downbeat_boost": 1.05,
        },
        {
            "pulses": [
                (0.60, 0.50, 0.10, 0.40),
                (0.10, 0.15, 0.10, 0.50),
                (0.35, 0.60, 0, 0.40),
                (0.10, 0.15, 0.10, 0.50),
            ],
            "downbeat_boost": 1.05,
        },
    ],
    1: [
        {
            "pulses": [
                (0.30, 0.65, 0, 0.35),
                (0.50, 0.60, 0, 0.40),
                (0.70, 0.50, 0.05, 0.45),
                (1.00, 0.40, 0.10, 0.50),
                (0.70, 0.35, 0.10, 0.55),
                (0.50, 0.30, 0.10, 0.55),
                (0.30, 0.25, 0.10, 0.55),
                (0.20, 0.20, 0.10, 0.55),
            ],
            "downbeat_boost": 1.10,
        },
        {
            "pulses": [
                (0.70, 0.12, 0.13, 0.55),
                (0.20, 0.08, 0.08, 0.45),
                (0.90, 0.12, 0.18, 0.55),
                (0.20, 0.08, 0.08, 0.45),
            ],
            "downbeat_boost": 1.10,
        },
    ],
    2: [
        {
            "pulses": [
                (0.40, 0.60, 0, 0.40),
                (0.60, 0.50, 0.05, 0.45),
                (0.80, 0.40, 0.10, 0.50),
                (1.00, 0.25, 0.15, 0.55),
            ],
            "downbeat_boost": 1.15,
        },
        {
            "pulses": [
                (1.00, 0.08, 0.12, 0.60),
                (0.20, 0.05, 0.05, 0.40),
                (0.40, 0.35, 0.05, 0.50),
                (0.80, 0.15, 0.15, 0.60),
            ],
            "downbeat_boost": 1.15,
        },
    ],
    3: [
        {
            "pulses": [
                (0.50, 0.45, 0, 0.50),
                (0.65, 0.35, 0.05, 0.55),
                (0.80, 0.25, 0.10, 0.60),
                (1.00, 0.10, 0.15, 0.65),
            ],
            "downbeat_boost": 1.20,
        },
        {
            "pulses": [
                (1.00, 0.05, 0.10, 0.55),
                (0.20, 0.05, 0, 0.35),
                (0.80, 0.08, 0.12, 0.55),
                (0.20, 0.05, 0, 0.35),
                (1.00, 0.05, 0.15, 0.60),
                (0.20, 0.05, 0, 0.35),
                (0.60, 0.20, 0.05, 0.50),
                (0.20, 0.05, 0, 0.35),
            ],
            "downbeat_boost": 1.20,
        },
    ],
    4: [
        {
            "pulses": [
                (0.60, 0.35, 0, 0.55),
                (0.75, 0.25, 0.05, 0.60),
                (0.90, 0.15, 0.10, 0.65),
                (1.00, 0.05, 0.15, 0.65),
            ],
            "downbeat_boost": 1.25,
        },
        {
            "pulses": [
                (1.00, 0.03, 0.12, 0.50),
                (0.20, 0.03, 0, 0.30),
                (0.80, 0.08, 0.10, 0.50),
                (0.20, 0.03, 0, 0.30),
                (1.00, 0.03, 0.17, 0.55),
                (0.20, 0.03, 0, 0.30),
                (1.00, 0.20, 0.20, 0.55),
                (0.40, 0.05, 0.05, 0.40),
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
