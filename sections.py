"""Bar-aligned structural sections derived from bars and segment features."""

import bisect

import numpy as np

# A boundary has to cut this much squared error out of the piecewise-constant
# fit below. In its units (3 dB of loudness, or the track's whole timbre
# spread scaled by TIMBRE_WEIGHT) two 8-bar parts split once they sit about
# 4.5 dB apart with the same timbre and chords, sooner when those move too.
BOUNDARY_PENALTY = 9.0
# Extra cost of a part whose length is not a multiple of PHRASE_GRID_BARS.
# Fills and pickups sound like the part they lead into, so on sound alone a
# boundary tends to land a bar early; this pulls it onto the phrase. It stays
# under BOUNDARY_PENALTY so a real odd-length part (or a bar Spotify
# miscounted) still wins.
OFF_GRID_PENALTY = 6.0
PHRASE_GRID_BARS = 4
MIN_PART_BARS = 2
LOUDNESS_UNIT_DB = 3.0
TIMBRE_WEIGHT = 2.0
# Weight of the 12 pitch-class strengths (each 0..1). A chorus in a dense mix
# is as loud and as bright as the verse before it; the chords are what change.
CHROMA_WEIGHT = 3.0
# Digital silence reads -60 dB; left unclamped a silent bar would outweigh
# every musical difference in the track.
LOUDNESS_FLOOR_DB = -40.0
# A drop is a part whose first DROP_WINDOW_BARS sound far closer to the
# track's peak sound than the DROP_WINDOW_BARS before it: the build strips the
# mix down, the drop brings it all back at once. The peak sound is the mean
# loudness and timbre of the loudest DROP_PEAK_SHARE of bars; chords are left
# out, a drop changes the mix, not the harmony. In the feature units above,
# the distance has to at least halve (DROP_MAX_RATIO), shrink by DROP_MIN_FALL
# (about 4 dB at the same timbre) and end within DROP_MAX_ARRIVAL (about
# 9 dB), so a quiet intro getting less quiet is no drop.
DROP_WINDOW_BARS = 4
DROP_PEAK_SHARE = 0.25
DROP_MAX_RATIO = 0.5
DROP_MIN_FALL = 1.25
DROP_MAX_ARRIVAL = 3.0
DROP_MIN_BARS = 4


def bar_features(analysis: dict, bars: list[dict]) -> np.ndarray:
    """One row per bar: loudness, the 12 timbre coefficients and the 12
    pitch-class strengths, averaged over the segments sounding in the bar
    weighted by their overlap with it, scaled so a unit step in any column is
    a comparable change."""
    segments = analysis["segments"]
    segment_starts = [s["start"] for s in segments]
    rows = []
    for bar in bars:
        start, end = bar["start"], bar["start"] + bar["duration"]
        first = max(0, bisect.bisect_right(segment_starts, start) - 1)
        sounding = segments[first:bisect.bisect_left(segment_starts, end)]
        overlap = [min(s["start"] + s["duration"], end) - max(s["start"], start) for s in sounding]
        loudness = np.average([s["loudness_max"] for s in sounding], weights=overlap)
        timbre = np.average([s["timbre"] for s in sounding], axis=0, weights=overlap)
        chroma = np.average([s["pitches"] for s in sounding], axis=0, weights=overlap)
        rows.append(np.concatenate(([max(loudness, LOUDNESS_FLOOR_DB) / LOUDNESS_UNIT_DB], timbre, chroma * CHROMA_WEIGHT)))
    features = np.array(rows)
    timbre = features[:, 1:13]
    features[:, 1:13] = (timbre - timbre.mean(axis=0)) / timbre.std(axis=0) * (TIMBRE_WEIGHT / np.sqrt(timbre.shape[1]))
    return features


def part_starts(features: np.ndarray) -> list[int]:
    """Bar indexes where parts start (bar 0 excluded): the split of the track
    into runs of bars that minimises each run's spread around its own mean,
    plus BOUNDARY_PENALTY per part and OFF_GRID_PENALTY per off-grid length.

    Solved exactly by dynamic programming over the end bar of the last part,
    so a boundary sits on the bar the sound changes on instead of wherever a
    smoothed novelty curve happens to peak.
    """
    count = len(features)
    sums = np.vstack([np.zeros(features.shape[1]), np.cumsum(features, axis=0)])
    squares = np.concatenate([[0.0], np.cumsum((features ** 2).sum(axis=1))])
    best = np.full(count + 1, np.inf)
    best[0] = 0.0
    previous = np.zeros(count + 1, dtype=int)
    for end in range(MIN_PART_BARS, count + 1):
        starts = np.arange(0, end - MIN_PART_BARS + 1)
        lengths = end - starts
        total = sums[end] - sums[starts]
        spread = squares[end] - squares[starts] - (total ** 2).sum(axis=1) / lengths
        # The last part ends where the track does, not on a phrase.
        off_grid = OFF_GRID_PENALTY * ((lengths % PHRASE_GRID_BARS != 0) & (end < count))
        cost = best[starts] + spread + BOUNDARY_PENALTY + off_grid
        choice = int(np.argmin(cost))
        best[end] = cost[choice]
        previous[end] = starts[choice]
    cuts = []
    end = count
    while previous[end] > 0:
        end = int(previous[end])
        cuts.append(end)
    return cuts[::-1]


def peak_approaches(features: np.ndarray, cuts: list[int]) -> list[tuple[float, float]]:
    """For each part after the first, its distance from the track's peak
    sound over the DROP_WINDOW_BARS before its first bar and over its own
    first DROP_WINDOW_BARS."""
    sound = features[:, :13]
    loudest = sound[:, 0] >= np.quantile(sound[:, 0], 1 - DROP_PEAK_SHARE)
    distance = np.linalg.norm(sound - sound[loudest].mean(axis=0), axis=1)
    ends = cuts[2:] + [len(features)]
    return [
        (float(distance[max(0, start - DROP_WINDOW_BARS):start].mean()),
         float(distance[start:min(end, start + DROP_WINDOW_BARS)].mean()))
        for start, end in zip(cuts[1:], ends)
    ]


def drop_verdict(before: float, after: float, bars: int) -> tuple[bool, str]:
    """Whether a part that moves from `before` to `after` away from the peak
    sound is a drop, and the numbers that decided it."""
    numbers = f"peak distance {before:.2f} → {after:.2f} (x{after / before:.2f})"
    if bars < DROP_MIN_BARS:
        return False, f"{numbers}, only {bars} bars (drops need {DROP_MIN_BARS})"
    if after > DROP_MAX_RATIO * before:
        return False, f"{numbers}, needs x{DROP_MAX_RATIO:.2f} or less"
    if before - after < DROP_MIN_FALL:
        return False, f"{numbers}, closer by only {before - after:.2f} (needs {DROP_MIN_FALL:.2f})"
    if after > DROP_MAX_ARRIVAL:
        return False, f"{numbers}, lands too far from the peak (needs {DROP_MAX_ARRIVAL:.2f} or less)"
    return True, numbers


def detect_sections(analysis: dict, bars: list[dict]) -> list[dict]:
    """Split the track into parts of uniform loudness, timbre and harmony,
    each starting on a bar, and mark the parts that are drops."""
    segments = analysis["segments"]
    duration = analysis["track"]["duration"]
    cuts = [0]
    approaches = []
    if len(bars) >= 2 * MIN_PART_BARS:
        features = bar_features(analysis, bars)
        cuts += part_starts(features)
        approaches = peak_approaches(features, cuts)
    boundaries = [0.0] + [bars[index]["start"] for index in cuts[1:]] + [duration]
    lengths = np.diff(cuts + [len(bars)])

    # The first part has nothing before it to drop from.
    verdicts = [(False, "first part")] + [
        drop_verdict(before, after, int(length)) for (before, after), length in zip(approaches, lengths[1:])
    ]
    sections = []
    for (start, end), (drop, why) in zip(zip(boundaries, boundaries[1:]), verdicts):
        section_segments = [s for s in segments if start <= s["start"] < end]
        sections.append({
            "start": start,
            "duration": end - start,
            "confidence": 1.0,
            "loudness": float(np.mean([s["loudness_max"] for s in section_segments])) if section_segments else -20.0,
            "drop": drop,
            "drop_why": why,
        })
    return sections


def _check():
    # 8 quiet bars, a 4-bar build whose last bar is a fill halfway to the
    # drop's sound, then 12 loud bars: the drop is bar 12, not the fill.
    def level(bar):
        return -20 if bar < 8 else -12 if bar < 11 else -8 if bar == 11 else -5

    bars = [{"start": float(i), "duration": 1.0} for i in range(24)]
    segments = [
        {"start": i / 2, "duration": 0.5, "loudness_max": level(i // 2), "timbre": [level(i // 2) * (k + 1.0) for k in range(12)], "pitches": [1.0] * 12}
        for i in range(48)
    ]
    sections = detect_sections({"track": {"duration": 24.0}, "segments": segments}, bars)
    assert [section["start"] for section in sections] == [0.0, 8.0, 12.0], [section["start"] for section in sections]
    assert sum(section["duration"] for section in sections) == 24.0
    assert sections[2]["drop"] and not sections[0]["drop"], [section["drop_why"] for section in sections]
    short = detect_sections({"track": {"duration": 3.0}, "segments": segments[:6]}, bars[:3])
    assert [section["start"] for section in short] == [0.0]
    assert not short[0]["drop"]
    assert drop_verdict(3.3, 1.4, 8)[0]
    assert not drop_verdict(3.3, 1.4, 3)[0]
    assert not drop_verdict(12.8, 6.1, 8)[0]
    assert not drop_verdict(2.0, 1.0, 8)[0]


if __name__ == "__main__":
    _check()
    print("custom sections: ok")
