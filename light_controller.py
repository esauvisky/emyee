import asyncio
import bisect
import json
import time
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

from atmospheric import profile_for, pulses_for
from light_device import LightDevice
from sections import detect_sections

class LightsController:
    def __init__(self, devices: List[LightDevice], events_queue: asyncio.Queue):
        self.devices = devices
        self.events_queue = events_queue
        self._pending_event = None
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
        self.min_segment_duration = 0.1
        self.beats_per_measure = 4
        self.avg_measure_duration = 0.5
        self._loudness_min = 0.0
        self._loudness_max = 1.0
        self._segment_starts: list = []
        self._section_energies: list = []
        self._section_timbres: list = []
        self._beat_starts: list = []
        self.current_song: str = ""
        self._tatum_starts: list = []
        self._fade_in_end: float = 0.0
        self._fade_out_start: float = float('inf')
        self._track_duration: float = 0.0
        self._section_bar_starts: list = []
        self._last_segment_idx = -1
        self._beat_task: Optional[asyncio.Task] = None
        self._beat_scheduled_for: int = -1
        self._section_color_task: Optional[asyncio.Task] = None
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
                self._cancel_section_color_task()
                self.analysis = None
            self.events_queue.task_done()
            await asyncio.sleep(CONTROLLER_TICK)

    def handle_song_changed(self, event: EventSongChanged):
        self.analysis = event.analysis
        with open("analysis.json", "w") as f:
            json.dump(self.analysis, f, indent=4)

        conf = self.min_confidence
        self.bars = sorted(
            [b for b in self.analysis['bars'] if b.get('confidence', 1.0) >= conf],
            key=lambda x: x['start'],
        )
        self.sections = detect_sections(self.analysis, self.bars)
        logger.debug(
            "Custom section starts: {}",
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
        self._cancel_beat_task()
        self._cancel_section_color_task()
        self._beat_scheduled_for = -1
        self._tatum_starts = [t['start'] for t in self.analysis.get('tatums', []) if t.get('confidence', 1.0) >= conf]
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
            return
        loudnesses = np.array([section.get('loudness', -20.0) for section in self.sections])
        order = np.argsort(loudnesses)
        scores = np.empty(len(order), dtype=float)
        for rank, index in enumerate(order):
            scores[index] = rank / max(len(order) - 1, 1)
        self._section_energies = scores.tolist()
        logger.debug(
            "Section energies: {}",
            ' '.join(f"{i}:{energy:.2f}" for i, energy in enumerate(self._section_energies))
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
        loudness = 10 ** (segment["loudness_start"] / 20)
        span = self._loudness_max - self._loudness_min
        raw = 0.5 if span == 0 else (loudness - self._loudness_min) / span
        raw = max(0.0, min(1.0, raw))
        floor = max(1, 5 + int(section_energy_score * 25))
        ceiling = 55 + int(section_energy_score * 45)
        return max(1, min(100, int(floor * (ceiling / floor) ** raw)))

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

        tasks = []

        section = self.sections[current_section_index]
        section_changed = self.current_section_index != current_section_index
        seg_idx = min(
            max(0, bisect.bisect_right(self._segment_starts, current_time) - 1),
            len(self.segments) - 1,
        )
        energy_score = self._section_energies[current_section_index]
        fade = self._fade_multiplier(current_time)

        # Low-energy colors drift in early; high-energy colors arrive near the boundary.
        next_section_idx = current_section_index + 1
        if (
            next_section_idx < len(self.sections)
            and self._beat_starts
            and self._pre_section_trans_for != next_section_idx
            and not section_changed
        ):
            next_section = self.sections[next_section_idx]
            next_section_start = self._section_bar_starts[next_section_idx]
            important_boundary = next_section_idx in self.section_color_indexes

            if important_boundary and next_section.get('confidence', 1.0) >= self.min_confidence:
                idx_at_section = bisect.bisect_left(self._beat_starts, next_section_start)
                lead_beats = max(2, round((1.0 - self._section_energies[next_section_idx]) * 8))
                target_beat_idx = max(0, idx_at_section - lead_beats)
                pre_trans_start = self._beat_starts[target_beat_idx]

                if lead_beats and pre_trans_start <= current_time < next_section_start:
                    time_remaining = next_section_start - current_time
                    target_hue, target_sat = self._section_hue_sat(next_section)
                    self._pre_section_trans_for = next_section_idx
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

        # Sections are detected at their raw start; color changes are anchored
        # to the first analyzed bar in that section.
        if section_changed:
            logger.info(
                "Section change (index {}, energy={:.2f})",
                current_section_index, energy_score,
            )
            self.current_section_index = current_section_index
            if (
                current_section_index in self.section_color_indexes
                and section.get('confidence', 1.0) >= self.min_confidence
                and self._pre_section_trans_for != current_section_index
            ):
                target_hue, target_saturation = self._section_hue_sat(section)
                deadline = time.monotonic() + max(
                    0.0,
                    self._section_bar_starts[current_section_index] - current_time,
                )
                self._pre_section_trans_for = current_section_index
                self._cancel_section_color_task()
                self._section_color_task = asyncio.create_task(
                    self._scheduled_section_color(
                        deadline, target_hue, target_saturation,
                        current_section_index,
                    )
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
            else:
                self._cancel_beat_task()
                self._beat_scheduled_for = next_beat_idx
                deadline = time.monotonic() + fire_in
                profile_index, profile = profile_for(energy_score, bar_index)
                pulses = pulses_for(
                    profile, self.atmospheric_pulse_on, self.atmospheric_pulse_off,
                )
                pulse, attack, hold, release = pulses[next_beat_idx % len(pulses)]
                spike = max(1, int(self.map_brightness(self.segments[seg_idx], energy_score) * fade))
                if next_beat_idx % max(1, self.beats_per_measure) == 0:
                    spike = min(100, int(spike * profile["downbeat_boost"]))
                low = max(1, int((1 + energy_score * 5) * fade))
                high = max(low + 1, int(spike * pulse))
                high, shown_high, shown_low = self._visible_pulse_brightness(high, low)
                energy_level = round(energy_score * 4)
                energy_color = ("blue", "cyan", "green", "yellow", "red")[energy_level]
                logger.opt(colors=True).debug(
                    f"<bold>Beat {{beat:>3}}</bold> | <{energy_color}>E{{energy}}</{energy_color}> P{{profile}} | "
                    "pulse <magenta>{pulse:>4.2f}</magenta> | "
                    "env <cyan>{attack:>4.2f} {hold:>4.2f} {release:>4.2f}</cyan> | "
                    "brightness <green>{high:>3}%</green> → <dim>{low:>3}%</dim>",
                    beat=next_beat_idx, energy=energy_level,
                    profile=profile_index, pulse=pulse, attack=attack, hold=hold,
                    release=release, high=shown_high, low=shown_low,
                )
                self._beat_task = asyncio.create_task(
                    self._scheduled_beat_pulse(
                        deadline, high, low, next_beat.get('duration', 0.5),
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

        if tasks:
            await asyncio.gather(*tasks)

    async def _scheduled_beat_pulse(
        self,
        deadline: float,
        spike: int,
        base: int,
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

    async def _scheduled_section_color(
        self,
        deadline: float,
        hue: int,
        saturation: int,
        section_idx: int,
    ):
        await asyncio.sleep(max(0.0, deadline - self._device_latency() - time.monotonic()))
        beat_duration = self.avg_measure_duration / max(1, self.beats_per_measure)
        await self.set_parameters(
            beat_duration,
            change_color=True,
            target_hue=hue,
            target_saturation=saturation,
            deadline=deadline,
        )
        logger.info(
            "Section recolor (index {}, energy={:.2f})",
            section_idx, self._section_energies[section_idx],
        )

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

    def _cancel_beat_task(self):
        if self._beat_task and not self._beat_task.done():
            self._beat_task.cancel()
        self._beat_task = None

    def _cancel_section_color_task(self):
        if self._section_color_task and not self._section_color_task.done():
            self._section_color_task.cancel()
        self._section_color_task = None

    async def set_parameters(
        self,
        duration: float = 0.05,
        brightness: int | None = None,
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
            new_brightness = self._quantize_brightness(brightness)
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
            # Lerp hue/sat to the current transition position for the panel while
            # brightness-only beat commands leave the bulb's color untouched.
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

        if wait_before_ms:
            await asyncio.sleep(wait_before_ms / 1000)
        if change_color:
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

    def _quantize_brightness(self, brightness: float) -> int:
        scaled_brightness = brightness * self.max_brightness / 100
        return max(1, min(100, round(scaled_brightness / 5) * 5 or 1))

    def _visible_pulse_brightness(self, high: int, low: int) -> tuple[int, int, int]:
        low_quantized = self._quantize_brightness(low)
        for candidate in range(high, 101):
            high_quantized = self._quantize_brightness(candidate)
            if high_quantized > low_quantized:
                return candidate, high_quantized, low_quantized
        return high, self._quantize_brightness(high), low_quantized


if __name__ == "__main__":
    controller = object.__new__(LightsController)
    controller.max_brightness = 100
    assert controller._visible_pulse_brightness(4, 3)[1:] == (10, 5)
    assert controller._visible_pulse_brightness(14, 3)[1:] == (15, 5)
    print("pulse quantization: ok")
