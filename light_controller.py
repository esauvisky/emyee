import asyncio
import time
from typing import AsyncIterable
from dotenv import load_dotenv
from loguru import logger
from models import EventSongChanged, EventAdjustProgressTime, EventStop
from utils import get_current_item, get_new_color, COLORS, get_next_item, map_loudness_to_brightness, CONTROLLER_TICK
import random
import numpy as np


class LightsController:
    def __init__(self, devices, events_queue: asyncio.Queue):
        self.devices = devices
        self.events_queue = events_queue
        self.last_section_num_next = 0
        self.last_index = -1 # Initialize to -1 to ensure the first index is processed
        self.current_hue = random.randint(0, 359)
        self.current_saturation = random.randint(50, 80)
        self.current_brightness = 0
        self.sections = []   # List of song sections
        self.last_bar = {}
        self.current_section = None
        self.analysis = None
        self.current_progress = 0

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
            await asyncio.sleep(CONTROLLER_TICK)
            self.events_queue.task_done()

    def handle_song_changed(self, event):
        self.analysis = event.analysis
        self.sections = self.analysis['sections']
        self.segments = self.analysis['segments']
        self.bars = self.analysis['bars']
        self.last_bar = self.bars[0]
        self.current_section = self.sections[0]
        self.beats = self.analysis['beats']
        self.mapped = map_loudness_to_brightness(event.analysis)

    def map_brightness(self, next_segment):
        decibel_to_linear = lambda x: 10**(x / 20)
        next_loudness = decibel_to_linear(next_segment["loudness_start"])

        # Calculate mean and standard deviation of loudness values
        loudness_values = [decibel_to_linear(segment['loudness_start']) for segment in self.segments]
        mean_loudness = np.mean(loudness_values)
        std_dev_loudness = np.std(loudness_values)

        # Define the range within which to consider values
        lower_bound = mean_loudness - 2*std_dev_loudness
        upper_bound = mean_loudness + 2*std_dev_loudness
        filtered_loudness_values = [loudness for loudness in loudness_values if lower_bound <= loudness <= upper_bound]

        min_loudness = min(filtered_loudness_values)
        max_loudness = max(filtered_loudness_values)
        brightness = int((next_loudness-min_loudness) / (max_loudness-min_loudness) * 50)

        logger.trace(f"Next segment loudness: {next_loudness:.5f} (min: {min_loudness:.2f}, max: {max_loudness:.2f})")
        return brightness

    async def handle_adjust_progress(self, current_time):
        next_segment = get_next_item(self.segments, current_time)
        current_bar = get_current_item(self.bars, current_time)
        if not next_segment or not current_bar:
            return
        current_bar_duration = current_bar["duration"] - (current_time - current_bar['start'])
        brightness = self.map_brightness(next_segment)

        ## Check if we need to move to the next section
        # if self.current_section and current_bar['start'] + current_bar_duration > self.current_section['start'] + self.current_section['duration']:
        #     logger.warning(f"Moving to next section: {self.current_section}")
        #     self.current_section = get_next_item(self.sections, current_bar['start'] + current_bar_duration)
        #     asyncio.create_task(self.set_parameters(current_bar_duration, change_color=True))

        # Check if we need to move to the next bar
        if self.last_bar != current_bar and current_bar['confidence'] > 0.6:
            logger.warning(f"Transitioning from bar {self.bars.index(self.last_bar)} to bar {self.bars.index(current_bar)} in {current_bar_duration:.2f}s")
            await self.set_parameters(current_bar_duration, change_color=True)
            self.last_bar = current_bar
        # Check if we need to move to the next segment
        elif next_segment['start']:
            await self.set_parameters(next_segment['duration'], brightness=brightness)


    async def set_parameters(self, duration=0.05, brightness=None, change_color=False):
        hue = random.randint(0, 359) if change_color else self.current_hue
        saturation = random.randint(50, 80) if change_color else self.current_saturation
        brightness = None if brightness is None or brightness == self.current_brightness else brightness

        if change_color:
            logger.warning(f"Setting parameters: duration={duration:.2f}s, brightness={brightness}%, hue={hue}, saturation={saturation}")
            self.current_hue = hue
            self.current_saturation = saturation
            for device in self.devices:
                device.duration = duration * 1000
                device.set_hsv(hue, saturation, brightness, duration=duration * 1000)
            await asyncio.sleep(duration)
        elif brightness is not None:
            logger.info(f"Setting parameters: duration={duration:.2f}s, brightness={brightness}%, hue={hue}, saturation={saturation}")
            for device in self.devices:
                device.duration = duration * 1000
                device.set_hsv(self.current_hue, self.current_saturation, brightness, duration=duration * 1000)

