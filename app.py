#!/usr/bin/env python3
# shamelessly stolen from https://gist.github.com/nvbn/73b613849cb176ec33057236b2fd4558
from __future__ import annotations

import array
import asyncio
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
SPOTIFY_CHANGES_LISTENER_DEALY = 1
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

        await asyncio.sleep(SPOTIFY_CHANGES_LISTENER_DEALY)


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

async def send_to_device(device, brightness: int, duration: int) -> None:
    logger.trace(f"Setting {device._ip} to {brightness} for {int(duration * 1000)}ms...")
    # device.stop_flow()
    device.duration = int(duration * 1000)
        device.set_brightness(brightness)
        device.set_brightness(brightness)
        # else:
        #     logger.debug(f"Setting {device._ip} color to {(brightness[0][0], brightness[0][1], brightness[0][2])}...")
        #     device.set_rgb(brightness[0][0], brightness[0][1], brightness[0][2])


# Light collors selector, spooky, more details in the notebook
SCALE = (100, 100, 100)
BASE_COLOR_MULTIPLIER = 40
LOUDNESS_MULTIPLIER = 3

    device.set_brightness(brightness)
        # else:
        #     logger.debug(f"Setting {device._ip} color to {(brightness[0][0], brightness[0][1], brightness[0][2])}...")
        #     device.set_rgb(brightness[0][0], brightness[0][1], brightness[0][2])


# Light collors selector, spooky, more details in the notebook
SCALE = (100, 100, 100)
BASE_COLOR_MULTIPLIER = 40
LOUDNESS_MULTIPLIER = 3


def _normalize(pv: float) -> float:
    if pv < 0:
        return 0.
    elif pv > 255:
        return 255.
    else:
        return pv

def make_get_current_colors(analysis: RawSpotifyResponse) -> Callable[[float], Colors]:

    def make_get_current(name):
        keys = [x['start'] for x in analysis[name]]
        key_to_x = {x['start']: x for x in analysis[name]}
        return lambda t: key_to_x[keys[bisect_left(keys, t) - 1]]

    def make_get_next(name):
        keys = [x['start'] for x in analysis[name]]
        key_to_x = {x['start']: x for x in analysis[name]}
        return lambda t, n: key_to_x[keys[bisect_left(keys, t) - 1 + n]]

    get_current_segment = make_get_current('segments')
    get_current_section = make_get_current('sections')
    get_current_beat = make_get_current('beats')
    get_next_n = make_get_next('segments')

    def make_scale(name):
        xs = [x[name] for x in analysis['sections']]
        min_xs = min(xs)
        max_xs = max(xs)
        logger.trace(f"minimum {name} is {min_xs}, maximum {name} is {max_xs}")
        return lambda x: (x-min_xs) / (max_xs-min_xs)

    def make_scale_log(name):
        xs = [10 ** (x[name] / 10) for x in analysis['sections']]
        min_xs = min(xs)
        max_xs = max(xs)
        logger.trace(f"minimum {name} is {min_xs}, maximum {name} is {max_xs}")
        return lambda x: (10 ** (x / 10) - min_xs) / (max_xs - min_xs)

    def get_segment_loudness(segment):
        segment_start_loudness = segment['loudness_start'] * (segment['duration'] - segment['loudness_max_time']) / segment['duration']
        segment_max_loudness = segment['loudness_max'] * segment['loudness_max_time'] / segment['duration']
        return segment_start_loudness + segment_max_loudness

    def make_scale_loudness():
        loudnesses = [get_segment_loudness(x) for x in analysis['segments']]
        min_segment_loudness = min(loudnesses)
        max_segment_loudness = max(loudnesses)
        logger.debug(f"minimum loudness is {min_segment_loudness}, maximum is {max_segment_loudness}")
        return lambda x: (x-min_segment_loudness) / (max_segment_loudness-min_segment_loudness)

    scale_section_loudness = make_scale_log('loudness')
    scale_segment_loudness = make_scale_loudness()
    scale_tempo = make_scale('tempo')

    def get_current_loudness(t):
        segment = get_current_segment(t)
        section = get_current_section(t)
        beat = get_current_beat(t)

        duration = segment["duration"]

        segment_loudness = scale_segment_loudness(get_segment_loudness(segment))
        # while duration < 0.5:       # while duration is less than .5 seconds, keep bunching up segments
        #     n += 1
        #     next_segment = get_next_n(t, n)
        #     duration += next_segment['duration']
        #     scaled_loudness += scale_segment_loudness(get_segment_loudness(next_segment))
        #     if n > 50:
        #         break
        # scaled_loudness /= n

        return segment_loudness, scale_section_loudness(section["loudness"]), duration

    return get_current_loudness

# Events listener, device controller
CONTROLLER_TICK = 0.01
CONTROLLER_ERROR_DELAY = 1


async def _events_to_colors(events_queue: asyncio.Queue[Event]) -> AsyncIterable[Colors, float]:
    get_current_colors = None
    start_time = 0
    event = EventStop()
    while True:
        await asyncio.sleep(CONTROLLER_TICK)

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



async def lights_controller(devices: List[yeelight.Bulb], events_queue: asyncio.Queue[Event]) -> NoReturn:
    while True:
        last_brightness = 0
        try:
            async for mult_segment, mult_section, duration in _events_to_colors(events_queue):
                # variation = last_loudness - loudness # for loudness = scale_loudness(segment['loudness_start'])

                brightness = int(mult_segment * 15 + mult_section * 25)
                if brightness != last_brightness:
                    logger.debug(f"brightness: {brightness:.0f} | mult_segment: {mult_segment:2.2f} | mult_section: {mult_section:2.2f} | duration: {duration:2.2f}s")
                    for device in devices:
                        asyncio.create_task(send_to_device(device, brightness, duration))
                    last_brightness = brightness
                await asyncio.sleep(CONTROLLER_TICK)

        except Exception:
            logging.exception("Something went wrong with lights_controller")
            await asyncio.sleep(CONTROLLER_ERROR_DELAY)


# Glue
def main():
    dotenv.load_dotenv(".env")
    user_id = os.environ['USER_ID']
    client_id = os.environ['CLIENT_ID']
    client_secret = os.environ['CLIENT_SECRET']
    setup_logging("DEBUG")

    logger.info("Discovering bulbs...")
    bulbs = yeelight.discover_bulbs(5)
    logger.info(f"Found {len(bulbs)} bulbs in the network.")
    devices = []
    for bulb in [yeelight.Bulb(device["ip"], device["port"], effect="smooth", auto_on=False) for device in bulbs]:
        logger.trace(bulb.get_model_specs())
        logger.trace(bulb.get_capabilities())
        logger.trace(bulb.get_properties())
        if bulb.get_capabilities()["model"] == "ct_bulb":
            logger.info(f"Setting music mode to {bulb._ip}")
            bulb.start_music()
            devices.append(bulb)
        else:
            logger.warning(f"Not setting music mode to {bulb._ip} because it's not a ct_bulb bulb")
    logger.success(f"Using {len(devices)} bulbs in the network.")

    events_queue = asyncio.Queue()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        asyncio.gather(spotify_changes_listener(user_id, client_id, client_secret, events_queue),
                       lights_controller(devices, events_queue)))


if __name__ == '__main__':
    # asyncio.run(main())
    main()
