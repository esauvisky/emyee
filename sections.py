"""Bar-aligned structural sections derived from bars and segment features."""

import bisect

import numpy as np

# A boundary has to cut this much squared error out of the piecewise-constant
# fit below. In its units (3 dB of loudness, or the track's whole timbre
# spread scaled by TIMBRE_WEIGHT) two 8-bar parts split once they sit about
# 5 dB apart with the same timbre and chords, sooner when those move too.
BOUNDARY_PENALTY = 11.0
# The fit places a boundary where the parts' averages differ most, which can
# sit a bar off the bar the sound actually jumps on; each boundary then moves
# to the bar with the biggest bar-to-bar change within SNAP_BARS of it. Parts
# don't follow a fixed 4-bar grid: Spotify's bars drift by a beat or two
# mid-track, so a grid counted in its bars pulled boundaries off the change.
SNAP_BARS = 1
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
# Spotify's bar grid is sometimes a beat or two off the music's real
# downbeat: on many techno tracks new parts, drops and breakdowns all land on
# beat 2 of its bars. It also slips: a bar of 1 or 2 beats mid-track moves
# every bar after it, though its beats stay perfectly steady. New sounds enter
# on the downbeat, so the beat of the bar that the music's changes peak on is
# the real beat 1. A change is a local peak (within 2 beats) of the distance
# between the mean sound of the DOWNBEAT_SPANS beats after a beat and before
# it; each peak counts by its height above the median. The grid the changes
# gather on more sharply wins: Spotify's bars, or a steady grid laid every
# per_bar beats over its beats. Beat 1 then moves only when one beat of the
# bar collects at least DOWNBEAT_MIN_SHARE of the change and DOWNBEAT_MIN_LEAD
# times what the grid's beat 1 collects; otherwise no beat stands out.
DOWNBEAT_SPANS = (4, 8, 16)
DOWNBEAT_MIN_SHARE = 0.35
DOWNBEAT_MIN_LEAD = 1.5


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


def beat_features(analysis: dict, beats: list[dict]) -> np.ndarray:
    """One row per beat: loudness, timbre and pitch-class strengths of the
    segments sounding in it, weighted by overlap, each column standardised."""
    segments = analysis["segments"]
    segment_starts = [s["start"] for s in segments]
    rows = []
    for beat in beats:
        start, end = beat["start"], beat["start"] + beat["duration"]
        first = max(0, bisect.bisect_right(segment_starts, start) - 1)
        sounding = [
            (s, min(s["start"] + s["duration"], end) - max(s["start"], start))
            for s in segments[first:bisect.bisect_left(segment_starts, end)]
        ]
        sounding, overlap = zip(*[(s, o) for s, o in sounding if o > 0])
        rows.append(np.concatenate((
            [np.average([s["loudness_max"] for s in sounding], weights=overlap)],
            np.average([s["timbre"] for s in sounding], axis=0, weights=overlap),
            np.average([s["pitches"] for s in sounding], axis=0, weights=overlap),
        )))
    features = np.array(rows)
    return (features - features.mean(axis=0)) / features.std(axis=0)


def change_peaks(features: np.ndarray) -> list[list[tuple[int, float]]]:
    """For each of DOWNBEAT_SPANS: the beats where the music changes, as
    (beat index, weight). A change is a local peak (within 2 beats) of the
    distance between the mean sound of the span after a beat and before it,
    weighted by its height above the median."""
    count = len(features)
    peaks = []
    for span in DOWNBEAT_SPANS:
        novelty = np.zeros(count)
        for index in range(span, count - span):
            novelty[index] = np.linalg.norm(features[index:index + span].mean(axis=0) - features[index - span:index].mean(axis=0))
        median = np.median(novelty[span:count - span])
        peaks.append([
            (index, novelty[index] - median) for index in range(span + 2, count - span - 2)
            if novelty[index] == novelty[index - 2:index + 3].max() and novelty[index] > median
        ])
    return peaks


def change_shares(peaks: list[list[tuple[int, float]]], positions: np.ndarray, per_bar: int) -> np.ndarray:
    """Share of the music's changes on each beat of the bar, given each beat's
    position in its bar, averaged over the spans."""
    shares = []
    for span_peaks in peaks:
        score = np.zeros(per_bar)
        for index, weight in span_peaks:
            score[positions[index]] += weight
        shares.append(score / score.sum())
    return np.mean(shares, axis=0)


def bar_grids(analysis: dict, bars: list[dict], beats: list[dict]) -> dict:
    """Beats per bar, Spotify's first bar's beat, how many of Spotify's bars
    hold another beat count (slips), and the share of the music's changes on
    each beat of Spotify's bars and of a steady grid of per_bar beats laid
    from Spotify's first bar (index 0 is beat 1 of each)."""
    beat_starts = [beat["start"] for beat in beats]
    bar_beats = [bisect.bisect_left(beat_starts, bar["start"] - 1e-3) for bar in bars]
    per_bar = int(np.median(np.diff(bar_beats)))
    first = bar_beats[0]
    # Beats before Spotify's first bar count back from it.
    containing = np.maximum(np.searchsorted(bar_beats, np.arange(len(beats)), side="right") - 1, 0)
    spotify_positions = (np.arange(len(beats)) - np.array(bar_beats)[containing]) % per_bar
    steady_positions = (np.arange(len(beats)) - first) % per_bar
    peaks = change_peaks(beat_features(analysis, beats))
    return {
        "per_bar": per_bar,
        "first_beat": first,
        "slips": int(np.sum(np.diff(bar_beats) != per_bar)),
        "spotify": change_shares(peaks, spotify_positions, per_bar),
        "steady": change_shares(peaks, steady_positions, per_bar),
    }


def downbeat_shift(shares: np.ndarray) -> int:
    """Beats to move beat 1 later by, 0 when no beat stands out."""
    best = int(np.argmax(shares))
    if best and shares[best] >= DOWNBEAT_MIN_SHARE and shares[best] >= DOWNBEAT_MIN_LEAD * shares[0]:
        return best
    return 0


def shift_bars(bars: list[dict], beats: list[dict], shift: int) -> list[dict]:
    """Bars starting `shift` beats later than Spotify's. A bar whose shifted
    start runs past the last beat is dropped."""
    beat_starts = [beat["start"] for beat in beats]
    starts = []
    for bar in bars:
        index = bisect.bisect_left(beat_starts, bar["start"] - 1e-3) + shift
        if index < len(beat_starts):
            starts.append((beat_starts[index], bar))
    shifted = []
    for position, (start, bar) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else start + bar["duration"]
        shifted.append({**bar, "start": start, "duration": end - start})
    return shifted


def steady_bars(bars: list[dict], beats: list[dict], per_bar: int, first_beat: int) -> list[dict]:
    """Bars of exactly per_bar beats from beat `first_beat` on, ignoring
    Spotify's slips. Each keeps the confidence of the Spotify bar it starts in;
    the last ends where the last beat does."""
    beat_starts = [beat["start"] for beat in beats]
    spotify_starts = [bar["start"] for bar in bars]
    steady = []
    for index in range(first_beat, len(beats), per_bar):
        end = beats[index + per_bar]["start"] if index + per_bar < len(beats) else beats[-1]["start"] + beats[-1]["duration"]
        spotify = bars[max(0, bisect.bisect_right(spotify_starts, beat_starts[index] + 1e-3) - 1)]
        steady.append({"start": beat_starts[index], "duration": end - beat_starts[index], "confidence": spotify["confidence"]})
    return steady


def part_starts(features: np.ndarray) -> list[int]:
    """Bar indexes where parts start (bar 0 excluded): the split of the track
    into runs of bars that minimises each run's spread around its own mean,
    plus BOUNDARY_PENALTY per part, each then snapped to the bar the sound
    jumps on (snap_to_changes).

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
        cost = best[starts] + spread + BOUNDARY_PENALTY
        choice = int(np.argmin(cost))
        best[end] = cost[choice]
        previous[end] = starts[choice]
    cuts = []
    end = count
    while previous[end] > 0:
        end = int(previous[end])
        cuts.append(end)
    return cuts[::-1]


def texture_steps(features: np.ndarray) -> np.ndarray:
    """Per bar, how far its loudness and timbre moved from the bar before.
    Chords are left out: in a chord loop they change on almost every bar."""
    return np.r_[0.0, np.linalg.norm(np.diff(features[:, :13], axis=0), axis=1)]


def snap_to_changes(features: np.ndarray, cuts: list[int]) -> list[int]:
    """Move each boundary to the bar with the biggest bar-to-bar change within
    SNAP_BARS of it. A boundary that would land under MIN_PART_BARS after the
    one before it is dropped."""
    step = np.r_[0.0, np.linalg.norm(np.diff(features, axis=0), axis=1)]
    snapped = []
    for cut in cuts:
        window = range(max(1, cut - SNAP_BARS), min(len(features) - 1, cut + SNAP_BARS) + 1)
        bar = max(window, key=lambda index: step[index])
        if not snapped or bar - snapped[-1] >= MIN_PART_BARS:
            snapped.append(bar)
    return snapped


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
        cuts += snap_to_changes(features, part_starts(features))
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
    # 8 quiet bars, a 3-bar build, then 13 loud bars: the drop starts on bar
    # 11, where the sound jumps, though it is off the 4-bar grid.
    def level(bar):
        return -20 if bar < 8 else -12 if bar < 11 else -5

    bars = [{"start": float(i), "duration": 1.0} for i in range(24)]
    segments = [
        {"start": i / 2, "duration": 0.5, "loudness_max": level(i // 2), "timbre": [level(i // 2) * (k + 1.0) for k in range(12)], "pitches": [1.0] * 12}
        for i in range(48)
    ]
    sections = detect_sections({"track": {"duration": 24.0}, "segments": segments}, bars)
    assert [section["start"] for section in sections] == [0.0, 8.0, 11.0], [section["start"] for section in sections]
    assert sum(section["duration"] for section in sections) == 24.0
    assert sections[2]["drop"] and not sections[0]["drop"], [section["drop_why"] for section in sections]
    short = detect_sections({"track": {"duration": 3.0}, "segments": segments[:6]}, bars[:3])
    assert [section["start"] for section in short] == [0.0]
    assert not short[0]["drop"]
    jump = np.array([[0.0]] * 11 + [[5.0]] * 13)
    assert snap_to_changes(jump, [12]) == [11]
    assert snap_to_changes(jump, [10]) == [11]
    assert snap_to_changes(jump, [11, 12]) == [11]
    assert drop_verdict(3.3, 1.4, 8)[0]
    assert not drop_verdict(3.3, 1.4, 3)[0]
    assert not drop_verdict(12.8, 6.1, 8)[0]
    assert not drop_verdict(2.0, 1.0, 8)[0]


if __name__ == "__main__":
    _check()
    print("custom sections: ok")
