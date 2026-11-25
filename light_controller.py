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
from sections import DOWNBEAT_SPANS, detect_sections, downbeat_shares, downbeat_shift, shift_bars

# A bar whose loudest hit is this many dB under the median loudest hit of the
# QUIET_LOOKBACK_BARS before it is a breakdown: it holds one colour instead of
# pulsing. Judged on the loudest hit, not the bar's average: a bare kick with
# silence between the hits averages low but is no breakdown. A run just past
# the threshold holds at its phrase's mid level, one QUIET_DARK_DB deeper
# holds dark, and depths in between scale linearly.
QUIET_BAR_DB = 5.0
QUIET_LOOKBACK_BARS = 8
QUIET_DARK_DB = 15.0
# A run of segments at least SILENCE_MIN_S long whose loudest moment is
# SILENCE_TRACK_DB under the track's loud bars (90th percentile) and
# SILENCE_LOCAL_DB under the median loudest hit of the SILENCE_LOOKBACK_BARS
# before it is a near-silent gap (the breath before a drop): the lights fall
# fast to a dim level and come back on the next beat after it. The local test
# keeps a quiet intro, which is far under the loud bars all along, from
# dimming; the first bar, with no bars before it, is judged against itself.
SILENCE_TRACK_DB = 24.0
SILENCE_LOCAL_DB = 15.0
SILENCE_LOOKBACK_BARS = 4
SILENCE_MIN_S = 0.25
# A silence's dimming is scheduled this far ahead, like a beat.
SILENCE_LEAD_S = 0.5
# A gap falls to SILENCE_DIM_BRIGHTNESS (or the phrase's resting level, if
# lower) over SILENCE_FADE_S, capped at a third of the gap so a short one
# still gets to rest. Only a gap of at least SILENCE_DARK_MIN_S then carries
# on to real 0%, over SILENCE_DARK_FADE_S.
SILENCE_DIM_BRIGHTNESS = 4.0
SILENCE_FADE_S = 0.15
SILENCE_DARK_MIN_S = 1.0
SILENCE_DARK_FADE_S = 0.4
# A bar whose segments rise under FLOW_MAX_ONSET_DB from onset to peak and
# whose beats Spotify is less than FLOW_MAX_BEAT_CONFIDENCE sure of has no
# transients to pulse on (a vocal over a pad): it flows instead. Runs
# shorter than FLOW_MIN_BARS do not flip the mode either way.
FLOW_MAX_ONSET_DB = 8.0
FLOW_MAX_BEAT_CONFIDENCE = 0.5
FLOW_MIN_BARS = 2
# A bar within this many dB of the track's loud bars (90th percentile) is
# never flow: a brickwalled drop has no headroom for an onset to rise into
# and loses Spotify's beat tracker, yet it is the loudest part of the song.
FLOW_QUIET_DB = 6.0
# Each flow bar drifts the hue to the nearest palette colour this far up the
# wheel over the whole bar. The palette's widest gap is 40 degrees, so there
# is always one in range.
FLOW_HUE_STEP_MIN = 20
FLOW_HUE_STEP_MAX = 45
# The first bar of every drop (see sections.py) strobes: a new colour every
# eighth note, brightness held at the phrase's peak.
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
# One log file per played track, down to DEBUG. The per-lamp command lines
# are TRACE and stay out of it.
SONG_LOG_DIR = "logs"
SONG_LOG_LEVEL = "DEBUG"
SONG_LOG_FORMAT = "{time:HH:mm:ss.SSS} | {level:1.1} | {message}"
# The web app's identify button blinks a light this many times, this fast.
IDENTIFY_BLINKS = 3
IDENTIFY_BLINK_S = 0.3
# Names of the five energy levels, matching ATMOSPHERIC_DB in atmospheric.py.
ENERGY_NAMES = ("very calm", "calm", "medium", "energetic", "peak")
# Rules read only when a track's analysis runs: switching one re-runs it.
ANALYSIS_RULES = {'downbeat_fix'}
# Mechanics that can be switched off at runtime (from the web app) to judge
# each one by eye. All start on. RULES.md describes every rule in full.
RULES = {
    'downbeat_fix': "Bars move onto the beat the music's changes land on when Spotify's downbeat is off",
    'colour_changes': "Each phrase crosses into a new palette colour over its last bar",
    'strobe': "The first bar of every drop strobes a new colour every eighth note",
    'flow': "Bars without transients drift slowly instead of pulsing",
    'flow_hue_drift': "Flow bars step the hue one palette colour up the wheel per bar",
    'quiet': "Breakdown bars hold one colour, dimmer the quieter they are",
    'silence': "Near-silent gaps fall fast to a dim level; long ones go on to 0%",
    'loudness_pulse': "Quieter segments pull each beat's peak down inside the band",
    'slow_tempo_ramps': "Slow songs get longer pulse ramps so troughs don't flash",
    'track_fade': "Brightness follows the track's fade in and fade out",
}


def bar_runs(bars) -> list[tuple[int, int]]:
    """Consecutive runs of bar indexes as (first, last) pairs."""
    runs = []
    for bar in sorted(bars):
        if runs and runs[-1][1] == bar - 1:
            runs[-1] = (runs[-1][0], bar)
        else:
            runs.append((bar, bar))
    return runs

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
        self._quiet_bars: dict[int, float] = {}  # quiet bar index → 0 (dark) to 1 (phrase mid level)
        self._bar_peaks: list[float] = []
        self._silences: list[tuple[float, float, float]] = []   # (start, end, loudest dB) in song time
        self._silence_starts: list[float] = []
        self._silence_scheduled_for: int = -1    # index of the silence whose blackout is scheduled
        self._bar_loudness: list[float] = []
        self._strobe_bars: dict[int, int] = {}   # strobe bar index → the drop part it opens
        self._flow_bars: set[int] = set()
        self._flow_scheduled_for: int = -1       # bar index of the scheduled flow command
        self._short_sections: set[int] = set()   # sections the next colour fades across
        self._strobe_task: Optional[asyncio.Task] = None
        self._strobe_scheduled_for: int = -1     # bar index of the scheduled strobe
        self._section_timbres: list = []
        self._beat_starts: list = []
        self.current_song: str = ""
        self.current_artists: str = ""
        self.current_track_id: str = ""
        self.timeline_id: int = 0                # bumped on every loaded track, so the web app resends the timeline
        self.disabled_devices: set[str] = set()  # IPs switched off from the web app: sent nothing
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
        self.rules: dict[str, bool] = {name: True for name in RULES}

        # Web app display: brightness ramps over the last command's transition
        self._trans_start_bri: float = float(self.current_brightness)
        self._trans_start_time: float = 0.0
        self._trans_duration: float = 0.0

        # Color-only transition state keeps panel and command interpolation aligned.
        self._color_trans_start_hue: float = float(self.current_hue)
        self._color_trans_start_sat: float = float(self.current_saturation)
        self._color_trans_start_time: float = 0.0
        self._color_trans_duration: float = 0.0
        self._sent_color: tuple[int, int] | None = None   # last hue/sat sent to the devices
        self.mode: str = ""                     # pulse, flow, quiet or strobe: logged when it changes

        self._current_params = {
            'hue': self.current_hue,
            'saturation': self.current_saturation,
            'brightness': self.current_brightness
        }

    async def control_lights(self):
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
                # The listener repeats the stop on every poll while paused.
                if self.analysis is not None:
                    logger.warning("Lights stopped: {}", event.reason)
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
            os.path.join(SONG_LOG_DIR, log_name), level=SONG_LOG_LEVEL, format=SONG_LOG_FORMAT, colorize=False,
        )
        self.analysis = event.analysis
        with open("analysis.json", "w") as f:
            json.dump(self.analysis, f, indent=4)

        conf = self.min_confidence
        self.bars = sorted(
            [b for b in self.analysis['bars'] if b.get('confidence', 1.0) >= conf],
            key=lambda x: x['start'],
        )
        self.beats = sorted(
            [b for b in self.analysis['beats'] if b.get('confidence', 1.0) >= conf],
            key=lambda x: x['start'],
        )
        self._beat_starts = [b['start'] for b in self.beats]
        self._fix_downbeat()
        self._parts = detect_sections(self.analysis, self.bars)
        self._part_energies = self._energies_for(self._parts)
        logger.debug(
            "Parts: runs of bars uniform in loudness, timbre and chords. Energy is the part's loudness rank "
            "(60%) blended with its onset-density rank (40%); a drop is a part whose first bars sound far "
            "closer to the track's loudest sound than the bars before it"
        )
        self.sections = self._split_phrases()
        bar_starts = [bar['start'] for bar in self.bars]
        self._section_bar_starts = []
        for section in self.sections:
            bar_index = bisect.bisect_left(bar_starts, section.get('start', 0.0))
            self._section_bar_starts.append(
                bar_starts[bar_index] if bar_index < len(bar_starts) else section.get('start', 0.0)
            )
        # Pulse cycles restart on each section's first beat, so every section
        # needs its first beat index and how many beats it spans.
        self._section_first_beat = [
            bisect.bisect_left(self._beat_starts, start) for start in self._section_bar_starts
        ]
        self._cancel_beat_task()
        self._cancel_strobe_task()
        self._strobe_scheduled_for = -1
        self._flow_scheduled_for = -1
        self.mode = ""
        self._silence_scheduled_for = -1
        self._beat_scheduled_for = -1
        self._tatum_starts =[t['start'] for t in self.analysis.get('tatums', []) if t.get('confidence', 1.0) >= conf]
        self.current_song = event.song_name
        self.current_artists = event.artist_names
        self.current_track_id = event.track_id

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
        self._compute_silences()
        self._compute_flow_bars()
        bar_starts = [bar['start'] for bar in self.bars]
        section_bars =[bisect.bisect_left(bar_starts, start) for start in self._section_bar_starts]
        self._short_sections = {
            index for index in range(len(self.sections) - 1)
            if section_bars[index + 1] - section_bars[index] < MIN_COLOR_SECTION_BARS
        }
        for index in sorted(self._short_sections):
            logger.debug(
                "Phrase {} at {:.1f}s has {} bars (under {}): no colour of its own, the next colour crosses in across it",
                index, self._section_bar_starts[index], section_bars[index + 1] - section_bars[index], MIN_COLOR_SECTION_BARS,
            )
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
        self.timeline_id += 1

    def reanalyse(self):
        """Run the loaded track's analysis again from where it is playing,
        so detection settings changed mid-song apply to it."""
        logger.info("Re-analysing the current song with the changed settings")
        self.handle_song_changed(EventSongChanged(
            self.analysis, self.song_time(), song_name=self.current_song,
            artist_names=self.current_artists, track_id=self.current_track_id,
        ))

    def song_time(self) -> float:
        """Song position now, run on from the last progress tick."""
        return self._last_progress + (time.monotonic() - self._last_progress_mono)

    def _fix_downbeat(self):
        """Move Spotify's bars onto the beat the music's changes land on
        (sections.py): part, phrase, colour and strobe boundaries follow."""
        # A track too short for the longest window can't be judged.
        if len(self.bars) < 2 or len(self.beats) < 2 * max(DOWNBEAT_SPANS) + 5:
            logger.debug("Downbeat: too few beats to judge, Spotify's bars kept")
            return
        per_bar, shares = downbeat_shares(self.analysis, self.bars, self.beats)
        shift = downbeat_shift(shares)
        spread = ', '.join(f"beat {index + 1} {100 * share:.0f}%" for index, share in enumerate(shares))
        if not shift:
            logger.debug("Downbeat: changes by beat of Spotify's {}-beat bars: {}. No beat stands out over beat 1, "
                         "Spotify's bars kept", per_bar, spread)
            return
        if not self.rules['downbeat_fix']:
            logger.debug("Downbeat: changes by beat of Spotify's {}-beat bars: {}. Bars would move {} beat(s) later, "
                         "but the downbeat_fix rule is off", per_bar, spread, shift)
            return
        self.bars = shift_bars(self.bars, self.beats, shift)
        logger.info("Downbeat: changes by beat of Spotify's {}-beat bars: {}. Spotify's beat {} is the real downbeat, "
                    "so every bar moves {} beat(s) later (first bar now {:.2f}s)",
                    per_bar, spread, shift + 1, shift, self.bars[0]['start'])

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
            energy = self._part_energies[index]
            length = PHRASE_BARS_LOUD if energy >= PHRASE_LOUD_ENERGY else PHRASE_BARS_CALM
            cuts = list(range(first, end, length))
            if len(cuts) > 1 and end - cuts[-1] < PHRASE_MIN_BARS:
                cuts.pop()
            logger.debug(
                "Part {:>2} at {:6.1f}s, {:>3} bars: energy {:.2f} ({} {}, so {}-bar phrases) | {}: {}",
                index, part['start'], end - first, energy,
                '≥' if energy >= PHRASE_LOUD_ENERGY else '<', PHRASE_LOUD_ENERGY, length,
                'DROP' if part['drop'] else 'no drop', part['drop_why'],
            )
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
            "Phrases (index:start/energy, ranked like parts): {}",
            ' '.join(
                f"{index}:{section['start']:.1f}s/{energy:.2f}"
                for index, (section, energy) in enumerate(zip(self.sections, self._section_energies))
            ),
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
        self._bar_peaks = []
        for bar in self.bars:
            start = bar['start']
            end = start + bar.get('duration', 0.0)
            # Every segment sounding in the bar, not just those starting in it:
            # a chord struck just before the bar line fills the whole bar.
            first = max(0, bisect.bisect_right(segment_starts, start) - 1)
            sounding = [
                (s, min(s['start'] + s['duration'], end) - max(s['start'], start))
                for s in segments[first:bisect.bisect_left(segment_starts, end)]
            ]
            sounding = [(s, overlap) for s, overlap in sounding if overlap > 0]
            # A bar past the analysed audio has nothing sounding: silence.
            self._bar_loudness.append(
                float(np.average([s['loudness_max'] for s, _ in sounding], weights=[o for _, o in sounding]))
                if sounding else -60.0
            )
            self._bar_peaks.append(max((s['loudness_max'] for s, _ in sounding), default=-60.0))
        peaks = self._bar_peaks
        # How far each bar's loudest hit sits under the bars leading into it.
        deficits = [0.0] + [
            float(np.median(peaks[max(0, index - QUIET_LOOKBACK_BARS):index])) - peaks[index]
            for index in range(1, len(self.bars))
        ]
        quiet = [index for index in range(1, len(self.bars)) if deficits[index] > QUIET_BAR_DB]
        # Each run holds one level, set by how far under the bars before it sits.
        self._quiet_bars = {}
        for first, last in bar_runs(quiet):
            span = deficits[first:last + 1]
            depth = float(np.mean(span))
            closeness = max(0.0, 1.0 - (depth - QUIET_BAR_DB) / QUIET_DARK_DB)
            for bar in range(first, last + 1):
                self._quiet_bars[bar] = closeness
            logger.debug(
                "Quiet bars {}–{} ({:.1f}s): loudest hit {:.1f}–{:.1f} dB under the {} bars before (more than {} dB), "
                "so they hold one colour without pulsing, at {:.0f}% of the way from dark to the phrase's mid level "
                "({:.1f} dB deep on average; {} dB deeper than the threshold is dark)",
                first, last, self.bars[first]['start'], min(span), max(span), QUIET_LOOKBACK_BARS, QUIET_BAR_DB,
                100 * closeness, depth, QUIET_DARK_DB,
            )

    def _compute_silences(self):
        """Near-silent gaps between hits: runs of segments far under both the
        track's loud bars and the bars just before them, lasting at least
        SILENCE_MIN_S. The lights dim for them, and long ones go dark."""
        # The min_confidence filter can leave a track without bars.
        if not self.bars:
            self._silences, self._silence_starts = [], []
            return
        loud = float(np.percentile(self._bar_loudness, 90))
        bar_starts = [bar['start'] for bar in self.bars]
        self._silences = []
        run = None
        for segment in self.analysis['segments']:
            bar = max(0, bisect.bisect_right(bar_starts, segment['start']) - 1)
            # The first bar has no bars before it, so it is its own reference.
            before = self._bar_peaks[max(0, bar - SILENCE_LOOKBACK_BARS):bar] or [self._bar_peaks[0]]
            local = float(np.median(before)) - SILENCE_LOCAL_DB
            if segment['loudness_max'] < min(loud - SILENCE_TRACK_DB, local):
                end = segment['start'] + segment['duration']
                run = (run[0], end, max(run[2], segment['loudness_max'])) if run else (segment['start'], end, segment['loudness_max'])
                continue
            if run and run[1] - run[0] >= SILENCE_MIN_S:
                self._silences.append(run)
            run = None
        if run and run[1] - run[0] >= SILENCE_MIN_S:
            self._silences.append(run)
        self._silence_starts = [start for start, _, _ in self._silences]
        for start, end, peak in self._silences:
            logger.debug(
                "Silence {:.2f}–{:.2f}s: loudest {:.1f} dB, at least {} dB under the loud bars ({:.1f} dB) and {} dB "
                "under the bars before, so the lights dim until the next hit{}",
                start, end, peak, SILENCE_TRACK_DB, loud, SILENCE_LOCAL_DB,
                f" and, at {end - start:.2f}s (at least {SILENCE_DARK_MIN_S}s), go dark" if end - start >= SILENCE_DARK_MIN_S else "",
            )

    def _silence_dim_level(self, start: float) -> float:
        """Brightness a silence starting at `start` falls to before any dark stage."""
        energy = self._section_energies[self._section_index_for_time(start)]
        return min(SILENCE_DIM_BRIGHTNESS, to_brightness(band_for(energy)[0]))

    def _silence_at(self, song_time: float) -> tuple[float, float, float] | None:
        index = bisect.bisect_right(self._silence_starts, song_time) - 1
        if index >= 0 and song_time < self._silences[index][1]:
            return self._silences[index]
        return None

    def _silence_starting_in(self, start: float, end: float) -> tuple[float, float, float] | None:
        index = bisect.bisect_right(self._silence_starts, start)
        if index < len(self._silences) and self._silences[index][0] < end:
            return self._silences[index]
        return None

    def _silence_ended_in(self, start: float, end: float) -> bool:
        index = bisect.bisect_right(self._silence_starts, end) - 1
        return index >= 0 and start < self._silences[index][1] <= end

    def _compute_flow_bars(self):
        """Bars without transients: soft segment onsets, beats Spotify only
        inferred from the tempo, and FLOW_QUIET_DB under the track's loud
        bars. Gaps shorter than FLOW_MIN_BARS inside a flow run are flow;
        runs shorter than it are not."""
        # The min_confidence filter can leave a track without bars.
        if not self.bars:
            self._flow_bars = set()
            return
        segments = self.analysis.get('segments', [])
        segment_starts = [s.get('start', 0.0) for s in segments]
        beats = self.analysis['beats']
        beat_starts = [b['start'] for b in beats]
        loud = float(np.percentile(self._bar_loudness, 90))
        onsets, confidences, flags = [], [], []
        for index, bar in enumerate(self.bars):
            start = bar['start']
            end = start + bar.get('duration', 0.0)
            in_bar = segments[bisect.bisect_left(segment_starts, start):bisect.bisect_left(segment_starts, end)]
            beats_in_bar = beats[bisect.bisect_left(beat_starts, start):bisect.bisect_left(beat_starts, end)]
            # A bar with no segments or no beats has nothing to pulse on.
            onset = float(np.mean([s['loudness_max'] - s['loudness_start'] for s in in_bar])) if in_bar else 0.0
            confidence = float(np.mean([b.get('confidence', 1.0) for b in beats_in_bar])) if beats_in_bar else 0.0
            onsets.append(onset)
            confidences.append(confidence)
            flags.append(
                onset < FLOW_MAX_ONSET_DB and confidence < FLOW_MAX_BEAT_CONFIDENCE
                and self._bar_loudness[index] < loud - FLOW_QUIET_DB
            )

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
        for first, last in bar_runs(self._flow_bars):
            span = range(first, last + 1)
            logger.debug(
                "Flow bars {}–{} ({:.1f}s): no transients to pulse on, so one slow drift per bar. Mean onset rise "
                "{:.1f} dB (flow under {}), beat confidence {:.2f} (under {}), {:.1f} dB under the loud bars (at least {})",
                first, last, self.bars[first]['start'],
                np.mean([onsets[i] for i in span]), FLOW_MAX_ONSET_DB,
                np.mean([confidences[i] for i in span]), FLOW_MAX_BEAT_CONFIDENCE,
                loud - np.mean([self._bar_loudness[i] for i in span]), FLOW_QUIET_DB,
            )
        too_loud = [
            index for index in range(len(self.bars))
            if onsets[index] < FLOW_MAX_ONSET_DB and confidences[index] < FLOW_MAX_BEAT_CONFIDENCE
            and self._bar_loudness[index] >= loud - FLOW_QUIET_DB
        ]
        if too_loud:
            logger.debug(
                "{} bars have soft onsets and unsure beats but sit within {} dB of the loud bars ({:.1f} dB), "
                "so they pulse: a loud mix leaves no headroom for an onset to rise into",
                len(too_loud), FLOW_QUIET_DB, loud,
            )

    def _compute_strobe_bars(self):
        """The first bar of each drop, unless it is a quiet or flow bar or its
        phrase is too short for a colour of its own."""
        bar_starts = [bar['start'] for bar in self.bars]
        self._strobe_bars = {}
        for index, part in enumerate(self._parts):
            if not part['drop']:
                continue
            bar = bisect.bisect_left(bar_starts, part['start'])
            if bar in self._quiet_bars:
                logger.debug("Drop part {} at {:.1f}s: no strobe, its first bar {} is quiet", index, part['start'], bar)
            elif bar in self._flow_bars:
                logger.debug("Drop part {} at {:.1f}s: no strobe, its first bar {} is a flow bar", index, part['start'], bar)
            elif self._section_index_for_time(bar_starts[bar]) in self._short_sections:
                logger.debug(
                    "Drop part {} at {:.1f}s: no strobe, its first phrase is too short for a colour of its own",
                    index, part['start'],
                )
            else:
                self._strobe_bars[bar] = index
                logger.debug("Drop part {} at {:.1f}s: strobe on its first bar {}", index, part['start'], bar)

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
        if not self.rules['track_fade']:
            return 1.0
        if self._fade_in_end > 0 and current_time < self._fade_in_end:
            return max(0.05, current_time / self._fade_in_end)
        if self._fade_out_start < float('inf') and current_time > self._fade_out_start:
            remaining = self._track_duration - self._fade_out_start
            if remaining > 0:
                return max(0.05, 1.0 - (current_time - self._fade_out_start) / remaining)
        return 1.0

    def _panel_brightness(self) -> float:
        """Brightness the lamps are at now, ramping over the last command's transition."""
        if self._trans_duration <= 0:
            return float(self.current_brightness)
        t = min(1.0, (time.monotonic() - self._trans_start_time) / self._trans_duration)
        return self._trans_start_bri + (self.current_brightness - self._trans_start_bri) * t

    def panel_state(self) -> tuple:
        """(hue, sat, bri) the lamps show now, as ints for the web app. The
        colour is where its crossing has got to: a crossing runs over a bar
        while the commands carrying it ramp over a fraction of a beat."""
        hue, saturation = self._color_at(time.monotonic())
        return int(hue), int(saturation), int(self._panel_brightness())

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
                self.rules['colour_changes']
                and important_boundary
                and target.get('confidence', 1.0) >= self.min_confidence
                and min(self._beat_starts[lead_beat], fade_start) <= current_time < target_start
            ):
                self._start_hue_crossing(target_idx, current_time, max(fade_start, current_time), target_start)

        # Sections are detected at their raw start; color changes are anchored
        # to the first analyzed bar in that section. This only fires when the
        # fade was not scheduled ahead (e.g. after a seek).
        if section_changed:
            self._log_phrase_start(current_section_index, current_time)
            self.current_section_index = current_section_index
            if (
                self.rules['colour_changes']
                and current_section_index in self.section_color_indexes
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

        # A near-silent gap blacks out on its own schedule, independent of the
        # beats: the gap rarely starts on one.
        silence_index = bisect.bisect_right(self._silence_starts, current_time + SILENCE_LEAD_S) - 1
        if (
            self.rules['silence']
            and silence_index >= 0
            and silence_index != self._silence_scheduled_for
            and self._silences[silence_index][1] > current_time
        ):
            self._silence_scheduled_for = silence_index
            start, end, _ = self._silences[silence_index]
            dim = self._silence_dim_level(start)
            fade_s = min(SILENCE_FADE_S, (end - start) / 3)
            dark = end - start >= SILENCE_DARK_MIN_S
            logger.debug(
                "Silence {:.2f}–{:.2f}s: fall to {:.1f}% over {:.2f}s{}",
                start, end, self._scale_brightness(dim), fade_s,
                f", then to 0% over {SILENCE_DARK_FADE_S}s" if dark else ", held until the next hit",
            )
            self._start_beat_task(self._scheduled_silence(
                time.monotonic() + max(0.0, start - current_time), fade_s, dim, dark,
            ))

        next_beat_idx = bisect.bisect_right(self._beat_starts, current_time)
        if next_beat_idx < len(self.beats) and next_beat_idx != self._beat_scheduled_for:
            next_beat = self.beats[next_beat_idx]
            fire_in = next_beat['start'] - current_time
            previous_beat_start = self._beat_starts[next_beat_idx - 1] if next_beat_idx else 0.0
            # The first beat after a silence relights whatever mode it is in.
            after_silence = self.rules['silence'] and self._silence_ended_in(previous_beat_start, next_beat['start'])
            if fire_in < 0.05:
                # Beat is essentially "now" — skip it; this happens during Spotify catch-up
                # bursts where song time jumps ahead rapidly and next beats appear stale.
                self._beat_scheduled_for = next_beat_idx
            elif self.rules['silence'] and self._silence_at(next_beat['start']):
                # The blackout owns beats inside a silence.
                self._beat_scheduled_for = next_beat_idx
            elif self.rules['strobe'] and self._beat_bar(next_beat_idx) in self._strobe_bars:
                # The strobe owns its whole bar: one task steps the colour on
                # every eighth note from this beat to the bar's end.
                self._beat_scheduled_for = next_beat_idx
                bar = self._beat_bar(next_beat_idx)
                self._enter_mode('strobe', bar, f"first bar of drop part {self._strobe_bars[bar]}")
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
                        "Bar {} strobe: {} colour steps, one per eighth note, held at the phrase's peak {:.1f}%",
                        bar, len(eighths), self._scale_brightness(peak),
                    )
                    self._cancel_strobe_task()
                    self._strobe_task = asyncio.create_task(self._scheduled_strobe(
                        [(now + (start - current_time), length) for start, length in eighths], peak,
                    ))
            elif self.rules['flow'] and self._beat_bar(next_beat_idx) in self._flow_bars:
                # Flow bars have no transients to pulse on: one command per
                # bar settles the brightness at the bar's loudness inside
                # the section's band over the rest of the bar, and drifts
                # the hue one palette step with it unless a section colour
                # crossing is already moving it.
                self._beat_scheduled_for = next_beat_idx
                bar = self._beat_bar(next_beat_idx)
                self._enter_mode('flow', bar, "no transients to pulse on: one slow drift per bar")
                if self._flow_scheduled_for != bar or after_silence:
                    self._flow_scheduled_for = bar
                    deadline = time.monotonic() + fire_in
                    length = self.bars[bar]['start'] + self.bars[bar].get('duration', 0.0) - next_beat['start']
                    bottom, top = band_for(energy_score)
                    rank = bisect.bisect_left(sorted(self._bar_loudness), self._bar_loudness[bar]) / max(len(self.bars) - 1, 1)
                    level = to_brightness((bottom + (top - bottom) * (0.5 + 0.5 * rank)) * fade)
                    if not self.rules['flow_hue_drift'] or self._color_at(deadline) != self._color_at(deadline + length):
                        logger.trace(
                            "Bar {} flow: settle at {:.1f}% over {:.2f}s, the phrase colour crossing moves the hue",
                            bar, self._scale_brightness(level), length,
                        )
                        self._start_beat_task(self._scheduled_hold(deadline, length, level))
                    else:
                        hue, saturation = self._flow_hue_sat()
                        logger.trace(
                            "Bar {} flow: drift to hue={} sat={} at {:.1f}% over {:.2f}s",
                            bar, hue, saturation, self._scale_brightness(level), length,
                        )
                        self._start_beat_task(self._scheduled_drift(deadline, length, level, hue, saturation))
            elif self.rules['quiet'] and self._beat_bar(next_beat_idx) in self._quiet_bars:
                # Quiet bars hold one level without pulsing: between dark and
                # the phrase's mid level, by how far under the bars before
                # they sit. The first beat of a quiet run settles there over
                # one beat, the rest stay still, except while a colour is
                # crossing: then every beat sends a hold so the crossing keeps
                # moving smoothly instead of snapping on the next pulse.
                self._beat_scheduled_for = next_beat_idx
                deadline = time.monotonic() + fire_in
                beat_len = next_beat.get('duration', 0.5)
                crossing = self._color_at(deadline) != self._color_at(deadline + beat_len)
                bar = self._beat_bar(next_beat_idx)
                bottom, top = band_for(energy_score)
                resting = to_brightness(self._quiet_bars[bar] * (bottom + top) / 2 * fade)
                self._enter_mode(
                    'quiet', bar,
                    f"breakdown: hold still at {self._scale_brightness(resting):.1f}%, "
                    f"{100 * self._quiet_bars[bar]:.0f}% of the phrase's mid level",
                )
                if (
                    crossing or after_silence or next_beat_idx == 0
                    or self._beat_bar(next_beat_idx - 1) not in self._quiet_bars
                ):
                    logger.trace("Beat {:>3} | quiet, hold at {:.1f}%", next_beat_idx, self._scale_brightness(resting))
                    self._start_beat_task(self._scheduled_hold(deadline, beat_len, resting))
            else:
                self._beat_scheduled_for = next_beat_idx
                self._enter_mode('pulse', self._beat_bar(next_beat_idx), "pulse on every beat")
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
                slow = max(0.0, min(1.0, (beat_len - 0.45) / 0.45)) if self.rules['slow_tempo_ramps'] else 0.0
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
                strength = pulse * (0.5 + 0.5 * self._segment_loudness(self.segments[seg_idx])) if self.rules['loudness_pulse'] else pulse
                low = to_brightness(bottom * fade)
                # A release landing after the blackout would relight the lamp.
                upcoming = self._silence_starting_in(next_beat['start'], next_beat['start'] + beat_len)
                if self.rules['silence'] and upcoming:
                    start, end, _ = upcoming
                    low = 0.0 if end - start >= SILENCE_DARK_MIN_S else min(low, self._silence_dim_level(start))
                high = to_brightness((bottom + (top - bottom) * strength) * fade)
                energy_color = ("blue", "cyan", "green", "yellow", "red")[energy_level]
                logger.opt(colors=True).trace(
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
            previous = (self.beats_per_measure, self.avg_measure_duration)
            self.min_segment_duration, self.beats_per_measure, self.avg_measure_duration = \
                min_segment_duration_from_future_measures(self.beats, self.bars, current_time)

            self.segments = merge_short_segments(self.analysis['segments'], min_duration=self.min_segment_duration)
            self._compute_loudness_stats()
            # Recomputed every bar; only a tempo or metre change is news.
            if self.beats_per_measure != previous[0] or abs(self.avg_measure_duration - previous[1]) > 0.05 * previous[1]:
                logger.debug(
                    "Tempo change at {:.1f}s: bar {:.3f}s → {:.3f}s, {} → {} beats per bar, "
                    "segments merged under {:.3f}s",
                    current_time, previous[1], self.avg_measure_duration, previous[0],
                    self.beats_per_measure, self.min_segment_duration,
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

    async def _scheduled_silence(self, deadline: float, fade_s: float, dim: float, dark: bool):
        await self._scheduled_hold(deadline, fade_s, dim)
        if dark:
            await self._scheduled_hold(deadline + fade_s, SILENCE_DARK_FADE_S, 0.0)

    async def _scheduled_hold(self, deadline: float, duration: float, resting: float):
        await asyncio.sleep(max(0.0, deadline - self._device_latency() - time.monotonic()))
        await self.set_parameters(duration, brightness=resting, deadline=deadline)

    async def _scheduled_drift(self, deadline: float, duration: float, level: float, hue: int, saturation: int):
        await asyncio.sleep(max(0.0, deadline - self._device_latency() - time.monotonic()))
        await self.set_parameters(
            duration, brightness=level, change_color=True,
            target_hue=hue, target_saturation=saturation, deadline=deadline,
        )

    def _enter_mode(self, mode: str, bar: int, why: str):
        """Log a switch between pulse, flow, quiet and strobe once, with its reason."""
        if mode == self.mode:
            return
        self.mode = mode
        logger.debug("Bar {} ({:.1f}s): {}, {}", bar, self.bars[bar]['start'], mode, why)

    def _log_phrase_start(self, index: int, current_time: float):
        energy = self._section_energies[index]
        level = max(0, min(4, round(energy * 4)))
        bottom, top = band_for(energy)
        first = self._section_first_beat[index]
        end = self._section_first_beat[index + 1] if index + 1 < len(self._section_first_beat) else len(self.beats)
        profile_index, profile = profile_for(energy, end - first)
        part = self._part_index_at(self._section_bar_starts[index])
        logger.info(
            "Phrase {} at {:.1f}s (part {}{}): energy {:.2f} → {} level {}, brightness band {:.1f}–{:.1f}%, "
            "profile {} ({}-beat cycle over {} beats)",
            index, current_time, part, ', drop' if self._parts[part]['drop'] else '', energy,
            ENERGY_NAMES[level], level, self._scale_brightness(to_brightness(bottom)),
            self._scale_brightness(to_brightness(top)), profile_index, len(profile['pulses']), end - first,
        )

    def _part_index_at(self, start: float) -> int:
        return bisect.bisect_right([part['start'] for part in self._parts], start) - 1

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
        logger.debug(
            "Colour {}/{} → {}/{} over {:.2f}s, landing on phrase {}'s first beat "
            "(a palette colour 40–{} degrees away, so the crossing reads as a change without sweeping the wheel)",
            start_hue, start_sat, hue, saturation, end - start, section_idx, MAX_HUE_STEP,
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
        self.mode = ""
        self._silence_scheduled_for = -1
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
        self._trans_start_bri = self._panel_brightness()
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
                for device in self.devices if device.ip not in self.disabled_devices
            ))
        elif brightness is not None:
            await asyncio.gather(*(
                device.set_brightness(
                    new_brightness, duration=duration,
                    dispatch_at=(deadline - device.estimated_latency - self.sync_offset) if deadline is not None else None,
                )
                for device in self.devices if device.ip not in self.disabled_devices
            ))
        await asyncio.sleep(0)

    async def identify(self, ip: str):
        """Blink one light white so it can be found in the room."""
        device = next(device for device in self.devices if device.ip == ip)
        logger.info("Identifying light {} ({})", ip, device.model)
        for _ in range(IDENTIFY_BLINKS):
            await device.set_hsv(0, 0, 100, duration=0.05)
            await asyncio.sleep(IDENTIFY_BLINK_S)
            # 1%, not 0: Yeelight rejects a brightness of 0.
            await device.set_hsv(0, 0, 1, duration=0.05)
            await asyncio.sleep(IDENTIFY_BLINK_S)
        # The lamp is white now; the next command has to carry the colour.
        self._sent_color = None

    def set_device_enabled(self, ip: str, enabled: bool):
        if enabled:
            self.disabled_devices.discard(ip)
            # The lamp missed the colours sent while it was off; the next
            # command has to carry the colour, not just brightness.
            self._sent_color = None
        else:
            self.disabled_devices.add(ip)
        logger.info("Light {} switched {} from the web app", ip, "on" if enabled else "off")

    def timeline(self) -> dict:
        """Everything the web app draws for the loaded track, in song seconds."""
        def bar_span(bar: int) -> tuple[float, float]:
            return self.bars[bar]['start'], self.bars[bar]['start'] + self.bars[bar]['duration']

        return {
            'duration': self._track_duration,
            'beats': self._beat_starts,
            'bars': [bar['start'] for bar in self.bars],
            'parts': [
                {'start': part['start'], 'end': part['start'] + part['duration'], 'energy': energy,
                 'drop': part['drop'], 'why': part['drop_why']}
                for part, energy in zip(self._parts, self._part_energies)
            ],
            'phrases': [
                {'start': section['start'], 'end': section['start'] + section['duration'],
                 'energy': energy, 'short': index in self._short_sections}
                for index, (section, energy) in enumerate(zip(self.sections, self._section_energies))
            ],
            'quiet': [[*bar_span(bar), level] for bar, level in sorted(self._quiet_bars.items())],
            'flow': [list(bar_span(bar)) for bar in sorted(self._flow_bars)],
            'strobe': [list(bar_span(bar)) for bar in sorted(self._strobe_bars)],
            'silences': [[start, end] for start, end, _ in self._silences],
            # Spotify gives no audio, only per-segment loudness: onset, peak
            # and when the peak lands. The page draws its envelope.
            'segments': [
                [s['start'], s['duration'], s['loudness_start'], s['loudness_max'], s['loudness_max_time']]
                for s in self.analysis['segments']
            ],
            'fade_in_end': self._fade_in_end,
            'fade_out_start': min(self._fade_out_start, self._track_duration),
        }

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
