import asyncio
import bisect
import colorsys
import json
import time
import math
from enum import Enum
from typing import List, Optional
from dotenv import load_dotenv
from loguru import logger
from models import EventSongChanged, EventAdjustProgressTime, EventStop
from utils import (
    get_next_item,
    CONTROLLER_TICK,
    MIN_ANALYSIS_CONFIDENCE,
    get_vibrant_color,
    merge_short_segments,
    min_segment_duration_from_future_measures,
)
import random
import numpy as np

from light_device import LightDevice


class Mode(Enum):
    ATMOSPHERIC = "atmospheric"
    PULSE       = "pulse"
    STROBE      = "strobe"
    BREATHE     = "breathe"
    SOLID       = "solid"
    GROOVE      = "groove"    # segment loudness delivered on beat boundaries
    GENERAL     = "general"   # phrase-building envelope: spikes grow over breathe_period bars


# Circle of fifths: pitch class → hue (degrees, 0–360)
# C, G, D, A, E, B, F#, Db, Ab, Eb, Bb, F
_KEY_TO_HUE = {0: 0, 7: 30, 2: 60, 9: 90, 4: 120, 11: 150,
               6: 180, 1: 210, 8: 240, 3: 270, 10: 300, 5: 330}


class LightsController:
    def __init__(self, devices: List[LightDevice], events_queue: asyncio.Queue):
        self.devices = devices
        self.events_queue = events_queue
        self.last_section_num_next = 0
        self.last_index = -1
        self.current_hue = random.randint(0, 359)
        self.current_saturation = random.randint(50, 80)
        self.current_brightness = 0
        self.sections = []
        self.last_next_bar = {}
        self.last_next_segment = None
        self.current_section = None
        self.current_section_index = -1
        self.analysis = None
        self.current_progress = 0
        self.section_color_indexes = set()
        self._section_color_target_count = 10
        self.min_segment_duration = 0.1
        self.beats_per_measure = 4
        self.avg_measure_duration = 0.5
        self._loudness_min = 0.0
        self._loudness_max = 1.0
        self._segment_starts: list = []
        self._last_segment_idx = -1
        self._section_energies: list = []
        self._section_energy_classes: list = []
        self._section_timbres: list = []
        self._beat_starts: list = []
        self._last_beat_idx = -1
        self.mode: Mode = Mode.ATMOSPHERIC
        self.color_trigger: str = 'energy'  # 'energy' | 'section' | 'key'
        self._last_section_key: int = -2
        self._last_mode: Optional[Mode] = None
        self.current_song: str = ""
        self._tatum_starts: list = []
        self._fade_in_end: float = 0.0
        self._fade_out_start: float = float('inf')
        self._track_duration: float = 0.0
        self._beat_task: Optional[asyncio.Task] = None
        self._beat_scheduled_for: int = -1
        self._seg_task: Optional[asyncio.Task] = None
        self._seg_scheduled_for: int = -1
        self._pre_section_trans_for: int = -1   # section index we've pre-transitioned for
        self._pre_section_target_hue: int = 0
        self._pre_section_target_sat: int = 80

        # Tunable — override from main.py after construction
        self.max_brightness: int = 100   # ceiling in %; all computed brightness values are scaled to this
        self.min_confidence: float = MIN_ANALYSIS_CONFIDENCE
        self.sync_offset: float = 0.0    # extra seconds to fire commands early (+) or late (-)
        self.breathe_period: int = 4     # bars per breathe cycle
        self.strobe_duty: float = 0.15   # strobe on-time as fraction of beat duration
        self.atmospheric_pulse_on: int = 2   # beats to pulse in atmospheric mode
        self.atmospheric_pulse_off: int = 2  # beats to silence after each pulse group

        # Panel animation: interpolate displayed color over the transition duration
        self._trans_start_hue: float = float(self.current_hue)
        self._trans_start_sat: float = float(self.current_saturation)
        self._trans_start_bri: float = float(self.current_brightness)
        self._trans_start_time: float = 0.0
        self._trans_duration: float = 0.0

        # Color-only transition state — used to lerp cmd_hue/sat during beat pulses
        # so brightness-only commands don't snap to the target color mid-transition.
        self._color_trans_start_hue: float = float(self.current_hue)
        self._color_trans_start_sat: float = float(self.current_saturation)
        self._color_trans_start_time: float = 0.0
        self._color_trans_duration: float = 0.0
        self._last_timing_log_second = -1

        self._current_params = {
            'hue': self.current_hue,
            'saturation': self.current_saturation,
            'brightness': self.current_brightness
        }

    async def control_lights(self):
        event = EventStop()
        while True:
            event = await self.events_queue.get()
            if isinstance(event, EventSongChanged):
                logger.debug("Loading analysis for {}", event.track_id or event.song_name or 'unknown track')
                self.handle_song_changed(event)
            elif isinstance(event, EventAdjustProgressTime):
                if self.analysis is not None:
                    self.current_progress = event.progress_time_ms
                    await self.handle_adjust_progress(self.current_progress)
            elif isinstance(event, EventStop):
                logger.warning("Song stopped!")
                self._cancel_beat_task()
                self._cancel_seg_task()
                self.analysis = None
            self.events_queue.task_done()
            await asyncio.sleep(CONTROLLER_TICK)

    def handle_song_changed(self, event: EventSongChanged):
        self.analysis = event.analysis
        with open("analysis.json", "w") as f:
            json.dump(self.analysis, f, indent=4)

        conf = self.min_confidence
        self.sections = self.analysis['sections']
        self.bars = sorted(
            [b for b in self.analysis['bars'] if b.get('confidence', 1.0) >= conf],
            key=lambda x: x['start'],
        )
        self.beats = sorted(
            [b for b in self.analysis['beats'] if b.get('confidence', 1.0) >= conf],
            key=lambda x: x['start'],
        )
        self._beat_starts = [b['start'] for b in self.beats]
        self._last_beat_idx = -1
        self._cancel_beat_task()
        self._cancel_seg_task()
        self._beat_scheduled_for = -1
        self._seg_scheduled_for = -1
        self._tatum_starts = [t['start'] for t in self.analysis.get('tatums', []) if t.get('confidence', 1.0) >= conf]
        self.current_song = event.song_name

        track = self.analysis.get('track', {})
        logger.info(
            "Analysis saved: {} — {} | duration={:.2f}s | start progress={:.2f}s | "
            "sections={} bars={} beats={} segments={}",
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

    def _compute_section_energies(self):
        if not self.sections:
            self._section_energies = []
            self._section_energy_classes = []
            return
        loudnesses = np.array([s.get('loudness', -20.0) for s in self.sections])
        order = np.argsort(loudnesses)
        scores = np.empty(len(order), dtype=float)
        n = len(order)
        for rank, idx in enumerate(order):
            scores[idx] = rank / max(n - 1, 1)
        self._section_energies = scores.tolist()
        self._section_energy_classes = [
            'low' if s < 0.33 else ('high' if s >= 0.67 else 'medium')
            for s in self._section_energies
        ]
        logger.debug(
            "Section energy classes: {}",
            ' '.join(f"{i}:{c[0].upper()}" for i, c in enumerate(self._section_energy_classes))
        )

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

    def map_brightness(self, segment, section_energy_score: float = 0.5) -> int:
        next_loudness = 10**(segment["loudness_start"] / 20)
        if self._loudness_max == self._loudness_min:
            raw = 0.5
        else:
            raw = (next_loudness - self._loudness_min) / (self._loudness_max - self._loudness_min)
        raw = max(0.0, min(1.0, raw))
        floor = max(1, 5 + int(section_energy_score * 25))
        ceiling = 55 + int(section_energy_score * 45)
        brightness = int(floor * (ceiling / floor) ** raw)
        return max(1, min(100, brightness))

    def _map_brightness_raw(self, raw: float, energy_score: float) -> int:
        raw = max(0.0, min(1.0, raw))
        floor = max(1, 5 + int(energy_score * 25))
        ceiling = 55 + int(energy_score * 45)
        brightness = int(floor * (ceiling / floor) ** raw)
        return max(1, min(100, brightness))

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
        """Return (hue, saturation) by cycling the vibrant palette away from the current hue."""
        return get_vibrant_color(self._current_params['hue'])

    async def handle_adjust_progress(self, current_time: float):
        next_bar = get_next_item(self.bars, current_time)
        if not next_bar or not self._segment_starts:
            logger.debug("skipping adjust progress")
            return

        current_section_index = self._section_index_for_time(current_time)
        if current_section_index is None:
            logger.debug("skipping adjust progress: no section found")
            return

        timing_second = int(current_time)
        if timing_second // 5 != self._last_timing_log_second // 5:
            bar_index = max(0, bisect.bisect_right([b['start'] for b in self.bars], current_time) - 1)
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

        seg_idx = max(0, bisect.bisect_right(self._segment_starts, current_time) - 1)
        seg_idx = min(seg_idx, len(self.segments) - 1)

        tasks = []

        energy_score = self._section_energies[current_section_index] if self._section_energies else 0.5
        fade = self._fade_multiplier(current_time)

        # Detect mode change (heuristic recolor trigger)
        mode_changed = self._last_mode is not None and self.mode != self._last_mode
        self._last_mode = self.mode

        section = self.sections[current_section_index]
        section_changed = self.current_section_index != current_section_index
        current_key = section.get('key', -1)
        key_changed = self.color_trigger == 'key' and current_key != self._last_section_key
        if section_changed:
            self._last_section_key = current_key

        # Pre-section color transition: start fading to the next section's color
        # one bar early so the light arrives at the new color exactly at the boundary.
        next_section_idx = current_section_index + 1
        if (
            next_section_idx < len(self.sections)
            and self._beat_starts
            and self._pre_section_trans_for != next_section_idx
            and not section_changed
        ):
            next_section = self.sections[next_section_idx]
            next_section_start = next_section.get('start', 0.0)

            next_energy_changed = next_section_idx in self.section_color_indexes
            next_bar_aligned = self._section_aligns_with_bar(next_section)
            next_key = next_section.get('key', -1)
            next_should_change = (
                (self.color_trigger == 'energy' and next_energy_changed)
                or (self.color_trigger == 'energy_bar' and next_energy_changed and next_bar_aligned)
                or self.color_trigger == 'section'
                or (self.color_trigger == 'key' and next_key != self._last_section_key)
            ) and next_section.get('confidence', 1.0) >= self.min_confidence

            if next_should_change:
                idx_at_section = bisect.bisect_left(self._beat_starts, next_section_start)
                target_beat_idx = max(0, idx_at_section - self.beats_per_measure)
                pre_trans_start = self._beat_starts[target_beat_idx]

                if pre_trans_start <= current_time < next_section_start:
                    time_remaining = next_section_start - current_time
                    target_hue, target_sat = self._section_hue_sat(next_section)
                    self._pre_section_trans_for = next_section_idx
                    self._pre_section_target_hue = target_hue
                    self._pre_section_target_sat = target_sat
                    tasks.append(asyncio.create_task(
                        self.set_parameters(
                            duration=time_remaining,
                            change_color=True,
                            target_hue=target_hue,
                            target_saturation=target_sat,
                        )
                    ))
                    logger.info(
                        "Pre-section → hue={} sat={} over {:.2f}s (section {} in {:.2f}s)",
                        target_hue, target_sat, time_remaining,
                        next_section_idx, time_remaining,
                    )

        # At section boundary: handle brightness and color updates.
        if section_changed or mode_changed:
            energy_changed = current_section_index in self.section_color_indexes
            bar_aligned = self._section_aligns_with_bar(section)
            should_change_color = mode_changed or (
                (
                    (self.color_trigger == 'energy' and energy_changed)
                    or (self.color_trigger == 'energy_bar' and energy_changed and bar_aligned)
                    or self.color_trigger == 'section'
                    or (self.color_trigger == 'key' and key_changed)
                ) and section.get('confidence', 1.0) >= self.min_confidence
            )
            # If we pre-transitioned this section's color, don't fire a second color change.
            # (mode_changed overrides this to ensure the new mode's color applies.)
            if not mode_changed and self._pre_section_trans_for == current_section_index:
                should_change_color = False
            section_duration = max(section.get('duration', 1.0), 0.5)
            section_transition_time = max(0.8, min(self.avg_measure_duration * 2, 3.0))

            if self.mode == Mode.SOLID:
                brightness = max(1, int(self._map_brightness_raw(energy_score, energy_score) * fade))
                if should_change_color:
                    target_hue, target_saturation = self._section_hue_sat(section)
                else:
                    target_hue, target_saturation = None, None
                tasks.append(asyncio.create_task(
                    self.set_parameters(
                        duration=section_transition_time,
                        change_color=should_change_color,
                        brightness=brightness,
                        target_hue=target_hue,
                        target_saturation=target_saturation,
                    )
                ))
                action = "recolor + brightness" if should_change_color else "brightness only"
                logger.info(
                    "Section change → {} (index {}, energy={:.2f} [{}])",
                    action, current_section_index, energy_score,
                    self._section_energy_classes[current_section_index] if self._section_energy_classes else '?',
                )
            elif should_change_color:
                target_hue, target_saturation = self._section_hue_sat(section)
                brightness = max(1, int(self.map_brightness(self.segments[seg_idx], energy_score) * fade))
                tasks.append(asyncio.create_task(
                    self.set_parameters(
                        duration=section_transition_time,
                        change_color=True,
                        brightness=brightness,
                        target_hue=target_hue,
                        target_saturation=target_saturation,
                    )
                ))
                _r, _g, _b = colorsys.hsv_to_rgb(target_hue / 360, target_saturation / 100, 1.0)
                _sw = f"\033[48;2;{int(_r*255)};{int(_g*255)};{int(_b*255)}m   \033[0m"
                logger.info(
                    "Section change → recolor {} (index {}, energy={:.2f} [{}])",
                    _sw, current_section_index, energy_score,
                    self._section_energy_classes[current_section_index] if self._section_energy_classes else '?',
                )
            self.current_section_index = current_section_index

        # Per-mode brightness logic
        if self.mode == Mode.PULSE and self._beat_starts:
            next_beat_idx = bisect.bisect_right(self._beat_starts, current_time)
            if next_beat_idx < len(self.beats) and next_beat_idx != self._beat_scheduled_for:
                self._cancel_beat_task()
                self._beat_scheduled_for = next_beat_idx
                next_beat = self.beats[next_beat_idx]
                time_until = next_beat['start'] - current_time
                fire_in = max(0.0, time_until - self._device_latency())
                base = max(1, int(self.map_brightness(self.segments[seg_idx], energy_score) * fade))
                spike = min(100, base + int(energy_score * 35))
                self._beat_task = asyncio.create_task(
                    self._scheduled_beat_pulse(fire_in, spike, base, next_beat.get('duration', 0.5))
                )
            self._last_segment_idx = seg_idx

        elif self.mode == Mode.STROBE and self._beat_starts:
            next_beat_idx = bisect.bisect_right(self._beat_starts, current_time)
            if next_beat_idx < len(self.beats) and next_beat_idx != self._beat_scheduled_for:
                self._cancel_beat_task()
                self._beat_scheduled_for = next_beat_idx
                next_beat = self.beats[next_beat_idx]
                time_until = next_beat['start'] - current_time
                fire_in = max(0.0, time_until - self._device_latency())
                spike = max(1, int(min(100, 85 + int(energy_score * 15)) * fade))
                base = max(1, int(max(1, int(energy_score * 8)) * fade))
                self._beat_task = asyncio.create_task(
                    self._scheduled_strobe_pulse(fire_in, spike, base, next_beat.get('duration', 0.5))
                )
            self._last_segment_idx = seg_idx

        elif self.mode == Mode.BREATHE:
            period = self.avg_measure_duration * self.breathe_period
            if period > 0:
                phase = (current_time % period) / period
                raw = (math.sin(2 * math.pi * phase - math.pi / 2) + 1) / 2
                brightness = max(1, int(self._map_brightness_raw(raw, energy_score) * fade))
                tasks.append(asyncio.create_task(
                    self.set_parameters(0.15, brightness=brightness)
                ))
            self._last_segment_idx = seg_idx

        elif self.mode == Mode.SOLID:
            self._last_segment_idx = seg_idx

        elif self.mode == Mode.GROOVE and self._beat_starts:
            # Segment loudness drives brightness, but changes are quantized to beat boundaries.
            # Reschedule whenever the target beat OR the segment (brightness value) changes.
            next_beat_idx = bisect.bisect_right(self._beat_starts, current_time)
            seg_changed = seg_idx != self._last_segment_idx
            self._last_segment_idx = seg_idx
            if next_beat_idx < len(self.beats) and (next_beat_idx != self._beat_scheduled_for or seg_changed):
                self._cancel_beat_task()
                self._beat_scheduled_for = next_beat_idx
                next_beat = self.beats[next_beat_idx]
                time_until = next_beat['start'] - current_time
                brightness = max(1, int(self.map_brightness(self.segments[seg_idx], energy_score) * fade))
                transition_duration = max(0.05, next_beat.get('duration', 0.5) * 0.8)
                fire_in = max(0.0, time_until - self._device_latency() - transition_duration)
                self._beat_task = asyncio.create_task(
                    self._scheduled_brightness(fire_in, transition_duration, brightness)
                )

        elif self.mode == Mode.GENERAL and self._beat_starts:
            next_beat_idx = bisect.bisect_right(self._beat_starts, current_time)
            if next_beat_idx < len(self.beats) and next_beat_idx != self._beat_scheduled_for:
                self._cancel_beat_task()
                self._beat_scheduled_for = next_beat_idx
                next_beat = self.beats[next_beat_idx]
                fire_in = max(0.0, next_beat['start'] - current_time - self._device_latency())

                # Phrase envelope: spike intensity builds over breathe_period bars then resets.
                # Raised-cosine curve: smooth build from ~0.2 → 1.0 over the phrase.
                phrase_beats = max(1, self.beats_per_measure * self.breathe_period)
                beat_in_phrase = next_beat_idx % phrase_beats
                t = beat_in_phrase / max(1, phrase_beats - 1)
                envelope = 0.2 + 0.8 * (0.5 - 0.5 * math.cos(math.pi * t))
                # Extra kick on the final beat of the phrase (the release point)
                if beat_in_phrase == phrase_beats - 1:
                    envelope = min(1.0, envelope * 1.15)
                # Subtle downbeat accent
                if next_beat_idx % max(1, self.beats_per_measure) == 0:
                    envelope = min(1.0, envelope * 1.1)

                # Base tracks segment loudness; spike applies the phrase envelope on top
                seg_brightness = max(1, int(self.map_brightness(self.segments[seg_idx], energy_score) * fade))
                base = max(1, int(seg_brightness * 0.2))
                spike = max(base + 1, int(seg_brightness * envelope))
                self._beat_task = asyncio.create_task(
                    self._scheduled_beat_pulse(fire_in, spike, base, next_beat.get('duration', 0.5))
                )
            self._last_segment_idx = seg_idx

        else:  # ATMOSPHERIC
            self._last_segment_idx = seg_idx
            if self._beat_starts:
                next_beat_idx = bisect.bisect_right(self._beat_starts, current_time)
                if next_beat_idx < len(self.beats) and next_beat_idx != self._beat_scheduled_for:
                    self._cancel_beat_task()
                    self._beat_scheduled_for = next_beat_idx
                    next_beat = self.beats[next_beat_idx]
                    fire_in = max(0.0, next_beat['start'] - current_time - self._device_latency())

                    # Accent cycle: pulse_on beats get full spike, pulse_off beats get a subtle bump.
                    # Every beat fires — no gaps — so the rhythm stays locked to the track.
                    n_on = max(1, self.atmospheric_pulse_on)
                    n_off = max(0, self.atmospheric_pulse_off)
                    phase = next_beat_idx % max(1, n_on + n_off)
                    in_accent = phase < n_on

                    # Spike magnitude driven by segment loudness for organic per-beat variation.
                    seg_spike = max(1, int(self.map_brightness(self.segments[seg_idx], energy_score) * fade))
                    # Downbeat (beat 0 of each measure) gets a subtle boost.
                    if next_beat_idx % max(1, self.beats_per_measure) == 0:
                        seg_spike = min(100, int(seg_spike * 1.2))

                    low  = max(1, int((1 + energy_score * 5) * fade))
                    high = max(low + 1, seg_spike if in_accent else int(seg_spike * 0.25))
                    self._beat_task = asyncio.create_task(
                        self._scheduled_beat_pulse(fire_in, high, low, next_beat.get('duration', 0.5))
                    )

        # Bar transition → recompute min-segment duration to track tempo changes
        if self.last_next_bar != next_bar and next_bar['confidence'] > 0.8:
            next_bar_duration = (next_bar["duration"] - (current_time - next_bar['start']))
            logger.warning(
                f"Transitioning from bar {self.bars.index(self.last_next_bar)} "
                f"to bar {self.bars.index(next_bar)} in {next_bar_duration:.2f}s"
            )

            self.min_segment_duration, self.beats_per_measure, self.avg_measure_duration = \
                min_segment_duration_from_future_measures(self.beats, self.bars, current_time)

            self.segments = merge_short_segments(self.analysis['segments'], min_duration=self.min_segment_duration)
            self._cancel_seg_task()
            self._seg_scheduled_for = -1
            self._compute_loudness_stats()
            logger.debug(
                f"Recomputed MIN_SEGMENT_DURATION={self.min_segment_duration:.3f}s "
                f"(avg_measure={self.avg_measure_duration:.3f}s, beats/measure={self.beats_per_measure}) "
                f"at t={current_time:.2f}s"
            )
            self.last_next_bar = next_bar

        if tasks:
            await asyncio.gather(*tasks)

    def _section_index_for_time(self, current_time: float) -> int | None:
        if not self.sections:
            return None
        for idx, section in enumerate(self.sections):
            section_end = section.get('start', 0.0) + section.get('duration', 0.0)
            if current_time < section_end:
                return idx
        return len(self.sections) - 1

    def _build_section_color_indexes(self, section_count: int) -> set[int]:
        if section_count == 0:
            return set()
        if section_count == 1:
            return {0}

        # Score each boundary between adjacent sections using multiple features.
        # Higher score = more structurally distinct transition (verse→chorus, etc.)
        scored = []
        for i in range(1, section_count):
            s_prev = self.sections[i - 1]
            s_curr = self.sections[i]

            loud_diff  = abs(s_prev.get('loudness', -20.0) - s_curr.get('loudness', -20.0)) / 20.0
            tempo_diff = abs(s_prev.get('tempo', 120.0)    - s_curr.get('tempo', 120.0))    / 30.0
            key_diff   = float(s_prev.get('key', -1)  != s_curr.get('key', -1))
            mode_diff  = float(s_prev.get('mode', -1) != s_curr.get('mode', -1))

            if self._section_timbres and i < len(self._section_timbres):
                t_prev = self._section_timbres[i - 1]
                t_curr = self._section_timbres[i]
                norm = np.linalg.norm(t_prev) * np.linalg.norm(t_curr)
                cos_dist = max(0.0, 1.0 - float(np.dot(t_prev, t_curr)) / norm) if norm > 1e-8 else 0.0
            else:
                cos_dist = 0.0

            score = (
                0.30 * min(1.0, loud_diff)
                + 0.15 * min(1.0, tempo_diff)
                + 0.15 * key_diff
                + 0.10 * mode_diff
                + 0.30 * cos_dist
            )
            scored.append((i, score))

        # Pick the top N highest-scoring boundaries; section 0 always triggers.
        target = max(1, self._section_color_target_count - 1)
        scored.sort(key=lambda x: x[1], reverse=True)
        indexes = {0} | {i for i, _ in scored[:target]}

        logger.debug(
            "Section boundary scores: {}",
            ' '.join(
                f"{i}:{'*' if i in indexes else ''}{s:.2f}"
                for i, s in sorted(scored, key=lambda x: x[0])
            )
        )
        return indexes

    async def _send_beat_pulse(self, spike: int, base: int, beat_duration: float):
        await self.set_parameters(0.05, brightness=spike)
        await asyncio.sleep(max(0.05, beat_duration * 0.2))
        await self.set_parameters(max(0.1, beat_duration * 0.6), brightness=base)

    async def _scheduled_beat_pulse(self, fire_in: float, spike: int, base: int, beat_duration: float):
        if fire_in > 0:
            await asyncio.sleep(fire_in)
        await self._send_beat_pulse(spike, base, beat_duration)

    async def _scheduled_strobe_pulse(self, fire_in: float, spike: int, base: int, beat_duration: float):
        if fire_in > 0:
            await asyncio.sleep(fire_in)
        await self.set_parameters(0.02, brightness=spike)
        await asyncio.sleep(max(0.02, beat_duration * self.strobe_duty))
        await self.set_parameters(0.02, brightness=base)

    async def _scheduled_brightness(self, fire_in: float, duration: float, brightness: int):
        if fire_in > 0:
            await asyncio.sleep(fire_in)
        await self.set_parameters(duration, brightness=brightness)

    def _device_latency(self) -> float:
        base = max(d.estimated_latency for d in self.devices) if self.devices else 0.02
        return base + self.sync_offset

    def _section_aligns_with_bar(self, section: dict, tolerance: float = 0.3) -> bool:
        """Returns True if the section's start time is within `tolerance` seconds of a bar start."""
        if not self.bars:
            return True
        sec_start = section.get('start', 0.0)
        bar_starts = [b['start'] for b in self.bars]
        idx = bisect.bisect_left(bar_starts, sec_start)
        candidates = bar_starts[max(0, idx - 1):idx + 2]
        if not candidates:
            return True
        return min(abs(x - sec_start) for x in candidates) <= tolerance

    def _cancel_beat_task(self):
        if self._beat_task and not self._beat_task.done():
            self._beat_task.cancel()
        self._beat_task = None

    def _cancel_seg_task(self):
        if self._seg_task and not self._seg_task.done():
            self._seg_task.cancel()
        self._seg_task = None

    async def set_parameters(
        self,
        duration: float = 0.05,
        brightness: int | None = None,
        change_color: bool = False,
        wait_before_ms: int = 0,
        target_hue: int | None = None,
        target_saturation: int | None = None,
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
            new_brightness = max(1, round(brightness * self.max_brightness / 100))
        else:
            new_brightness = self._current_params['brightness']

        params_changed = (
            new_hue != self._current_params['hue'] or
            new_saturation != self._current_params['saturation'] or
            new_brightness != self._current_params['brightness']
        )

        if not params_changed or (not change_color and brightness is None):
            return

        now = time.monotonic()

        if change_color:
            # Record current interpolated color position as the start of the new transition
            if self._color_trans_duration > 0:
                t = min(1.0, (now - self._color_trans_start_time) / self._color_trans_duration)
                diff = ((self._current_params['hue'] - self._color_trans_start_hue) + 180) % 360 - 180
                self._color_trans_start_hue = (self._color_trans_start_hue + diff * t) % 360
                diff_sat = self._current_params['saturation'] - self._color_trans_start_sat
                self._color_trans_start_sat = self._color_trans_start_sat + diff_sat * t
            else:
                self._color_trans_start_hue = float(self._current_params['hue'])
                self._color_trans_start_sat = float(self._current_params['saturation'])
            self._color_trans_start_time = now
            self._color_trans_duration = duration
            cmd_hue = new_hue
            cmd_sat = new_saturation
        else:
            # Lerp hue/sat to the current transition position so brightness-only
            # beat commands don't snap the bulb to the target color mid-transition.
            if self._color_trans_duration > 0:
                t = min(1.0, (now - self._color_trans_start_time) / self._color_trans_duration)
                diff = ((self._current_params['hue'] - self._color_trans_start_hue) + 180) % 360 - 180
                cmd_hue = int((self._color_trans_start_hue + diff * t) % 360)
                diff_sat = self._current_params['saturation'] - self._color_trans_start_sat
                cmd_sat = int(self._color_trans_start_sat + diff_sat * t)
            else:
                cmd_hue = self._current_params['hue']
                cmd_sat = self._current_params['saturation']

        # Capture current interpolated position as the start of the panel animation
        ph, ps, pb = self._panel_interp()
        self._trans_start_hue = ph
        self._trans_start_sat = ps
        self._trans_start_bri = pb
        self._trans_start_time = now
        self._trans_duration = duration

        if change_color:
            self._current_params['hue'] = new_hue
            self._current_params['saturation'] = new_saturation
        if brightness is not None:
            self._current_params['brightness'] = new_brightness

        self.current_hue = self._current_params['hue']
        self.current_saturation = self._current_params['saturation']
        self.current_brightness = self._current_params['brightness']

        logger.debug(f"Setting parameters: duration={duration:.2f}s, brightness={new_brightness}%, hue={cmd_hue}, saturation={cmd_sat}")

        set_tasks = []
        for device in self.devices:
            if change_color:
                set_tasks.append(device.set_hsv(cmd_hue, cmd_sat, new_brightness, duration=duration))
            elif brightness is not None:
                set_tasks.append(device.set_brightness(new_brightness, duration=duration))

        await asyncio.sleep(wait_before_ms)
        await asyncio.gather(*set_tasks)
        await asyncio.sleep(0)
