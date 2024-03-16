import asyncio
import time
from typing import AsyncIterable
from loguru import logger
from models import EventSongChanged, EventAdjustProgressTime, EventStop
from utils import get_current_item, get_new_color, COLORS, get_next_item, map_loudness_to_brightness, CONTROLLER_TICK
import random


class LightsController:
    def __init__(self, devices, events_queue: asyncio.Queue):
        self.devices = devices
        self.events_queue = events_queue
        self.last_section_num_next = 0
        self.last_index = -1 # Initialize to -1 to ensure the first index is processed
        self.current_color = random.choice(COLORS)
        self.sections = []   # List of song sections
        self.last_duration = 0
        self.last_bar = None
        self.current_section = None

    async def control_lights(self):
        event = EventStop()
        while True:
            while not self.events_queue.empty():
                event = self.events_queue.get_nowait()

            if isinstance(event, EventSongChanged):
                logger.debug("Song changed!")
                self.handle_song_changed(event)
            elif isinstance(event, EventAdjustProgressTime):
                logger.debug(f"Received event: {event}")
                progress_time = event.progress_time_ms / 1000
                asyncio.create_task(self.handle_adjust_progress(progress_time))
            elif isinstance(event, EventStop):
                logger.warning("Song stopped!")

            await asyncio.sleep(CONTROLLER_TICK)

    def handle_song_changed(self, event):
        self.analysis = event.analysis
        self.sections = self.analysis['sections']
        self.segments = self.analysis['segments']
        self.bars = self.analysis['bars']
        self.last_bar = self.bars[0]
        self.current_section = self.sections[0]
        self.beats = self.analysis['beats']
        self.mapped = map_loudness_to_brightness(event.analysis)
        self.current_color = get_new_color(self.current_color)

    async def handle_adjust_progress(self, current_time):
        current_beat = get_current_item(self.beats, current_time)
        current_segment = get_current_item(self.segments, current_time)
        current_bar = get_current_item(self.bars, current_time)
        if not current_beat or not current_segment or not current_bar:
            return
        current_loudness = current_segment["loudness_start"]
        current_duration = current_bar["duration"]
        # scale loudness to brightness by grabbing max and mins from the analysis and mapping current loudness to a range
        min_loudness = min(segment['loudness_start'] for segment in self.segments)
        max_loudness = max(segment['loudness_max'] for segment in self.segments)
        brightness = int((current_loudness-min_loudness) / (max_loudness-min_loudness) * 100)

        # Check if we need to move to the next section
        # logger.info(f"Checking if we need to move to the next section. {current_bar['start']} {current_duration} {self.current_section['start']} {self.current_section['duration']}")
        if self.current_section and current_bar['start'] + current_duration > self.current_section['start'] + self.current_section['duration']:
            logger.warning(f"Moving to next section: {self.current_section}")
            self.current_section = get_next_item(self.sections, current_duration)
            await self.begin_color_transition(current_duration)

        if self.last_bar and self.last_bar != current_bar:
            logger.info(f"Moving to next bar: {current_bar}")
            self.last_bar = current_bar
            await self.set_device_state(current_duration, brightness, self.current_color)

    async def begin_color_transition(self, current_duration):
        self.current_color = get_new_color(self.current_color)
        for device in self.devices:
            device.duration = int(current_duration * 1000)
            device.set_rgb(*self.current_color)
        await asyncio.sleep(current_duration)

    async def set_device_state(self, duration, brightness=None, color=None):
        for device in self.devices:
            logger.info(f"Setting device state: duration={duration}, brightness={brightness}, color={color}")
            device.duration = int(duration * 1000)
            if brightness:
                device.set_brightness(brightness)
            if color:
                device.set_rgb(*color)
            self.last_duration = duration
