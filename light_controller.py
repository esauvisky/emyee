import asyncio
import bisect
import json
import os
import time
from typing import List, Optional
from dotenv import load_dotenv
from loguru import logger
from models import EventSongChanged, EventAdjustProgressTime, EventStop
from utils import (
    get_next_item,
    CONTROLLER_TICK,
    MIN_ANALYSIS_CONFIDENCE,
    SEEK_THRESHOLD,
    get_vibrant_color,
    VIBRANT_COLOR_PALETTE,
    merge_short_segments,
    min_segment_duration_from_future_measures,
)
import random
import numpy as np

from atmospheric import VISIBLE_MIN, band_for, profile_for, pulses_for, to_brightness
from light_device import BaseLightDevice
from sections import detect_sections

# A bar whose loudest hit is this many dB under the median loudest hit of the
# QUIET_LOOKBACK_BARS before it is a breakdown: held at its resting level
# instead of pulsing. Judged on the loudest hit, not the bar's average: a
# bare kick with silence between the hits averages low but is no breakdown.
QUIET_BAR_DB = 5.0
QUIET_LOOKBACK_BARS = 8
# A bar whose segments rise under FLOW_MAX_ONSET_DB from onset to peak and
# whose beats Spotify is less than FLOW_MAX_BEAT_CONFIDENCE sure of has no
# transients to pulse on (a vocal over a pad): it flows instead. Runs
# shorter than FLOW_MIN_BARS do not flip the mode either way.
FLOW_MAX_ONSET_DB = 8.0
FLOW_MAX_BEAT_CONFIDENCE = 0.5
FLOW_MIN_BARS = 2
# Each flow bar drifts the hue to the nearest palette colour this far up the
# wheel over the whole bar. The palette's widest gap is 40 degrees, so there
# is always one in range.
FLOW_HUE_STEP_MIN = 20
FLOW_HUE_STEP_MAX = 45
# Sections at least this energetic (and STROBE_MIN_BARS long) strobe colour
# every eighth note on their loudest bar, brightness held at their peak.
STROBE_MIN_ENERGY = 0.75
STROBE_MIN_BARS = 4
# Detected parts are split into phrases counted from the part's first bar:
# PHRASE_BARS_CALM in parts below PHRASE_LOUD_ENERGY, PHRASE_BARS_LOUD in the
# rest. Each phrase gets its own energy and its own colour. A leftover shorter
# than PHRASE_MIN_BARS joins the phrase before it.
PHRASE_BARS_CALM = 4
PHRASE_BARS_LOUD = 8
PHRASE_LOUD_ENERGY = 0.5
PHRASE_MIN_BARS = 2
# A section shorter than this gets no colour of its own: no fade into it, and
# the fade into the next full section starts on its first bar (the first bar
# of a run of short sections) and spans it.
MIN_COLOR_SECTION_BARS = 4
# The light needs this long to visibly reach a pulse's peak, and commands
# closer together than MIN_COMMAND_GAP_S get merged or dropped by it. A
# pulse's release goes out at least that long after its attack and at least
# that long before the next beat's attack.
MIN_ATTACK_S = 0.03
MIN_COMMAND_GAP_S = 0.2
# Section colour changes step to a palette hue at most this far around the
# wheel, so the crossing never sweeps through unrelated colours on the way.
MAX_HUE_STEP = 90
# Bars over which the hue crosses in, ending on the section's first beat.
HUE_FADE_BARS = 1
# One log file per played track, everything down to TRACE.
SONG_LOG_DIR = "logs"
SONG_LOG_FORMAT = "{time:HH:mm:ss.SSS} | {level:1.1} | {message}"

class LightsController:
    def __init__(self, devices: List[BaseLightDevice], events_queue: asyncio.Queue):
        self.devices = devices
        self.events_queue = events_queue
        self._pending_event = None
        self.last_section_num_next = 0
        self.last_index = -1
        self.current_hue = random.randint(0, 359)
        self.current_saturation = random.randint(50, 80)
        self.current_brightness = 0
        self.sections = []           # phrases: the unit for energy, pulses and colour
        self._parts: list = []       # structural parts from detect_sections
        self._part_energies: list = []
        self.last_next_bar = {}
        self.last_next_segment = None
        self.current_section = None
        self.current_section_index = -1
        self.analysis = None
        self.current_progress = 0
        self._last_progress: float = 0.0          # song time of the previous progress tick
        self._last_progress_mono: float = time.monotonic()
        self.section_color_indexes = set()
        self.min_segment_duration = 0.1
        self.beats_per_measure = 4
        self.avg_measure_duration = 0.5
        self._loudness_min = 0.0
        self._loudness_max = 1.0
        self._segment_starts: list = []
        self._section_energies: list = []
        self._quiet_bars: set[int] = set()
        self._bar_loudness: list[float] = []
        self._strobe_bars: set[int] = set()
        self._flow_bars: set[int] = set()
        self._flow_scheduled_for: int = -1       # bar index of the scheduled flow command
        self._short_sections: set[int] = set()   # sections the next colour fades across
        self._strobe_task: Optional[asyncio.Task] = None
        self._strobe_scheduled_for: int = -1     # bar index of the scheduled strobe
        self._section_timbres: list = []
        self._beat_starts: list = []
        self.current_song: str = ""
        self._song_log: int | None = None        # loguru sink id of the current track's file
        self._tatum_starts: list = []
        self._fade_in_end: float = 0.0
        self._fade_out_start: float = float('inf')
        self._track_duration: float = 0.0
        self._section_bar_starts: list = []
        self._section_first_beat: list = []
        self._last_segment_idx = -1
        # Pulses run to completion side by side: the next beat is scheduled
        # while the previous pulse's release is still pending.
        self._beat_tasks: set[asyncio.Task] = set()
        self._beat_scheduled_for: int = -1
        self._pre_section_trans_for: int = -1   # section index we've pre-transitioned for

        # Tunable — override from main.py after construction
        self.max_brightness: int = 100   # ceiling in %; all computed brightness values are scaled to this
        self.min_confidence: float = MIN_ANALYSIS_CONFIDENCE
        self.sync_offset: float = 0.0    # extra seconds to fire commands early (+) or late (-)
        self.atmospheric_pulse_on: int | None = None
        self.atmospheric_pulse_off: int | None = None

        # Panel animation: interpolate displayed color over the transition duration
        self._trans_start_hue: float = float(self.current_hue)
        self._trans_start_sat: float = float(self.current_saturation)
        self._trans_start_bri: float = float(self.current_brightness)
        self._trans_start_time: float = 0.0
        self._trans_duration: float = 0.0

        # Color-only transition state keeps panel and command interpolation aligned.
        self._color_trans_start_hue: float = float(self.current_hue)
        self._color_trans_start_sat: float = float(self.current_saturation)
        self._color_trans_start_time: float = 0.0
        self._color_trans_duration: float = 0.0
        self._sent_color: tuple[int, int] | None = None   # last hue/sat sent to the devices
        self._last_timing_log_second = -1

        self._current_params = {
            'hue': self.current_hue,
            'saturation': self.current_saturation,
            'brightness': self.current_brightness
        }

    async def control_lights(self):
        event = EventStop()
        while True:
            event = self._pending_event or await self.events_queue.get()
            self._pending_event = None
            if isinstance(event, EventSongChanged):
                logger.debug("Loading analysis for {}", event.track_id or event.song_name or 'unknown track')
                self.handle_song_changed(event)
            elif isinstance(event, EventAdjustProgressTime):
                # Progress is a replaceable state update; never replay stale ticks.
                while True:
                    try:
                        newer_event = self.events_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if isinstance(newer_event, EventAdjustProgressTime):
                        self.events_queue.task_done()
                        event = newer_event
                    else:
                        self._pending_event = newer_event
                        break
                if self.analysis is not None:
                    self.current_progress = event.progress_time_ms
                    await self.handle_adjust_progress(self.current_progress)
            elif isinstance(event, EventStop):
                logger.warning("Song stopped!")
                self._cancel_beat_task()
                self._cancel_strobe_task()
                self.analysis = None
            self.events_queue.task_done()
            await asyncio.sleep(CONTROLLER_TICK)

    def handle_song_changed(self, event: EventSongChanged):
        if self._song_log is not None:
            logger.remove(self._song_log)
        os.makedirs(SONG_LOG_DIR, exist_ok=True)
        log_name = (
            f"{time.strftime('%Y%m%d-%H%M%S')} {event.artist_names} - {event.song_name} [{event.track_id}].log"
        ).replace(os.sep, "_")
        self._song_log = logger.add(
            os.path.join(SONG_LOG_DIR, log_name), level="TRACE", format=SONG_LOG_FORMAT, colorize=False,
        )
        self.analysis = event.analysis
        with open("analysis.json", "w") as f:
            json.dump(self.analysis, f, indent=4)

        conf = self.min_confidence
        self.bars = sorted(
            [b for b in self.analysis['bars'] if b.get('confidence', 1.0) >= conf],
            key=lambda x: x['start'],
        )
        self._parts = detect_sections(self.analysis, self.bars)
        self._part_energies = self._energies_for(self._parts)
        logger.debug(
            "Custom part starts: {}",
            ' '.join(
                f"{index}:{part['start']:.2f}s/{energy:.2f}"
                for index, (part, energy) in enumerate(zip(self._parts, self._part_energies))
            ),
        )
        self.sections = self._split_phrases()
        logger.debug(
            "Phrase starts: {}",
            ' '.join(f"{index}:{section['start']:.2f}s" for index, section in enumerate(self.sections)),
        )
        bar_starts = [bar['start'] for bar in self.bars]
        self._section_bar_starts = []
        for section in self.sections:
            bar_index = bisect.bisect_left(bar_starts, section.get('start', 0.0))
            self._section_bar_starts.append(
                bar_starts[bar_index] if bar_index < len(bar_starts) else section.get('start', 0.0)
            )
        self.beats = sorted(
            [b for b in self.analysis['beats'] if b.get('confidence', 1.0) >= conf],
            key=lambda x: x['start'],
        )
        self._beat_starts = [b['start'] for b in self.beats]
        # Pulse cycles restart on each section's first beat, so every section
        # needs its first beat index and how many beats it spans.
        self._section_first_beat = [
            bisect.bisect_left(self._beat_starts, start) for start in self._section_bar_starts
        ]
        self._cancel_beat_task()
        self._cancel_strobe_task()
        self._strobe_scheduled_for = -1
        self._flow_scheduled_for = -1
        self._beat_scheduled_for = -1
        self._tatum_starts =[t['start'] for t in self.analysis.get('tatums', []) if t.get('confidence', 1.0) >= conf]
        self.current_song = event.song_name

        track = self.analysis.get('track', {})
        logger.info(
            "Analysis saved: {} — {} | duration={:.2f}s | start progress={:.2f}s | "
            "custom sections={} bars={} beats={} segments={}",
            event.song_name or 'Unknown track', event.artist_names or 'Unknown artist',
            track.get('duration', 0.0), event.progress_time_ms,
            len(self.sections), len(self.bars), len(self.beats), len(self.analysis.get('segments', [])),
        )

        self._fade_in_end = track.get('end_of_fade_in', 0.0)
        self._fade_out_start = track.get('start_of_fade_out', float('inf'))
        self._track_duration = track.get('duration', 0.0)
        logger.info(
            "Fade in ends at {:.2f}s, fade out starts at {:.2f}s",
            self._fade_in_end, self._fade_out_start if self._fade_out_start < 1e9 else 0,
        )

        self._compute_section_energies()
        self._compute_section_timbres()
        self._compute_quiet_bars()
        self._compute_flow_bars()
        bar_starts = [bar['start'] for bar in self.bars]
        section_bars =[bisect.bisect_left(bar_starts, start) for start in self._section_bar_starts]
        self._short_sections = {
            index for index in range(len(self.sections) - 1)
            if section_bars[index + 1] - section_bars[index] < MIN_COLOR_SECTION_BARS
        }
        logger.debug("Short sections (faded across): {}", sorted(self._short_sections))
        self._compute_strobe_bars()

        self.min_segment_duration, self.beats_per_measure, self.avg_measure_duration = \
            min_segment_duration_from_future_measures(self.beats, self.bars, event.progress_time_ms)

        logger.info(
            f"Auto MIN_SEGMENT_DURATION≈{self.min_segment_duration:.3f}s "
            f"(avg_measure={self.avg_measure_duration:.3f}s, beats/measure={self.beats_per_measure})"
        )

        confident_segments = [s for s in self.analysis['segments'] if s.get('confidence', 1.0) >= conf]
        self.segments = merge_short_segments(confident_segments, min_duration=self.min_segment_duration)
        self._compute_loudness_stats()

        self.last_next_bar = self.bars[0] if self.bars else {}
        self.current_section = self.sections[0] if self.sections else None
        self.current_section_index = -1
        self.section_color_indexes = self._build_section_color_indexes(len(self.sections))
        self._color_trans_duration = 0.0
        self._pre_section_trans_for = -1
        self._last_progress = event.progress_time_ms
        self._last_progress_mono = time.monotonic()

    def _split_phrases(self) -> list[dict]:
        """Cut each part into phrases of a fixed bar count from its first bar,
        so colour changes and energy follow the music's phrasing inside long
        parts (an intro and the build after it no longer share one energy)."""
        bar_starts = [bar['start'] for bar in self.bars]
        segments = self.analysis.get('segments', [])
        segment_starts = [s.get('start', 0.0) for s in segments]
        duration = self.analysis.get('track', {}).get('duration', 0.0)
        part_bars = [bisect.bisect_left(bar_starts, part['start']) for part in self._parts] + [len(self.bars)]
        phrases = []
        for index, part in enumerate(self._parts):
            first, end = part_bars[index], part_bars[index + 1]
            length = PHRASE_BARS_LOUD if self._part_energies[index] >= PHRASE_LOUD_ENERGY else PHRASE_BARS_CALM
            cuts = list(range(first, end, length))
            if len(cuts) > 1 and end - cuts[-1] < PHRASE_MIN_BARS:
                cuts.pop()
            starts = [part['start']] + [bar_starts[bar] for bar in cuts[1:]]
            ends = starts[1:] + [part['start'] + part['duration']]
            for start, stop in zip(starts, ends):
                in_phrase = segments[bisect.bisect_left(segment_starts, start):bisect.bisect_left(segment_starts, stop)]
                phrases.append({
                    'start': start,
                    'duration': stop - start,
                    'confidence': part.get('confidence', 1.0),
                    'loudness': float(np.mean([s.get('loudness_max', -20.0) for s in in_phrase])) if in_phrase else -60.0,
                })
        assert abs(sum(phrase['duration'] for phrase in phrases) - duration) < 1e-6 or not duration
        return phrases

    def _compute_section_energies(self):
        self._section_energies = self._energies_for(self.sections)
        logger.debug(
            "Phrase energies: {}",
            ' '.join(f"{i}:{energy:.2f}" for i, energy in enumerate(self._section_energies))
        )

    def _energies_for(self, sections: list[dict]) -> list[float]:
        """Per-span activity (0–1): loudness rank blended with onset-density rank.

        Loud but sustained passages and busy but quiet ones both land mid-range;
        only spans that are loud *and* busy reach the full brightness swing.
        """
        if not sections:
            return []
        segment_starts = [s.get('start', 0.0) for s in self.analysis.get('segments', [])]
        densities = []
        for section in sections:
            start = section.get('start', 0.0)
            end = start + section.get('duration', 0.0)
            count = bisect.bisect_left(segment_starts, end) - bisect.bisect_left(segment_starts, start)
            densities.append(count / max(section.get('duration', 0.0), 1e-6))

        def ranks(values):
            order = np.argsort(values, kind='stable')
            scores = np.empty(len(order), dtype=float)
            for rank, index in enumerate(order):
                scores[index] = rank / max(len(order) - 1, 1)
            return scores

        loudnesses = [section.get('loudness', -20.0) for section in sections]
        return (0.6 * ranks(loudnesses) + 0.4 * ranks(densities)).tolist()

    def _compute_quiet_bars(self):
        """Bars far quieter than the bars leading into them (breakdowns, gaps).

        Measured against the preceding bars rather than the section, so a
        breakdown that forms its own section still counts.
        """
        segments = self.analysis.get('segments', [])
        segment_starts = [s.get('start', 0.0) for s in segments]
        self._bar_loudness = []
        peaks = []
        for bar in self.bars:
            start = bar['start']
            end = start + bar.get('duration', 0.0)
            in_bar = segments[bisect.bisect_left(segment_starts, start):bisect.bisect_left(segment_starts, end)]
            # A bar with no segments is silence.
            self._bar_loudness.append(
                float(np.mean([s.get('loudness_max', -20.0) for s in in_bar])) if in_bar else -60.0
            )
            peaks.append(max((s.get('loudness_max', -20.0) for s in in_bar), default=-60.0))
        self._quiet_bars = {
            index for index in range(1, len(self.bars))
            if peaks[index] < float(np.median(peaks[max(0, index - QUIET_LOOKBACK_BARS):index])) - QUIET_BAR_DB
        }
        logger.debug("Quiet bars: {}", ' '.join(str(index) for index in sorted(self._quiet_bars)))

    def _compute_flow_bars(self):
        """Bars without transients: soft segment onsets and beats Spotify
        only inferred from the tempo. Gaps shorter than FLOW_MIN_BARS inside
        a flow run are flow; runs shorter than it are not."""
        segments = self.analysis.get('segments', [])
        segment_starts = [s.get('start', 0.0) for s in segments]
        beats = self.analysis['beats']
        beat_starts = [b['start'] for b in beats]
        flags = []
        for bar in self.bars:
            start = bar['start']
            end = start + bar.get('duration', 0.0)
            in_bar = segments[bisect.bisect_left(segment_starts, start):bisect.bisect_left(segment_starts, end)]
            beats_in_bar = beats[bisect.bisect_left(beat_starts, start):bisect.bisect_left(beat_starts, end)]
            # A bar with no segments or no beats has nothing to pulse on.
            onset = float(np.mean([s['loudness_max'] - s['loudness_start'] for s in in_bar])) if in_bar else 0.0
            confidence = float(np.mean([b.get('confidence', 1.0) for b in beats_in_bar])) if beats_in_bar else 0.0
            flags.append(onset < FLOW_MAX_ONSET_DB and confidence < FLOW_MAX_BEAT_CONFIDENCE)

        def runs():
            out = []
            for index, flag in enumerate(flags):
                if out and out[-1][0] == flag:
                    out[-1][2] = index + 1
                else:
                    out.append([flag, index, index + 1])
            return out

        for flag, start, end in runs()[1:-1]:
            if not flag and end - start < FLOW_MIN_BARS:
                flags[start:end] = [True] * (end - start)
        for flag, start, end in runs():
            if flag and end - start < FLOW_MIN_BARS:
                flags[start:end] = [False] * (end - start)
        self._flow_bars = {index for index, flag in enumerate(flags) if flag}
        logger.debug("Flow bars: {}", ' '.join(str(index) for index in sorted(self._flow_bars)))

    def _compute_strobe_bars(self):
        """The loudest bar of each energetic part, never a phrase's last bar
        (that one belongs to the hue fade) nor a quiet or flow bar."""
        bar_starts = [bar['start'] for bar in self.bars]
        part_bars = [bisect.bisect_left(bar_starts, part['start']) for part in self._parts] + [len(self.bars)]
        phrase_bars = {bisect.bisect_left(bar_starts, start) for start in self._section_bar_starts} | {len(self.bars)}
        self._strobe_bars = set()
        for index, energy in enumerate(self._part_energies):
            first, end = part_bars[index], part_bars[index + 1]
            if energy < STROBE_MIN_ENERGY or end - first < STROBE_MIN_BARS:
                continue
            candidates = [
                bar for bar in range(first, end)
                if bar + 1 not in phrase_bars and bar not in self._quiet_bars and bar not in self._flow_bars
                and self._section_index_for_time(bar_starts[bar]) not in self._short_sections
            ]
            if candidates:
                self._strobe_bars.add(max(candidates, key=lambda bar: self._bar_loudness[bar]))
        logger.debug("Strobe bars: {}", ' '.join(str(bar) for bar in sorted(self._strobe_bars)))

    def _compute_section_timbres(self):
        """Average segment timbre vectors per section for structural boundary detection."""
        if not self.sections:
            self._section_timbres = []
            return
        segments = self.analysis.get('segments', [])
        timbres = []
        for section in self.sections:
            s_start = section.get('start', 0.0)
            s_end = s_start + section.get('duration', 0.0)
            vecs = [s['timbre'] for s in segments
                    if s_start <= s.get('start', 0.0) < s_end and 'timbre' in s]
            timbres.append(np.mean(vecs, axis=0) if vecs else np.zeros(12))
        self._section_timbres = timbres

    def _compute_loudness_stats(self):
        decibel_to_linear = lambda x: 10**(x / 20)
        loudness_values = [decibel_to_linear(seg['loudness_start']) for seg in self.segments]
        mean_l = np.mean(loudness_values)
        std_l = np.std(loudness_values)
        lower = mean_l - 2 * std_l
        upper = mean_l + 2 * std_l
        filtered = [l for l in loudness_values if lower <= l <= upper]
        self._loudness_min = min(filtered) if filtered else 0.0
        self._loudness_max = max(filtered) if filtered else 1.0
        self._segment_starts = [s['start'] for s in self.segments]
        self._last_segment_idx = -1

    def _segment_loudness(self, segment) -> float:
        """Segment loudness normalised to the track's range (0–1)."""
        loudness = 10 ** (segment["loudness_start"] / 20)
        span = self._loudness_max - self._loudness_min
        raw = 0.5 if span == 0 else (loudness - self._loudness_min) / span
        return max(0.0, min(1.0, raw))

    def _fade_multiplier(self, current_time: float) -> float:
        if self._fade_in_end > 0 and current_time < self._fade_in_end:
            return max(0.05, current_time / self._fade_in_end)
        if self._fade_out_start < float('inf') and current_time > self._fade_out_start:
            remaining = self._track_duration - self._fade_out_start
            if remaining > 0:
                return max(0.05, 1.0 - (current_time - self._fade_out_start) / remaining)
        return 1.0

    def _panel_interp(self) -> tuple:
        """Interpolated (hue, sat, bri) for panel animation — matches the smooth bulb transition."""
        if self._trans_duration <= 0:
            return float(self.current_hue), float(self.current_saturation), float(self.current_brightness)
        t = min(1.0, (time.monotonic() - self._trans_start_time) / self._trans_duration)
        # Shortest-arc hue interpolation (handles 350→10 going through 0 correctly)
        diff = ((self.current_hue - self._trans_start_hue) + 180) % 360 - 180
        h = (self._trans_start_hue + diff * t) % 360
        s = self._trans_start_sat + (self.current_saturation - self._trans_start_sat) * t
        b = self._trans_start_bri + (self.current_brightness - self._trans_start_bri) * t
        return h, s, b

    def panel_state(self) -> tuple:
        """Return (hue, sat, bri) as ints for the terminal panel."""
        h, s, b = self._panel_interp()
        return int(h), int(s), int(b)

    def _section_hue_sat(self, section: dict):
        """A palette colour 40–MAX_HUE_STEP degrees from the current hue."""
        current = self._current_params['hue']
        nearby = [
            color for color in VIBRANT_COLOR_PALETTE
            if 40 <= abs((color[0] - current + 180) % 360 - 180) <= MAX_HUE_STEP
        ]
        return random.choice(nearby)

    async def handle_adjust_progress(self, current_time: float):
        now = time.monotonic()
        expected = self._last_progress + (now - self._last_progress_mono)
        if abs(current_time - expected) > SEEK_THRESHOLD:
            self._reset_for_seek(expected, current_time)
        self._last_progress = current_time
        self._last_progress_mono = now

        next_bar = get_next_item(self.bars, current_time)
        if not self._segment_starts:
            logger.debug("skipping adjust progress")
            return

        current_section_index = self._section_index_for_time(current_time)
        if current_section_index is None:
            logger.debug("skipping adjust progress: no section found")
            return

        bar_index = max(0, bisect.bisect_right([b['start'] for b in self.bars], current_time) - 1)
        timing_second = int(current_time)
        if timing_second // 5 != self._last_timing_log_second // 5:
            section = self.sections[current_section_index]
            bar = self.bars[bar_index] if self.bars else {}
            logger.debug(
                "Timing: song={} t={:.2f}s | section={} start={:.2f}s | bar={} start={:.2f}s | "
                "section_delta={:+.2f}s bar_delta={:+.2f}s",
                self.current_song or 'unknown', current_time, current_section_index,
                section.get('start', 0.0), bar_index, bar.get('start', 0.0),
                current_time - section.get('start', 0.0), current_time - bar.get('start', 0.0),
            )
            self._last_timing_log_second = timing_second

        section = self.sections[current_section_index]
        section_changed = self.current_section_index != current_section_index
        seg_idx = min(
            max(0, bisect.bisect_right(self._segment_starts, current_time) - 1),
            len(self.segments) - 1,
        )
        energy_score = self._section_energies[current_section_index]
        fade = self._fade_multiplier(current_time)

        # The last HUE_FADE_BARS before an important boundary are one HSV
        # command into the new colour, landing on the section's first beat at
        # the level the last beat would have rested at.
        # The hue crosses into the next section's colour over its last
        # HUE_FADE_BARS while the beats keep pulsing: every pulse command
        # carries the colour the crossing has reached when it lands. Short
        # sections get no colour of their own: the crossing targets the first
        # full section after them and starts on the first short one. It is
        # set up a beat ahead so pulses already scheduled into it carry it.
        target_idx = current_section_index + 1
        while target_idx in self._short_sections:
            target_idx += 1
        if (
            target_idx < len(self.sections)
            and self._beat_starts
            and self._pre_section_trans_for != target_idx
            and not section_changed
        ):
            target = self.sections[target_idx]
            target_start = self._section_bar_starts[target_idx]
            important_boundary = target_idx in self.section_color_indexes
            if current_section_index + 1 in self._short_sections:
                fade_start = self._section_bar_starts[current_section_index + 1]
            elif current_section_index in self._short_sections:
                run_start = current_section_index
                while run_start - 1 in self._short_sections:
                    run_start -= 1
                fade_start = self._section_bar_starts[run_start]
            else:
                bar_starts = [bar['start'] for bar in self.bars]
                fade_start = max(
                    bar_starts[max(0, bisect.bisect_left(bar_starts, target_start) - HUE_FADE_BARS)],
                    self._section_bar_starts[current_section_index],
                )
            lead_beat = max(0, bisect.bisect_left(self._beat_starts, fade_start) - 1)
            if (
                important_boundary
                and target.get('confidence', 1.0) >= self.min_confidence
                and min(self._beat_starts[lead_beat], fade_start) <= current_time < target_start
            ):
                self._start_hue_crossing(target_idx, current_time, max(fade_start, current_time), target_start)

        # Sections are detected at their raw start; color changes are anchored
        # to the first analyzed bar in that section. This only fires when the
        # fade was not scheduled ahead (e.g. after a seek).
        if section_changed:
            logger.info(
                "Section change (index {}, energy={:.2f})",
                current_section_index, energy_score,
            )
            self.current_section_index = current_section_index
            if (
                current_section_index in self.section_color_indexes
                and current_section_index not in self._short_sections
                and section.get('confidence', 1.0) >= self.min_confidence
                and self._pre_section_trans_for != current_section_index
            ):
                start = max(self._section_bar_starts[current_section_index], current_time)
                self._start_hue_crossing(
                    current_section_index, current_time,
                    start, start + self.avg_measure_duration / max(1, self.beats_per_measure),
                )

        # Original atmospheric engine: every analyzed beat gets a short pulse;
        # segment loudness and section energy control its height.
        self._last_segment_idx = seg_idx
        next_beat_idx = bisect.bisect_right(self._beat_starts, current_time)
        if next_beat_idx < len(self.beats) and next_beat_idx != self._beat_scheduled_for:
            next_beat = self.beats[next_beat_idx]
            fire_in = next_beat['start'] - current_time
            if fire_in < 0.05:
                # Beat is essentially "now" — skip it; this happens during Spotify catch-up
                # bursts where song time jumps ahead rapidly and next beats appear stale.
                self._beat_scheduled_for = next_beat_idx
            elif self._beat_bar(next_beat_idx) in self._strobe_bars:
                # The strobe owns its whole bar: one task steps the colour on
                # every eighth note from this beat to the bar's end.
                self._beat_scheduled_for = next_beat_idx
                bar = self._beat_bar(next_beat_idx)
                if self._strobe_scheduled_for != bar:
                    self._strobe_scheduled_for = bar
                    bar_end = self.bars[bar]['start'] + self.bars[bar].get('duration', 0.0)
                    eighths = []
                    for beat in self.beats[next_beat_idx:]:
                        if beat['start'] >= bar_end - 0.01:
                            break
                        half = beat.get('duration', 0.5) / 2
                        eighths += [(beat['start'], half), (beat['start'] + half, half)]
                    now = time.monotonic()
                    peak = to_brightness(band_for(energy_score)[1] * fade)
                    logger.debug(
                        "Beat {:>3} | strobe bar {}: {} steps at {:.1f}%",
                        next_beat_idx, bar, len(eighths), self._scale_brightness(peak),
                    )
                    self._cancel_strobe_task()
                    self._strobe_task = asyncio.create_task(self._scheduled_strobe(
                        [(now + (start - current_time), length) for start, length in eighths], peak,
                    ))
            elif self._beat_bar(next_beat_idx) in self._flow_bars:
                # Flow bars have no transients to pulse on: one command per
                # bar settles the brightness at the bar's loudness inside
                # the section's band over the rest of the bar, and drifts
                # the hue one palette step with it unless a section colour
                # crossing is already moving it.
                self._beat_scheduled_for = next_beat_idx
                bar = self._beat_bar(next_beat_idx)
                if self._flow_scheduled_for != bar:
                    self._flow_scheduled_for = bar
                    deadline = time.monotonic() + fire_in
                    length = self.bars[bar]['start'] + self.bars[bar].get('duration', 0.0) - next_beat['start']
                    bottom, top = band_for(energy_score)
                    rank = bisect.bisect_left(sorted(self._bar_loudness), self._bar_loudness[bar]) / max(len(self.bars) - 1, 1)
                    level = to_brightness((bottom + (top - bottom) * (0.5 + 0.5 * rank)) * fade)
                    if self._color_at(deadline) != self._color_at(deadline + length):
                        logger.debug(
                            "Beat {:>3} | flow bar {}: crossing, settle at {:.1f}% over {:.2f}s",
                            next_beat_idx, bar, self._scale_brightness(level), length,
                        )
                        self._start_beat_task(self._scheduled_hold(deadline, length, level))
                    else:
                        hue, saturation = self._flow_hue_sat()
                        logger.debug(
                            "Beat {:>3} | flow bar {}: drift to hue={} sat={} at {:.1f}% over {:.2f}s",
                            next_beat_idx, bar, hue, saturation, self._scale_brightness(level), length,
                        )
                        self._start_beat_task(self._scheduled_drift(deadline, length, level, hue, saturation))
            elif self._beat_bar(next_beat_idx) in self._quiet_bars:
                # Quiet bars hold at the resting level: the first beat of a
                # quiet run settles there over one beat, the rest stay still,
                # except while a colour is crossing: then every beat sends a
                # hold so the crossing keeps moving.
                self._beat_scheduled_for = next_beat_idx
                deadline = time.monotonic() + fire_in
                beat_len = next_beat.get('duration', 0.5)
                crossing = self._color_at(deadline) != self._color_at(deadline + beat_len)
                if crossing or next_beat_idx == 0 or self._beat_bar(next_beat_idx - 1) not in self._quiet_bars:
                    resting = to_brightness(band_for(energy_score)[0] * fade)
                    logger.debug("Beat {:>3} | quiet, hold at {:.1f}%", next_beat_idx, self._scale_brightness(resting))
                    self._start_beat_task(self._scheduled_hold(deadline, beat_len, resting))
            else:
                self._beat_scheduled_for = next_beat_idx
                deadline = time.monotonic() + fire_in
                section_first_beat = self._section_first_beat[current_section_index]
                section_end_beat = (
                    self._section_first_beat[current_section_index + 1]
                    if current_section_index + 1 < len(self._section_first_beat)
                    else len(self.beats)
                )
                profile_index, profile = profile_for(energy_score, section_end_beat - section_first_beat)
                pulses = pulses_for(
                    profile, self.atmospheric_pulse_on, self.atmospheric_pulse_off,
                )
                pulse, attack, hold, release = pulses[(next_beat_idx - section_first_beat) % len(pulses)]
                beat_len = next_beat.get('duration', 0.5)
                # Envelopes are beat fractions tuned around 120 BPM. In a slow
                # song the snappy profiles leave 30-50ms jumps between very
                # different levels, which read as flashes once the low end is
                # visible. Floor the ramps at a share of the beat that grows as
                # the tempo drops: nothing above ~133 BPM, half a beat at 60.
                slow = max(0.0, min(1.0, (beat_len - 0.45) / 0.45))
                attack = max(attack, 0.5 * slow)
                release = max(release, 0.5 * slow)
                hold = min(hold, max(0.0, 1.0 - attack - release))
                # Calm and medium sections ease into every beat and decay all
                # the way into the next one instead of snapping and idling.
                # Energetic sections keep their snap, but while a colour is
                # crossing they also decay into the next beat: each command
                # carries the colour, so idle gaps would make it step.
                energy_level = round(energy_score * 4)
                if energy_level < 3:
                    attack = max(attack, 0.2)
                    hold = min(hold, 0.1)
                attack = max(attack, MIN_ATTACK_S / beat_len)
                # Release start (attack + hold), clamped into the window the
                # command gap leaves; a beat too short for two gaps splits evenly.
                if beat_len >= 2 * MIN_COMMAND_GAP_S:
                    peak_end = min(max(attack + hold, MIN_COMMAND_GAP_S / beat_len), 1.0 - MIN_COMMAND_GAP_S / beat_len)
                else:
                    peak_end = 0.5
                attack = min(attack, peak_end)
                hold = peak_end - attack
                if energy_level < 3 or self._color_at(deadline) != self._color_at(deadline + beat_len):
                    release = max(0.0, 1.0 - attack - hold)
                else:
                    release = max(0.0, min(release, 1.0 - attack - hold))
                # The section's band sets the swing; the pulse places this
                # beat's peak inside it, pulled down on quieter segments.
                bottom, top = band_for(energy_score)
                strength = pulse * (0.5 + 0.5 * self._segment_loudness(self.segments[seg_idx]))
                low = to_brightness(bottom * fade)
                high = to_brightness((bottom + (top - bottom) * strength) * fade)
                energy_color = ("blue", "cyan", "green", "yellow", "red")[energy_level]
                logger.opt(colors=True).debug(
                    f"<bold>Beat {{beat:>3}}</bold> | <{energy_color}>E{{energy}}</{energy_color}> P{{profile}} | "
                    "pulse <magenta>{pulse:>4.2f}</magenta> | "
                    "env <cyan>{attack:>4.2f} {hold:>4.2f} {release:>4.2f}</cyan> | "
                    "brightness <green>{high:>5.1f}%</green> → <dim>{low:>5.1f}%</dim>",
                    beat=next_beat_idx, energy=energy_level,
                    profile=profile_index, pulse=pulse, attack=attack, hold=hold,
                    release=release, high=self._scale_brightness(high), low=self._scale_brightness(low),
                )
                self._start_beat_task(
                    self._scheduled_beat_pulse(
                        deadline, high, low, beat_len,
                        (attack, hold, release),
                    )
                )

        # Bar transition → recompute min-segment duration to track tempo changes
        if next_bar and self.last_next_bar != next_bar and next_bar['confidence'] > 0.8:
            self.min_segment_duration, self.beats_per_measure, self.avg_measure_duration = \
                min_segment_duration_from_future_measures(self.beats, self.bars, current_time)

            self.segments = merge_short_segments(self.analysis['segments'], min_duration=self.min_segment_duration)
            self._compute_loudness_stats()
            logger.debug(
                f"Recomputed MIN_SEGMENT_DURATION={self.min_segment_duration:.3f}s "
                f"(avg_measure={self.avg_measure_duration:.3f}s, beats/measure={self.beats_per_measure}) "
                f"at t={current_time:.2f}s"
            )
            self.last_next_bar = next_bar

    async def _scheduled_beat_pulse(
        self,
        deadline: float,
        spike: float,
        base: float,
        beat_duration: float,
        envelope: tuple[float, float, float],
    ):
        await asyncio.sleep(max(0.0, deadline - self._device_latency() - time.monotonic()))
        attack, hold, release = envelope
        await self.set_parameters(beat_duration * attack, brightness=spike, deadline=deadline)
        base_deadline = deadline + beat_duration * (attack + hold)
        await asyncio.sleep(max(0.0, base_deadline - self._device_latency() - time.monotonic()))
        await self.set_parameters(
            beat_duration * release,
            brightness=base,
            deadline=base_deadline,
        )

    async def _scheduled_strobe(self, steps: list[tuple[float, float]], peak: float):
        for deadline, length in steps:
            await asyncio.sleep(max(0.0, deadline - self._device_latency() - time.monotonic()))
            hue, saturation = get_vibrant_color(self._current_params['hue'])
            await self.set_parameters(
                length * 0.25,
                brightness=peak,
                change_color=True,
                target_hue=hue,
                target_saturation=saturation,
                deadline=deadline,
            )

    async def _scheduled_hold(self, deadline: float, duration: float, resting: float):
        await asyncio.sleep(max(0.0, deadline - self._device_latency() - time.monotonic()))
        await self.set_parameters(duration, brightness=resting, deadline=deadline)

    async def _scheduled_drift(self, deadline: float, duration: float, level: float, hue: int, saturation: int):
        await asyncio.sleep(max(0.0, deadline - self._device_latency() - time.monotonic()))
        await self.set_parameters(
            duration, brightness=level, change_color=True,
            target_hue=hue, target_saturation=saturation, deadline=deadline,
        )

    def _flow_hue_sat(self) -> tuple[int, int]:
        """The nearest palette colour FLOW_HUE_STEP_MIN–FLOW_HUE_STEP_MAX
        degrees up the wheel, so flow bars drift one way instead of wandering."""
        current = self._current_params['hue']
        return min(
            (color for color in VIBRANT_COLOR_PALETTE
             if FLOW_HUE_STEP_MIN <= (color[0] - current) % 360 <= FLOW_HUE_STEP_MAX),
            key=lambda color: (color[0] - current) % 360,
        )

    def _beat_bar(self, beat_idx: int) -> int:
        return max(0, bisect.bisect_right([b['start'] for b in self.bars], self._beat_starts[beat_idx]) - 1)

    def _start_hue_crossing(self, section_idx: int, current_time: float, start: float, end: float):
        """Cross the colour into the section's over song time start→end.

        Only the crossing is set up here; the pulses scheduled over it send
        the colour it has reached as they land.
        """
        hue, saturation = self._section_hue_sat(self.sections[section_idx])
        start_mono = time.monotonic() + (start - current_time)
        start_hue, start_sat = self._color_at(start_mono)
        self._color_trans_start_hue = float(start_hue)
        self._color_trans_start_sat = float(start_sat)
        self._color_trans_start_time = start_mono
        self._color_trans_duration = end - start
        self._current_params['hue'] = hue
        self._current_params['saturation'] = saturation
        self._pre_section_trans_for = section_idx
        logger.info(
            "Hue crossing → hue={} sat={} over {:.2f}s into section {} (starts in {:.2f}s)",
            hue, saturation, end - start, section_idx, start - current_time,
        )

    def _reset_for_seek(self, expected: float, current_time: float):
        """Drop everything scheduled against the old position.

        Pending beats and hue fades carry monotonic deadlines computed
        from the old song time. The colour freezes where its crossing had got
        to, and the section is re-entered from scratch so it recolours.
        """
        logger.info("Seek detected {:.2f}s → {:.2f}s; rescheduling", expected, current_time)
        self._cancel_beat_task()
        self._cancel_strobe_task()
        self._strobe_scheduled_for = -1
        self._flow_scheduled_for = -1
        self._beat_scheduled_for = -1
        self._pre_section_trans_for = -1
        self.current_section_index = -1
        hue, saturation = self._color_at(time.monotonic())
        self._current_params['hue'] = hue
        self._current_params['saturation'] = saturation
        self._color_trans_duration = 0.0

    def _section_index_for_time(self, current_time: float) -> int | None:
        if not self.sections:
            return None
        for idx, section in enumerate(self.sections):
            section_end = section.get('start', 0.0) + section.get('duration', 0.0)
            if current_time < section_end:
                return idx
        return len(self.sections) - 1

    def _build_section_color_indexes(self, section_count: int) -> set[int]:
        indexes = set(range(section_count))
        logger.debug("Color transitions at every section boundary ({} sections)", section_count)
        return indexes

    def _device_latency(self) -> float:
        base = max(d.estimated_latency for d in self.devices) if self.devices else 0.02
        return base + self.sync_offset

    def _start_beat_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._beat_tasks.add(task)
        task.add_done_callback(self._beat_tasks.discard)

    def _cancel_beat_task(self):
        for task in list(self._beat_tasks):
            task.cancel()
        self._beat_tasks.clear()

    def _cancel_strobe_task(self):
        if self._strobe_task and not self._strobe_task.done():
            self._strobe_task.cancel()
        self._strobe_task = None

    async def set_parameters(
        self,
        duration: float = 0.05,
        brightness: float | None = None,
        change_color: bool = False,
        wait_before_ms: int = 0,
        target_hue: int | None = None,
        target_saturation: int | None = None,
        deadline: float | None = None,
    ):
        if change_color:
            if target_hue is not None:
                new_hue = target_hue
                new_saturation = target_saturation if target_saturation is not None else self._current_params['saturation']
            else:
                new_hue, new_saturation = get_vibrant_color(self._current_params['hue'])
        else:
            new_hue = self._current_params['hue']
            new_saturation = self._current_params['saturation']

        if brightness is not None:
            new_brightness = self._scale_brightness(brightness)
        else:
            new_brightness = self._current_params['brightness']

        params_changed = (
            new_hue != self._current_params['hue'] or
            new_saturation != self._current_params['saturation'] or
            new_brightness != self._current_params['brightness']
        )

        now = time.monotonic()
        starts_at = deadline if deadline is not None else now
        # A command repeating the target still has to go out while the colour
        # it would carry has moved on.
        color_moved = self._color_at(starts_at + duration) != self._sent_color

        if (not params_changed and not color_moved) or (not change_color and brightness is None):
            return

        if change_color:
            # Restart the crossing from wherever the colour has got to.
            start_hue, start_sat = self._color_at(starts_at)
            self._color_trans_start_hue = float(start_hue)
            self._color_trans_start_sat = float(start_sat)
            self._color_trans_start_time = starts_at
            self._color_trans_duration = duration
            self._current_params['hue'] = new_hue
            self._current_params['saturation'] = new_saturation

        # A brightness-only command resolves its unset colour fields from the
        # bulb's in-flight target, which would cut a colour crossing short and
        # snap the new hue in. Every command lands on the crossing's position
        # at its landing time, and goes out as HSV whenever that differs from
        # what the bulb was last told.
        cmd_hue, cmd_sat = self._color_at(starts_at + duration)
        send_color = change_color or (cmd_hue, cmd_sat) != self._sent_color

        # Capture current interpolated position as the start of the panel animation
        ph, ps, pb = self._panel_interp()
        self._trans_start_hue = ph
        self._trans_start_sat = ps
        self._trans_start_bri = pb
        self._trans_start_time = now
        self._trans_duration = duration

        if brightness is not None:
            self._current_params['brightness'] = new_brightness

        self.current_hue = self._current_params['hue']
        self.current_saturation = self._current_params['saturation']
        self.current_brightness = self._current_params['brightness']

        if wait_before_ms:
            await asyncio.sleep(wait_before_ms / 1000)
        if send_color:
            self._sent_color = (cmd_hue, cmd_sat)
            await asyncio.gather(*(
                device.set_hsv(
                    cmd_hue, cmd_sat, new_brightness, duration=duration,
                    dispatch_at=(deadline - device.estimated_latency - self.sync_offset) if deadline is not None else None,
                )
                for device in self.devices
            ))
        elif brightness is not None:
            await asyncio.gather(*(
                device.set_brightness(
                    new_brightness, duration=duration,
                    dispatch_at=(deadline - device.estimated_latency - self.sync_offset) if deadline is not None else None,
                )
                for device in self.devices
            ))
        await asyncio.sleep(0)

    def _color_at(self, at: float) -> tuple[int, int]:
        """Hue and saturation the colour crossing has reached at monotonic time `at`."""
        hue = self._current_params['hue']
        saturation = self._current_params['saturation']
        if self._color_trans_duration <= 0:
            return hue, saturation
        t = max(0.0, min(1.0, (at - self._color_trans_start_time) / self._color_trans_duration))
        diff = ((hue - self._color_trans_start_hue) + 180) % 360 - 180
        return (
            round(self._color_trans_start_hue + diff * t) % 360,
            round(self._color_trans_start_sat + (saturation - self._color_trans_start_sat) * t),
        )

    def _scale_brightness(self, brightness: float) -> float:
        scaled = max(0.0, min(100.0, brightness * self.max_brightness / 100))
        # A lowered ceiling must not push visible levels into the off range.
        if brightness >= VISIBLE_MIN:
            scaled = max(scaled, VISIBLE_MIN)
        return scaled
