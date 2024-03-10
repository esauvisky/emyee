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
SPOTIFY_CHANGES_LISTENER_DELAY = 0.01
SPOTIFY_CHANGES_LISTENER_FAILURE_DELAY = 1


async def _get_current_playing(session: aiohttp.ClientSession) -> RawSpotifyResponse:
    async with session.get(API_CURRENT_PLAYING) as response:
        return await response.json()


async def _get_audio_analysis(session: aiohttp.ClientSession, id: str) -> RawSpotifyResponse:
    async with session.get(API_AUDIO_ANALYSIS + id) as response:
        return await response.json()


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
        token = prompt_for_user_token(user_id, SPOTIFY_SCOPE, client_id=client_id, client_secret=client_secret,
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

import random
last_mult_section = 0
last_rgb = (0, 0, 0)

# An expanded array that includes primary, secondary, and tertiary colors
COLORS = [
    (255, 0, 0),     # Red
    (0, 255, 0),     # Green
    (0, 0, 255),     # Blue
    (255, 255, 0),   # Yellow
    (0, 255, 255),   # Cyan
    (255, 0, 255),   # Magenta
    (128, 0, 0),     # Maroon
    (128, 128, 0),   # Olive
    (0, 128, 0),     # Dark Green
    (128, 0, 128),   # Purple
    (0, 128, 128),   # Teal
    (0, 0, 128),     # Navy
    (255, 165, 0),   # Orange
    (255, 192, 203), # Pink
    (255, 215, 0),   # Gold
    (75, 0, 130),    # Indigo
    (240, 128, 128), # Light Coral
    (95, 158, 160),  # Cadet Blue
    # Add more colors as needed
]

last_mult_section = 0
last_rgb = (0, 0, 0)

async def send_to_device(device: Device, brightness: int, duration: int, mult_section: float) -> None:
    global last_mult_section, last_rgb
    logger.debug(f"Setting {device._ip} to {brightness} for {int(duration * 1000)}ms...")

    device.duration = 0
    device.set_rgb(*last_rgb)
    device.duration = int(duration * 1000 - CONTROLLER_TICK * 1000)
    device.set_brightness(brightness)

    # Optionally, log the new color and brightness
    # logger.info(f"New brightness: {brightness}")

def _normalize(pv: float) -> float:
    if pv < 0:
        return 0.
    elif pv > 255:
        return 255.
    else:
        return pv

def aggregate_high_confidence_items(items):
    high_confidence_items = [item for item in items if item['confidence'] > 0.5]
    if not high_confidence_items:
        return None

    aggregated = {
        'start': high_confidence_items[0]['start'],
        'duration': sum(item['duration'] for item in high_confidence_items),
        'confidence': sum(item['confidence'] for item in high_confidence_items) / len(high_confidence_items),
        'loudness': sum(item.get('loudness', 0) for item in high_confidence_items) / len(high_confidence_items),
        'loudness_start': high_confidence_items[0].get('loudness_start'),
        'tempo': sum(item.get('tempo', 0) for item in high_confidence_items) / len(high_confidence_items),
        'tempo_confidence': sum(item.get('tempo_confidence', 0) for item in high_confidence_items) / len(high_confidence_items),
        'key': high_confidence_items[0].get('key'),
        'key_confidence': high_confidence_items[0].get('key_confidence'),
        'mode': high_confidence_items[0].get('mode'),
        'mode_confidence': sum(item.get('mode_confidence', 0) for item in high_confidence_items) / len(high_confidence_items),
        'time_signature': high_confidence_items[0].get('time_signature'),
        'time_signature_confidence': high_confidence_items[0].get('time_signature_confidence'),
        'loudness_max': max(item.get('loudness_max', float('-inf')) for item in high_confidence_items),
        'loudness_max_time': high_confidence_items[high_confidence_items.index(max(high_confidence_items, key=lambda item: item.get('loudness_max', float('-inf'))))].get('loudness_max_time'),
        'loudness_end': high_confidence_items[-1].get('loudness_end'),
        'pitches': high_confidence_items[0].get('pitches'),
        'timbre': high_confidence_items[0].get('timbre'),
    }

    return aggregated

def make_get_current_colors(music: RawSpotifyResponse) -> Callable[[float], Colors]:

    def make_get_current(name):
        keys = [x['start'] for x in music[name]]
        key_to_x = {x['start']: x for x in music[name]}
        items_sorted_by_start = sorted(music[name], key=lambda x: x['start'])

        def current_aggregate(t):
            index = bisect_left(keys, t) - 1
            current_item = items_sorted_by_start[index]
            # Find all subsequent items with high confidence
            high_confidence_items = [item for item in items_sorted_by_start[index:] if item['confidence'] > 0.5]
            return aggregate_high_confidence_items(high_confidence_items[:1])  # Just the current item if alone

        return current_aggregate

    def make_get_maximums(name):
        keys = [x['start'] for x in music[name]]
        key_to_x = {x['start']: x for x in music[name]}
        items_sorted_by_start = sorted(music[name], key=lambda x: x['start'])

    def make_get_next(name):
        keys = [x['start'] for x in music[name]]
        key_to_x = {x['start']: x for x in music[name]}
        items_sorted_by_start = sorted(music[name], key=lambda x: x['start'])

        def next_aggregate(t, n):
            index = bisect_left(keys, t) - 1 + n
            next_items = items_sorted_by_start[index:index+n]  # Get n items starting from the next one
            high_confidence_items = [item for item in next_items if item['confidence'] > 0.5]
            return aggregate_high_confidence_items(high_confidence_items)

        return next_aggregate
    get_current_segment = make_get_current('segments')
    get_current_section = make_get_current('sections')
    get_current_beat = make_get_current('beats')
    get_next_segment = make_get_next('segments')
    get_next_section = make_get_next('sections')

    def make_scale(name):
        xs = [x[name] for x in music['sections']]
        min_xs = min(xs)
        max_xs = max(xs)
        logger.trace(f"minimum {name} is {min_xs}, maximum {name} is {max_xs}")
        return lambda x: (x-min_xs) / (max_xs-min_xs)

    def make_scale_log(name, where):
        xs = [10 ** (x[name] / 10) for x in where]
        min_xs = min(xs)
        max_xs = max(xs)
        logger.trace(f"minimum {name} is {min_xs}, maximum {name} is {max_xs}")
        return lambda x: (10 ** (x / 10) - min_xs) / (max_xs - min_xs)

    def get_segment_loudness(segment):
        segment_start_loudness = segment['loudness_start'] * (segment['duration'] - segment['loudness_max_time']) / segment['duration']
        segment_max_loudness = segment['loudness_max'] * segment['loudness_max_time'] / segment['duration']
        return segment_start_loudness + segment_max_loudness

    def get_section_segments(section):
        for s in music['segments']:
            if s['start'] >= section['start'] and s['start'] < section['start'] + section['duration']:
                yield s

    scale_section_loudness = make_scale_log('loudness', music['sections'])
    # scale_segment_loudness = make_scale_log('loudness', analysis['segments'])

    def scale_segment_loudness(loudness, segments):
        loudnesses = [get_segment_loudness(s) for s in segments]
        min_segment_loudness = min(loudnesses)
        max_segment_loudness = max(loudnesses)
        return (loudness - min_segment_loudness) / (max_segment_loudness-min_segment_loudness)

    def get_current_loudness(t):
        # Helper function to get segment loudness

        segments = sorted([s for s in music['segments'] if s['start'] <= t], key=lambda x: x['start'], reverse=True)
        sections = sorted([s for s in music['sections'] if s['start'] <= t], key=lambda x: x['start'], reverse=True)
        # all_segments_from_all_sections = [s for s in music['segments']]

        first_segment = segments[0] if segments else None
        current_section = sections[0] if sections else None
        next_section = sections[1] if sections else None

        if not first_segment or not current_section:
            return None

        # Aggregate segments with confidence < 0.75
        agg_duration = 0
        agg_loudness = 0
        count = 0
        final_start = first_segment['start']
        for next_segment in music['segments']:
            if next_segment['start'] >= first_segment['start'] and next_segment['start'] <= next_section['start']:
                final_start = next_segment['start']
                agg_duration += next_segment['duration']
                agg_loudness += get_segment_loudness(next_segment)
                count += 1
                print("somei mais um")
            else:
                break

        delay = final_start - first_segment['start']
        avg_loudness = agg_loudness / count if count > 0 else get_segment_loudness(first_segment)

        analysis_segments = [segment for segment in music['segments']]
        loudness = int(scale_segment_loudness(avg_loudness, analysis_segments) * 50)

        current_segment_object = {
            'delay': delay,
            'duration': agg_duration,
            'loudness': loudness,
            'section_start': current_section['start'],
            'section_end': current_section['start'] + current_section['duration'],
            'section_loudness': current_section['loudness']
        }

        return current_segment_object
    return get_current_loudness

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
        last_brightness = 0
        last_section_loudness = 0
        color = random.choice(COLORS)
        try:
            async for info in _events_to_colors(events_queue):
                for device in devices:
                    device.duration = int(info["duration"] * 1000 - CONTROLLER_TICK * 1000)

                if info["section_loudness"] != last_section_loudness or info["loudness"] != last_brightness:
                    logger.debug(pprint(info))
                    await asyncio.sleep(info["delay"])

                if info["section_loudness"] != last_section_loudness:
                    actual_color = get_new_color(color)
                    for device in devices:
                        device.set_rgb(*actual_color)
                    last_section_loudness = info["section_loudness"]

                if section != last_brightness:
                    for device in devices:
                        device.set_brightness(section)
                    last_brightness = section

                await asyncio.sleep(CONTROLLER_TICK)
                # await asyncio.sleep(duration - 2 * CONTROLLER_TICK)

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
    bulbs = yeelight.discover_bulbs(10)
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
        # else:
        #     logger.warning(f"Not setting music mode to {bulb._ip} because it's not a ct_bulb bulb")
    logger.success(f"Using {len(devices)} bulbs in the network.")

    events_queue = asyncio.Queue()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        asyncio.gather(spotify_changes_listener(user_id, client_id, client_secret, events_queue),
                       lights_controller(devices, events_queue)))


if __name__ == '__main__':
    # asyncio.run(main())
    main()
