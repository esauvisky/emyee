"""Beat-aligned structural sections derived from bars and segment features."""

import numpy as np


def detect_sections(analysis: dict, bars: list[dict], phrase_bars: int = 4) -> list[dict]:
    segments = analysis.get("segments", [])
    duration = analysis.get("track", {}).get("duration", 0.0)
    if len(bars) < phrase_bars * 2 or not segments:
        return [{"start": 0.0, "duration": duration, "confidence": 1.0, "loudness": -20.0}]

    candidates = []
    for bar_index in range(phrase_bars, len(bars), phrase_bars):
        start = bars[bar_index]["start"]
        left_start = bars[bar_index - phrase_bars]["start"]
        right_end = bars[min(len(bars) - 1, bar_index + phrase_bars)]["start"]
        left = [s for s in segments if left_start <= s.get("start", 0) < start]
        right = [s for s in segments if start <= s.get("start", 0) < right_end]
        if not left or not right:
            continue
        loudness = min(1.0, abs(
            np.mean([s.get("loudness_max", -20) for s in left])
            - np.mean([s.get("loudness_max", -20) for s in right])
        ) / 12)
        left_timbre = np.mean([s.get("timbre", [0] * 12) for s in left], axis=0)
        right_timbre = np.mean([s.get("timbre", [0] * 12) for s in right], axis=0)
        denominator = np.linalg.norm(left_timbre) * np.linalg.norm(right_timbre)
        timbre = 0.0 if not denominator else 1 - float(np.dot(left_timbre, right_timbre) / denominator)
        candidates.append((bar_index, 0.6 * loudness + 0.4 * timbre))

    if not candidates:
        return [{"start": 0.0, "duration": duration, "confidence": 1.0, "loudness": -20.0}]

    threshold = max(0.12, float(np.median([score for _, score in candidates])))
    boundaries = [0.0] + [
        bars[index]["start"] for position, (index, score) in enumerate(candidates)
        if score >= threshold
        and score >= (candidates[position - 1][1] if position else -1)
        and score >= (candidates[position + 1][1] if position + 1 < len(candidates) else -1)
    ]
    boundaries.append(duration)

    sections = []
    for start, end in zip(boundaries, boundaries[1:]):
        section_segments = [s for s in segments if start <= s.get("start", 0) < end]
        loudness = float(np.mean([s.get("loudness_max", -20) for s in section_segments])) if section_segments else -20.0
        sections.append({
            "start": start,
            "duration": end - start,
            "confidence": 1.0,
            "loudness": loudness,
        })
    return sections


def _check():
    bars = [{"start": float(i)} for i in range(12)]
    segments = [
        {"start": i / 2, "loudness_max": -20 if i < 8 else -5, "timbre": [1] * 12}
        for i in range(24)
    ]
    sections = detect_sections({"track": {"duration": 12.0}, "segments": segments}, bars)
    assert sections[0]["start"] == 0.0
    assert all(section["start"] in {bar["start"] for bar in bars} for section in sections)
    assert sum(section["duration"] for section in sections) == 12.0


if __name__ == "__main__":
    _check()
    print("custom sections: ok")
