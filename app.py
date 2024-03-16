#!/usr/bin/env python3
# shamelessly stolen from https://gist.github.com/nvbn/73b613849cb176ec33057236b2fd4558
from __future__ import annotations

import array
import asyncio
from pprint import pprint
import dotenv
from bisect import bisect_left
from dataclasses import dataclass
import logging
import os
import socket
import time
from typing import Union, Dict, Any, NoReturn, AsyncIterable, List, Tuple, Callable
import aiohttp
from spotipy.util import prompt_for_user_token
import yeelight

from loguru import logger
import time
import sys

# Events listener, device controller
CONTROLLER_TICK = 0
CONTROLLER_ERROR_DELAY = 1

import random

last_mult_section = 0
last_rgb = (0, 0, 0)

# An expanded array that includes primary, secondary, and tertiary colors
COLORS = [
    (255, 0, 0),        # Red
    (0, 255, 0),        # Green
    (0, 0, 255),        # Blue
    (255, 255, 0),      # Yellow
    (0, 255, 255),      # Cyan
    (255, 0, 255),      # Magenta
    (128, 0, 0),        # Maroon
    (128, 128, 0),      # Olive
    (0, 128, 0),        # Dark Green
    (128, 0, 128),      # Purple
    (0, 128, 128),      # Teal
    (0, 0, 128),        # Navy
    (255, 165, 0),      # Orange
    (255, 192, 203),    # Pink
    (255, 215, 0),      # Gold
    (75, 0, 130),       # Indigo
    (240, 128, 128),    # Light Coral
    (95, 158, 160),     # Cadet Blue
                        # Add more colors as needed
]


def setup_logging(level="DEBUG", show_module=False):
    """
    Setups better log format for loguru
    """
    logger.remove(0)    # Remove the default logger
    log_level = level
    log_fmt = "<green>["
    log_fmt += "{file:10.10}…:{line:<3} | " if show_module else ""
    log_fmt += "{time:HH:mm:ss.SSS}]</green> <level>{level: <8}</level> | <level>{message}</level>"
    logger.add(sys.stderr, level=log_level, format=log_fmt, colorize=True, backtrace=True, diagnose=True)


# Shared communication
RawSpotifyResponse = Dict[str, Any]


@dataclass
class EventSongChanged:
    analysis: RawSpotifyResponse
    start_time: float


@dataclass
class EventAdjustStartTime:
    start_time: float


@dataclass
class EventStop:
    ...


Event = Union[EventSongChanged, EventAdjustStartTime, EventStop]

Colors = List[Tuple[int, int, int]]

# Event producer
API_CURRENT_PLAYING = 'https://api.spotify.com/v1/me/player/currently-playing'
API_AUDIO_ANALYSIS = 'https://api.spotify.com/v1/audio-analysis/'
SPOTIFY_SCOPE = 'user-read-currently-playing,user-read-playback-state'
SPOTIFY_CHANGES_LISTENER_DELAY = 0.1
SPOTIFY_CHANGES_LISTENER_FAILURE_DELAY = 1




def _get_start_time(current_playing: RawSpotifyResponse, request_time: float) -> float:
    # spotify timestamp appears to be incorrect https://github.com/spotify/web-api/issues/640
    return (request_time + time.time()) / 2 - current_playing['progress_ms'] / 1000

async def _listen_to_spotify_changes(session: aiohttp.ClientSession) -> AsyncIterable[Event]:
    current_id = None
    while True:
        request_time = time.time()
        current = await _get_current_playing(session)
        if not current['is_playing']:
            current_id = None
            yield EventStop()
        elif current['item']['id'] != current_id:
            current_id = current['item']['id']
            # if current_id % 2 == 0:
            analysis = await _get_audio_analysis(session, current_id)
            yield EventSongChanged(analysis, _get_start_time(current, request_time))
        else:
            yield EventAdjustStartTime(_get_start_time(current, request_time))

        await asyncio.sleep(SPOTIFY_CHANGES_LISTENER_DELAY)


async def spotify_changes_listener(user_id: str, client_id: str, client_secret: str, events_queue: asyncio.Queue[Event]) -> NoReturn:
    while True:
        # I'm too lazy to make that async
        token = prompt_for_user_token(user_id,
                                      SPOTIFY_SCOPE,
                                      client_id=client_id,
                                      client_secret=client_secret,
                                      redirect_uri='http://localhost:8000/')
        headers = {'Authorization': f'Bearer {token}'}
        async with aiohttp.ClientSession(headers=headers) as session:
            try:
                async for event in _listen_to_spotify_changes(session):
                    await events_queue.put(event)
            except Exception:
                logging.exception('Something went wrong with spotify_changes_listener')

                await asyncio.sleep(SPOTIFY_CHANGES_LISTENER_FAILURE_DELAY)


# Communication with the device
class DeviceBus:
    def connection_made(self, _):
        logging.info('Connected')

    def error_received(self, exc):
        logging.exception('Error received', exc_info=exc)

    def connection_lost(self, exc):
        logging.exception('Connection closed', exc_info=exc)


async def _get_current_playing(session: aiohttp.ClientSession) -> RawSpotifyResponse:
    async with session.get(API_CURRENT_PLAYING) as response:
        return await response.json()


def map_loudness_to_brightness(data):
    segments = data['segments']

    # Extract all loudness_start and loudness_max values to find the overall min and max
    loudness_values = [segment['loudness_start'] for segment in segments] + [
        segment.get('loudness_max', segment['loudness_start']) for segment in segments]
    min_loudness = min(loudness_values)
    max_loudness = max(loudness_values)

    def loudness_to_brightness(loudness, min_loudness, max_loudness):
        # Scale the loudness to a 0-100 scale
        if max_loudness == min_loudness: # Avoid division by zero if all loudnesses are the same
            return 0                     # Or return another appropriate value
        brightness = ((loudness-min_loudness) / (max_loudness-min_loudness)) * 100
        return int(brightness)

    # Update our previous function to include brightness calculation
    parsed_data = parse_audio_data_improved(data)

    # Scale each "loudness_next" to the new brightness scale
    for item in parsed_data:
        if "loudness_next" in item:
            item['loudness_next'] = loudness_to_brightness(item['loudness_next'], min_loudness, max_loudness)
        elif "loudness_current" in item:
            item['loudness_current'] = loudness_to_brightness(item['loudness_current'], min_loudness, max_loudness)

    return parsed_data


def parse_audio_data_improved_with_check(data):
    bars = data['bars']
    segments = data['segments']
    sections = data['sections']

    # Pre-process sections to know the bounds of each section
    section_bounds = []
    for i, section in enumerate(sections):
        start = section['start']
        end = start + section['duration']
        section_bounds.append((start, end, i)) # Include section index for reference

    def find_section_number(bar_start):
        for start, end, section_index in section_bounds:
            if start <= bar_start < end:
                return section_index
        return None     # In case no section is found, though this shouldn't happen with valid input

    result = []
    for i in range(len(bars) - 1):
        current_bar = bars[i]
        next_bar = bars[i + 1]

        # Finding segments that start within the bounds of the next bar
        segment_loudnesses = [
            segment['loudness_start']
            for segment in segments
            if next_bar['start'] <= segment['start'] < next_bar['start'] + next_bar['duration']]

        # If there are no segments in the next bar, default to 0 for average loudness (or some other logic as required)
        average_loudness_next = sum(segment_loudnesses) / len(segment_loudnesses) if segment_loudnesses else 0

        section_num_next = find_section_number(next_bar['start'])

        result.append({
            'start': current_bar['start'],
            'duration': current_bar['duration'],
            'loudness_next': average_loudness_next,
            'section_num_next': section_num_next})

    return result


def parse_audio_data_improved(data):
    bars = data['bars']
    segments = data['segments']
    sections = data['sections']

    # Pre-process sections to know the bounds of each section
    section_bounds = []
    for i, section in enumerate(sections):
        start = section['start']
        end = start + section['duration']
        section_bounds.append((start, end, i)) # Include section index for reference

    def find_section_number(bar_start):
        for start, end, section_index in section_bounds:
            if start <= bar_start < end:
                return section_index
        return None     # In case no section is found, though this shouldn't happen with valid input

    def calculate_segment_loudness(segment):
        # This calculation will consider the loudness_start, loudness_max, duration, and loudness_max_time
        # Assuming a linear increase in loudness from loudness_start to loudness_max at loudness_max_time
        # Then a constant loudness at loudness_max for the remainder of the segment's duration
        # This is a simplified model and may not accurately reflect the true loudness curve of the segment
        loudness_start = segment['loudness_start']
        loudness_max = segment['loudness_max']
        loudness_max_time = segment['loudness_max_time']
        duration = segment['duration']

        # Calculate average loudness during the rise to loudness_max
        if loudness_max_time > 0:
            average_loudness_rise = (loudness_start+loudness_max) / 2
            proportion_rise = loudness_max_time / duration
        else:
            average_loudness_rise = loudness_max # No rise time implies instant max loudness
            proportion_rise = 0

        # Calculate average loudness for the remainder of the segment
        average_loudness_remainder = loudness_max
        proportion_remainder = 1 - proportion_rise

        # Weighted average of the two phases
        average_loudness = (average_loudness_rise*proportion_rise) + (average_loudness_remainder*proportion_remainder)
        return average_loudness

    result = []
    for i in range(len(bars) - 1): # Iterando sobre cada barra, exceto a última
        current_bar = bars[i]      # O compasso atual na iteração

        # Encontrando segmentos que iniciam dentro do intervalo do compasso atual
        relevant_segments = [
            segment for segment in segments
            if current_bar['start'] <= segment['start'] < current_bar['start'] + current_bar['duration']]

        # Calcula a média da sonoridade para esses segmentos relevantes
        if relevant_segments:
            average_loudness_current = round(sum(calculate_segment_loudness(segment)
                                           for segment in relevant_segments) / len(relevant_segments))
            duration = sum(segment['duration'] for segment in relevant_segments)
        else:
            average_loudness_current = 0 # Assume 0 se não encontrar segmentos relevantes
            duration = 0

        # Determina a qual seção pertence o próximo
        section_num_next = find_section_number(bars[i+1]['start'])

        # Adiciona os resultados para o compasso atual ao resultado
        result.append({
            'index': i,                           # O índice do compasso atual
            'start': current_bar['start'],        # O tempo de início do compasso atual
            'duration': duration,                 # O tempo de duração do compasso atual
            'loudness_current': average_loudness_current, # A média da sonoridade dos segmentos que iniciam neste compasso
            'section_num_next': section_num_next     # O número da seção do compasso atual
        })

    return result


async def _get_audio_analysis(session: aiohttp.ClientSession, id: str) -> RawSpotifyResponse:
    async with session.get(API_AUDIO_ANALYSIS + id) as response:
        json = await response.json()
        # parsed = parse_audio_data_improved(json)
        mapped = map_loudness_to_brightness(json)
        return mapped

def make_get_current_colors(music: RawSpotifyResponse) -> Callable[[float], Colors]:
    items_sorted_by_start = sorted(music, key=lambda x: x['start'])

    def get_current(t):
        remaining_items = [item for item in items_sorted_by_start if item['start'] >= t]
        current_item = remaining_items[0] if remaining_items else None
        return current_item

    return get_current


async def _events_to_colors(events_queue: asyncio.Queue[Event]) -> AsyncIterable[Colors, float]:
    get_current_colors = None
    start_time = 0
    event = EventStop()
    while True:
        while not events_queue.empty():
            event = events_queue.get_nowait()

        if isinstance(event, EventSongChanged):
            start_time = event.start_time
            get_current_colors = make_get_current_colors(event.analysis)
        elif isinstance(event, EventAdjustStartTime):
            start_time = event.start_time
        elif isinstance(event, EventStop):
            get_current_colors = None

        if get_current_colors:
            yield get_current_colors(time.time() - start_time)

        await asyncio.sleep(CONTROLLER_TICK)


async def lights_controller(devices: List[yeelight.Bulb], events_queue: asyncio.Queue[Event]) -> NoReturn:
    while True:
        last_section_num_next = 0
        last_index = 0
        color = random.choice(COLORS)
        actual_color = get_new_color(color)
        try:
            async for item in _events_to_colors(events_queue):
                if item["index"] != last_index:
                    logger.debug(pprint(item))
                    if item["section_num_next"] != last_section_num_next:
                        actual_color = get_new_color(color)

                    for device in devices:
                        device.duration = int(item["duration"] * 1000 - CONTROLLER_TICK * 1000)
                        device.set_rgb(*actual_color)
                        device.set_brightness(item["loudness_current" if "loudness_current" in item else "loudness_next"])

                    last_index = item["index"]
                    last_section_num_next = item["section_num_next"]

                await asyncio.sleep(CONTROLLER_TICK)

        except Exception:
            logging.exception("Something went wrong with lights_controller")
            await asyncio.sleep(CONTROLLER_ERROR_DELAY)


def get_new_color(color):
    colors = list(COLORS)
    del colors[COLORS.index(color)]
    color = random.choice(colors)
    actual_color = tuple(max(0, min(255, component + random.randint(-20, 20))) for component in color)
    return actual_color


# Glue
def main():
    dotenv.load_dotenv(".env")
    user_id = os.environ['USER_ID']
    client_id = os.environ['CLIENT_ID']
    client_secret = os.environ['CLIENT_SECRET']
    setup_logging("DEBUG")

    logger.info("Discovering bulbs...")
    bulbs = yeelight.discover_bulbs(2)
    logger.info(f"Found {len(bulbs)} bulbs in the network.")
    devices = []
    for bulb in [yeelight.Bulb(device["ip"], device["port"], effect="smooth", auto_on=False) for device in bulbs]:
        logger.trace(bulb.get_model_specs())
        logger.trace(bulb.get_capabilities())
        logger.trace(bulb.get_properties())
        if bulb.get_capabilities()["model"] != "ct_bulb":
            try:
                logger.info(f"Setting music mode to {bulb._ip}")
                bulb.start_music()
                devices.append(bulb)
            except Exception:
                logger.debug(f"Not setting music mode to {bulb._ip}")
    logger.success(f"Using {len(devices)} bulbs in the network.")

    events_queue = asyncio.Queue()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        asyncio.gather(spotify_changes_listener(user_id, client_id, client_secret, events_queue),
                       lights_controller(devices, events_queue)))


if __name__ == '__main__':
    # asyncio.run(main())
    main()
