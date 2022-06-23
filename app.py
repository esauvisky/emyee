#!/usr/bin/env python3
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

def timeit(func):
    """
    Decorator to time and report elapsed time of functions
    """
    def wrapped(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        end = time.time()
        logger.debug("Function '{}' executed in {:f} s", func.__name__, end - start)
        return result
    return wrapped

@timeit
def setup_logging(level = "DEBUG", show_module = False):
    """
    Setups better log format for loguru
    """
    logger.remove(0)  # Remove the default logger
    log_level = level
    log_fmt = "<green>["
    log_fmt += "{file:10.10}…:{line:<3} | " if show_module else ""
    log_fmt += "{time:HH:mm:ss.SSS}]</green> <level>{level: <8}</level> | <level>{message}</level>"
    logger.add(sys.stderr, level=log_level, format=log_fmt, colorize=True, backtrace=True, diagnose=True)

setup_logging("DEBUG")

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
            analysis = await _get_audio_analysis(session, current_id)
            yield EventSongChanged(analysis, _get_start_time(current, request_time))
        else:
            yield EventAdjustStartTime(_get_start_time(current, request_time))

        await asyncio.sleep(SPOTIFY_CHANGES_LISTENER_DEALY)


async def spotify_changes_listener(user_id: str,
                                   client_id: str,
                                   client_secret: str,
                                   events_queue: asyncio.Queue[Event]) -> NoReturn:
    while True:
        # I'm too lazy to make that async
        token = prompt_for_user_token(user_id, SPOTIFY_SCOPE,
                                      client_id=client_id,
                                      client_secret=client_secret,
                                      redirect_uri='http://localhost:8000/')
        headers = {'Authorization': f'Bearer {token}'}
        async with aiohttp.ClientSession(headers=headers) as session:
            try:
                async for event in _listen_to_spotify_changes(session):
                    await events_queue.put_nowait(event)
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


async def make_send_to_device(device) -> Callable[[Colors]]:
    def send_to_device(colors: Colors) -> None:
        if device.bulb_type.name == "WhiteTemp":
            logger.debug(f"Setting {device._ip} brightness to {((colors[0][0] + colors[0][1] + colors[0][2]) / 3)}...")
            device.set_brightness((colors[0][0] + colors[0][1] + colors[0][2]) / 3)
        else:
            logger.debug(f"Setting {device._ip} color to {(colors[0][0], colors[0][1], colors[0][2])}...")
            device.set_rgb(colors[0][0], colors[0][1], colors[0][2])

    return send_to_device


# Light collors selector, spooky, more details in the notebook
SCALE = (50, 100, 150)
BASE_COLOR_MULTIPLIER = 100
LOUDNESS_MULTIPLIER = 1.5


def _normalize(pv: float) -> float:
    if pv < 0:
        return 0.
    elif pv > 255:
        return 255.
    else:
        return pv


_scale_pixel = lambda p: (int(_normalize(p[0]) * SCALE[0] / 255),
                          int(_normalize(p[1]) * SCALE[1] / 255),
                          int(_normalize(p[2]) * SCALE[2] / 255))


def make_get_current_colors(analysis: RawSpotifyResponse, leds: int) -> Callable[[float], Colors]:
    def make_get_current(name):
        keys = [x['start'] for x in analysis[name]]
        key_to_x = {x['start']: x for x in analysis[name]}
        return lambda t: key_to_x[keys[bisect_left(keys, t) - 1]]

    get_current_segmnet = make_get_current('segments')
    get_current_section = make_get_current('sections')
    get_current_beat = make_get_current('beats')

    def make_scale(name):
        xs = [x[name] for x in analysis['sections']]
        min_xs = min(xs)
        max_xs = max(xs)
        return lambda x: (x - min_xs) / (max_xs - min_xs)

    scale_loudness = make_scale('loudness')
    scale_tempo = make_scale('tempo')

    def get_current_colors(t):
        segment = get_current_segmnet(t)
        section = get_current_section(t)
        beat = get_current_beat(t)

        beat_color = BASE_COLOR_MULTIPLIER * (t - beat['start'] + beat['duration']) / beat['duration']
        tempo_color = BASE_COLOR_MULTIPLIER * scale_tempo(section['tempo'])
        pitch_colors = [BASE_COLOR_MULTIPLIER * p for p in segment['pitches']]

        loudness_multiplier = 1 + LOUDNESS_MULTIPLIER * scale_loudness(section['loudness'])

        colors = ((beat_color * loudness_multiplier,
                   tempo_color * loudness_multiplier,
                   pitch_colors[n // (leds // 12)] * loudness_multiplier)
                  for n in range(leds))

        if section['mode'] == 0:
            order = (0, 1, 2)
        elif section['mode'] == 1:
            order = (1, 2, 0)
        else:
            order = (2, 0, 1)

        ordered_colors = ((color[order[0]], color[order[1]], color[order[2]])
                          for color in colors)

        return [_scale_pixel(color) for color in ordered_colors]

    return get_current_colors


def get_empty_colors(leds: int) -> Colors:
    return [(0,) * 3] * leds


# Events listener, device controller
CONTROLLER_TICK = 0.01
CONTROLLER_ERROR_DELAY = 1


async def _events_to_colors(leds: int, events_queue: asyncio.Queue[Event]) -> AsyncIterable[Colors]:
    get_current_colors = None
    start_time = 0
    event = EventStop()
    while True:
        await asyncio.sleep(CONTROLLER_TICK)

        while not events_queue.empty():
            event = events_queue.get_nowait()

        if isinstance(event, EventSongChanged):
            start_time = event.start_time
            get_current_colors = make_get_current_colors(event.analysis, leds)
        elif isinstance(event, EventAdjustStartTime):
            start_time = event.start_time
        elif isinstance(event, EventStop):
            get_current_colors = None

        if get_current_colors is None:
            yield get_empty_colors(leds)
        else:
            yield get_current_colors(time.time() - start_time)


async def lights_controller(devices: List[yeelight.Bulb],
                            leds: int,
                            events_queue: asyncio.Queue[Event]) -> NoReturn:
    while True:
            try:
                async for colors in _events_to_colors(leds, events_queue):
                    for device in devices:
                        send_to_device = await make_send_to_device(device)
                        send_to_device(colors)
            except Exception:
                logging.exception("Something went wrong with lights_controller")
                await asyncio.sleep(CONTROLLER_ERROR_DELAY)


# Glue
def main():
    dotenv.load_dotenv(".env")
    user_id = os.environ['USER_ID']
    client_id = os.environ['CLIENT_ID']
    client_secret = os.environ['CLIENT_SECRET']
    leds = int(os.environ['LEDS'])

    logger.info("Discovering bulbs...")
    devices = yeelight.discover_bulbs(5)
    logger.info(f"Found {len(devices)} bulbs in the network.")
    bulbs = [yeelight.Bulb(device["ip"], device["port"], effect="sudden", auto_on=True) for device in devices]
    for bulb in bulbs:
        logger.info(f"Setting music mode to {bulb._ip}")
        try:
            bulb.stop_music()
        except:
            pass
        finally:
            bulb.start_music()

    events_queue = asyncio.Queue()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.gather(
        spotify_changes_listener(user_id, client_id, client_secret, events_queue),
        lights_controller(bulbs, leds, events_queue)
    ))


if __name__ == '__main__':
    # asyncio.run(main())
    main()

