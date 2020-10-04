from __future__ import annotations

import array
import asyncio
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
from bottle import run, post, request
import traceback
from yaml import load, Loader
import math

bulbs = []
in_use = []
previous = {}

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


class Bulb(yeelight.Bulb):

    def __init__(self, ip, min_temp=1700, effect="smooth", max_temp=6500, duration=50):
        self.min_temp = min_temp
        self.effect = effect
        self.duration = duration
        self.max_temp = max_temp
        super().__init__(ip)

    def set_brightness(self, brightness):
        super().set_brightness(brightness)
        print('Brightness set to', brightness, 'percent')

    def set_color_temp(self, color_temp):
        if color_temp >= self.max_temp:
            super().set_color_temp(self.max_temp)
            print('Reached highest color temperature of', self.max_temp, 'Kelvin')
        elif color_temp <= self.min_temp:
            super().set_color_temp(self.min_temp)
            print('Reached lowest color temperature of', self.min_temp, 'Kelvin')
        else:
            super().set_color_temp(color_temp)
            print('Color temperature set to', color_temp, 'Kelvin')



Event = Union[EventSongChanged, EventAdjustStartTime, EventStop]

Color = int

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


async def make_send_to_device() -> Callable[[Color]]:
    # loop = asyncio.get_event_loop()

    async def send_to_device(color: Color) -> None:
        global bulbs
        for bulb in bulbs:
            for key, value in previous[bulb._ip].items():
                # try:
                # if key == 'color_temp' and value != color[0]:
                #     bulb.set_color_temp(color[0])
                #     previous[bulb._ip]['color_temp']=color[0]
                # elif key == 'brightness':
                if key == 'brightness': #and round(value) != round(color[1]):
                    # print(f"value {value}")
                    # print(f"color ${color}")
                    # bulb.set_brightness(color[1])
                    try:
                        step = round(((color[1] - value)/10))
                        stepped_brightness = list(range(round(value), round(color[1]), step))
                    except Exception as e:
                        continue
                    else:
                        if not step or not stepped_brightness:
                            continue
#                    step = -1 if round(value) > round(color[1]) else 1
                    #stepped_brightness = list(range(round(value), round(color[1]), step))
                    # print(value)
                    # print(round(color[1]))
                    # print(stepped_brightness)
                    if len(stepped_brightness) >= 1 and bulb not in in_use:
                        stepped_brightness.remove(stepped_brightness[0])
                        print(stepped_brightness)
                        for brightness in stepped_brightness:
                            if not previous[bulb._ip]['brightness'] == brightness:
                                in_use.append(bulb)
                                bulb.set_brightness(brightness)
                                await asyncio.sleep(CONTROLLER_TICK/5)
                                in_use.remove(bulb)
                            previous[bulb._ip]['brightness']=stepped_brightness.pop()
                    elif bulb in in_use:
                        print('bulb is in use')
                            # previous[bulb._
                            # ip]['brightness']=brightness
                        # await asyncio.sleep(CONTROLLER_TICK)
                await asyncio.sleep(CONTROLLER_TICK/10)
                # await asyncio.sleep(CONTROLLER_TICK)
                # except Exception as e:
                #     print(e)

    return send_to_device


# Light collors selector, spooky, more details in the notebook
SCALE = 500
BASE_COLOR_MULTIPLIER = 2700
MIN_BRIGHTNESS = 0
MAX_BRIGHTNESS = 50


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


def make_get_current_color(analysis: RawSpotifyResponse, leds: int) -> Callable[[float], Color]:
    def make_get_current(name):
        keys = [x['start'] for x in analysis[name]]
        key_to_x = {x['start']: x for x in analysis[name]}
        return lambda t: key_to_x[keys[bisect_left(keys, t) - 1]]

    get_current_segment = make_get_current('segments')
    get_current_section = make_get_current('sections')
    get_current_beat = make_get_current('beats')

    def make_scale(name, square=False):
        xs = [x[name] for x in analysis['sections']]
        min_xs = min(xs)
        max_xs = max(xs)
        return lambda x: (x - min_xs) / (max_xs - min_xs)

    scale_loudness = make_scale('loudness')
    scale_tempo = make_scale('tempo')

    def get_current_color(t):
        segment = get_current_segment(t)
        section = get_current_section(t)
        beat = get_current_beat(t)
        # print(section)''
        beat_brightness = (t - beat['start'] + beat['duration']) / beat['duration']
        tempo_color = BASE_COLOR_MULTIPLIER * scale_tempo(section['tempo'])
        pitch_color = [BASE_COLOR_MULTIPLIER * p for p in segment['pitches']]

        # print(beat_brightness')
        loudness_brighness = ((MAX_BRIGHTNESS - MIN_BRIGHTNESS) * scale_loudness(segment['loudness_start']) - 5)
        # loudness_brighness = ((MAX_BRIGHTNESS - MIN_BRIGHTNESS) * scale_loudness(segment['loudness'])) + MIN_BRIGHTNESS
        loudness_brighness = max(round(loudness_brighness + (10 * scale_loudness(segment['loudness_start']) - 5), 0), 0)
        # loudness_brighness = max(round(loudness_brighness + (10 * scale_loudness(segment['loudness_start'] - segment['loudness_max']) / section['loudness'] - 5), 0), 0)
        print(loudness_brighness, scale_loudness(section['loudness']))
        loudness_brighness = max(round(loudness_brighness * scale_loudness(section['loudness']),0), 0)
        # print()
        # loudness_brighness = max(round(beat_brightness + loudness_brighness, 0), 0)
        # print(segment)
        # color = (pitch_color[n // (leds // 12)] * loudness_brighness
        #           for n in range('leds))

        # if section['mode'] == 0:
        #     order = (0, 1, 2)
        # elif section['mode'] == 1:
        #     order = (1, 2, 0)
        # else:
        #     order = (2, 0, 1)

        # ordered_colors = ((color[order[0]], color[order[1]], color[order[2]])
                          # for color in colors)
        # print(_scale_pixel(tempo_color))
        # print(section)
        # global CONTROLLER_TICK
        # print(beat['start'])
        # CONTROLLER_TICK = 60/beat['start']
        # time.sleep(240/beat['start'])
        # for bulb in bulbs:
        # print(beat_color, loudness_brighness)

        return [2700, loudness_brighness]

    return get_current_color


def get_empty_color(leds: int) -> Color:
    return 2700


# Events listener, device controller
CONTROLLER_TICK = 0.1
CONTROLLER_ERROR_DELAY = 1


async def _events_to_color(leds: int, events_queue: asyncio.Queue[Event]) -> AsyncIterable[Color]:
    get_current_color = None
    start_time = 0
    event = EventStop()
    while True:
        await asyncio.sleep(CONTROLLER_TICK)

        while not events_queue.empty():
            event = events_queue.get_nowait()

        if isinstance(event, EventSongChanged):
            start_time = event.start_time
            get_current_color = make_get_current_color(event.analysis, leds)
        elif isinstance(event, EventAdjustStartTime):
            start_time = event.start_time
        elif isinstance(event, EventStop):
            get_current_color = None

        if get_current_color is None:
            yield get_empty_color(leds)
        else:
            yield get_current_color(time.time() - start_time)


async def lights_controller(device_ip: str,
                            device_port: int,
                            leds: int,
                            events_queue: asyncio.Queue[Event]) -> NoReturn:
    global bulbs
    with open('config.yaml', 'r') as config_file:
        config = load(config_file, Loader=Loader)
        print('Initializing...')
        for bulb_config in config["bulbs"]:
            ip = bulb_config["ip"]
            min_temp = bulb_config.get("min_temp")
            max_temp = bulb_config.get("max_temp")

            bulb = Bulb(ip, min_temp, max_temp)
            try:
                bulb.start_music()
            except Exception as e:
                print(f"Error when starting bulb {ip}")
            else:
                print(f"Initializing Yeelight at {ip}")
                bulbs.append(bulb)
                previous[ip] = {'color_temp': 2700,
                            'brightness': MIN_BRIGHTNESS,
                            'in_use': False}

    while True:
        send_to_device = await make_send_to_device()
        try:
            async for color in _events_to_color(leds, events_queue):
                await send_to_device(color)
        except Exception:
            logging.exception("Something went wrong with lights_controller")
            await asyncio.sleep(CONTROLLER_ERROR_DELAY)

# Glue
def main():
    device_ip = os.environ['DEVICE_IP']
    device_port = int(os.environ['DEVICE_PORT'])
    user_id = os.environ['USER_ID']
    client_id = os.environ['CLIENT_ID']
    client_secret = os.environ['CLIENT_SECRET']
    leds = int(os.environ['LEDS'])

    events_queue = asyncio.Queue()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.gather(
        spotify_changes_listener(user_id, client_id, client_secret, events_queue),
        lights_controller(device_ip, device_port, leds, events_queue),
    ))


if __name__ == '__main__':
    main()
