"""Editable profiles for atmospheric beat pulses."""

ATMOSPHERIC_DB = {
    0: [
        {"accent_beats": 1, "subtle_beats": 3, "subtle_scale": 0.15, "downbeat_boost": 1.05},
        {"accent_beats": 2, "subtle_beats": 2, "subtle_scale": 0.15, "downbeat_boost": 1.05},
    ],
    1: [
        {"accent_beats": 2, "subtle_beats": 2, "subtle_scale": 0.20, "downbeat_boost": 1.10},
        {"accent_beats": 1, "subtle_beats": 1, "subtle_scale": 0.20, "downbeat_boost": 1.10},
    ],
    2: [
        {"accent_beats": 2, "subtle_beats": 2, "subtle_scale": 0.25, "downbeat_boost": 1.15},
        {"accent_beats": 3, "subtle_beats": 1, "subtle_scale": 0.25, "downbeat_boost": 1.15},
    ],
    3: [
        {"accent_beats": 3, "subtle_beats": 1, "subtle_scale": 0.30, "downbeat_boost": 1.20},
        {"accent_beats": 1, "subtle_beats": 1, "subtle_scale": 0.35, "downbeat_boost": 1.20},
    ],
    4: [
        {"accent_beats": 4, "subtle_beats": 0, "subtle_scale": 0.35, "downbeat_boost": 1.25},
        {"accent_beats": 3, "subtle_beats": 1, "subtle_scale": 0.40, "downbeat_boost": 1.25},
    ],
}


def profile_for(energy: float, bar_index: int) -> tuple[int, dict]:
    level = max(0, min(4, round(energy * 4)))
    profiles = ATMOSPHERIC_DB[level]
    index = (bar_index // 4) % len(profiles)
    return index, profiles[index]


def _check():
    assert set(ATMOSPHERIC_DB) == set(range(5))
    for profiles in ATMOSPHERIC_DB.values():
        for profile in profiles:
            assert profile["accent_beats"] > 0
            assert profile["subtle_beats"] >= 0
            assert 0 <= profile["subtle_scale"] <= 1
            assert profile["downbeat_boost"] >= 1
    assert profile_for(0.0, 0) == (0, ATMOSPHERIC_DB[0][0])
    assert profile_for(1.0, 4) == (1, ATMOSPHERIC_DB[4][1])


if __name__ == "__main__":
    _check()
    print("atmospheric profiles: ok")
