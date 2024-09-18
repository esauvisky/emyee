import asyncio
import time
from typing import List
from dotenv import load_dotenv
from loguru import logger
from models import EventSongChanged, EventAdjustProgressTime, EventStop
from utils import (
    get_current_item,
    get_new_color,
    COLORS,
    get_next_item,
    CONTROLLER_TICK,
    merge_short_segments,
    visualize_segments
)
import random
import numpy as np

from light_device import LightDevice  # Import the LightDevice class

class LightsController:
    def __init__(self, devices: List[LightDevice], events_queue: asyncio.Queue):
        self.devices = devices
        self.events_queue = events_queue
        self.last_section_num_next = 0
        self.last_index = -1  # Initialize to -1 to ensure the first index is processed
        self.current_hue = random.randint(0, 359)
        self.current_saturation = random.randint(50, 80)
        self.current_brightness = 0
        self.sections = []   # List of song sections
        self.last_bar = {}
        self.last_next_segment = None
        self.current_section = None
        self.analysis = None
        self.current_progress = 0

        # Initialize current parameters for comparison
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
                logger.debug("Song changed!")
                self.handle_song_changed(event)
            elif isinstance(event, EventAdjustProgressTime):
                if self.analysis is not None:
                    # logger.debug(f"Received event: {event}")
                    self.current_progress = event.progress_time_ms
                    await self.handle_adjust_progress(self.current_progress)
            elif isinstance(event, EventStop):
                logger.warning("Song stopped!")
            self.events_queue.task_done()
            await asyncio.sleep(CONTROLLER_TICK)

    def handle_song_changed(self, event: EventSongChanged):
        self.analysis = event.analysis
        self.sections = self.analysis['sections']
        # visualize_segments(self.analysis['segments'])
        self.segments = merge_short_segments(self.analysis['segments'])
        self.bars = self.analysis['bars']
        self.last_bar = self.bars[0]
        self.current_section = self.sections[0]
        self.beats = self.analysis['beats']

    def map_brightness(self, segment):
        decibel_to_linear = lambda x: 10**(x / 20)
        next_loudness = decibel_to_linear(segment["loudness_start"])

        # Calculate mean and standard deviation of loudness values
        loudness_values = [decibel_to_linear(seg['loudness_start']) for seg in self.segments]
        mean_loudness = np.mean(loudness_values)
        std_dev_loudness = np.std(loudness_values)

        # Define the range within which to consider values
        lower_bound = mean_loudness - 2 * std_dev_loudness
        upper_bound = mean_loudness + 2 * std_dev_loudness
        filtered_loudness_values = [loudness for loudness in loudness_values if lower_bound <= loudness <= upper_bound]

        min_loudness = min(filtered_loudness_values)
        max_loudness = max(filtered_loudness_values)
        brightness = int((next_loudness - min_loudness) / (max_loudness - min_loudness) * 50)

        # logger.trace(f"Segment loudness: {next_loudness:.5f} (min: {min_loudness:.2f}, max: {max_loudness:.2f})")
        return brightness

    async def handle_adjust_progress(self, current_time: float):
        next_segment = get_next_item(self.segments, current_time)
        current_bar = get_current_item(self.bars, current_time)
        if not next_segment or not current_bar:
            logger.debug("skipping adjust progress")
            return
        self.last_next_segment = next_segment
        current_bar_duration = current_bar["duration"] - (current_time - current_bar['start'])
        brightness = self.map_brightness(next_segment)

        # Check if we need to move to the next bar
        if self.last_bar != current_bar and current_bar['confidence'] > 0.5:
            logger.warning(f"Transitioning from bar {self.bars.index(self.last_bar)} to bar {self.bars.index(current_bar)} in {current_bar_duration:.2f}s")
            asyncio.create_task(self.set_parameters(current_bar_duration, change_color=True))
            self.last_bar = current_bar
        # Check if we need to move to the next segment
        elif next_segment['start']:
            asyncio.create_task(self.set_parameters(next_segment['duration'], brightness=brightness))

    async def set_parameters(self, duration: float = 0.05, brightness: int | None = None, change_color: bool = False):
        # Determine new parameters
        new_hue = random.randint(0, 359) if change_color else self._current_params['hue']
        new_saturation = random.randint(50, 80) if change_color else self._current_params['saturation']
        new_brightness = brightness if brightness is not None else self._current_params['brightness']

        # Check if any parameter has changed
        params_changed = (
            new_hue != self._current_params['hue'] or
            new_saturation != self._current_params['saturation'] or
            new_brightness != self._current_params['brightness']
        )

        if not params_changed:
            # logger.trace("No parameter changes detected. Skipping set_parameters to prevent blinking.")
            return

        # Update current parameters
        if change_color:
            self._current_params['hue'] = new_hue
            self._current_params['saturation'] = new_saturation
        elif brightness is not None:
            self._current_params['brightness'] = new_brightness

        logger.info(f"Setting parameters: duration={duration:.2f}s, brightness={new_brightness}%, hue={new_hue}, saturation={new_saturation}")

        # Apply settings to devices using LightDevice's asynchronous methods
        set_tasks = []
        for device in self.devices:
            if device.bulb.model == "ct_bulb":
                set_tasks.append(device.set_brightness(new_brightness, duration=duration))
            else:
                if change_color:
                    set_tasks.append(device.set_hsv(new_hue, new_saturation, new_brightness, duration=duration))
                elif brightness is not None:
                    set_tasks.append(device.set_brightness(new_brightness, duration=duration))

        # Execute all state changes concurrently
        await asyncio.gather(*set_tasks)

        # Optionally, wait for the duration minus the controller tick
        await asyncio.sleep(0)
