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
import numpy as np
from pprint import pprint

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
        # print(bulb)
        for bulb in bulbs:
            for key, value in previous[bulb._ip].items():
                if key == 'brightness': #and round(value) != round(color[1]):
                    previous_value=previous[bulb._ip]['brightness']
                    next_value=color[1]
                    time_to_spend=color[0]
                    # print(time_to_spend)
                    # interval=0.1
                    step_size = 0.2
                    # print(step_size)
                    if next_value < previous_value:
                        step_size = 0 - step_size
                    stepped_brightness = np.arange(previous_value, next_value, step_size).tolist()
                    number_of_steps = len(stepped_brightness)
                    # print(previous_value, next_value)
    #                 if abs(next_value - previous_value) <= 2:
    #                     continue

                    count = 0
                    # while True:
                    #     count += 1
                    #     print(count)
                    #     stepped_brightness = np.arange(previous_value, next_value, step_size).tolist()
                    #     number_of_steps = len(stepped_brightness)
                    #     try:
                    #         interval = time_to_spend / number_of_steps
                    #     except:
                    #         pass

                    #     if interval >= 0.1 and interval <= 1:
                    #         print(f'using interval {interval} with step size {step_size} and {number_of_steps} steps')
                    #     elif interval < 0.1 and not interval :
                    #         step_size = step_size * 2
                    #     elif interval > 1:
                    #         step_size = step_size / 2

                    #     if count > 50:
                    #         break
                    # if number_of_steps == 5:
                    #     continue
                    print(stepped_brightness)
                    # stepped_brightness = np.linspace(previous_value, next_value, num=number_of_steps).tolist()
                    # print(f'pre: {stepped_brightness}')
                    # print(min(range(len(stepped_brightness)), key=lambda i: abs(stepped_brightness[i]-previous_value)))
                    # stepped_brightness = stepped_brightness[min(range(len(stepped_brightness)), key=lambda i: abs(stepped_brightness[i]-previous_value)):]
                    # print(f'pos {stepped_brightness}')
                    if number_of_steps:
                        # interval = time_to_spend / number_of_steps
                        # print(interval)
                        # print(f'number of steps: {len(stepped_brightness)}, interval: {interval}, total time: {time_to_spend}')
                        print(f'number of steps: {number_of_steps}, interval: {interval}, total time: {time_to_spend}')
                        for brightness in stepped_brightness:
                            brightness = round(brightness, 2)
                            if not previous[bulb._ip]['brightness'] == brightness:
                                bulb.set_brightness(brightness)
                                await asyncio.sleep(interval)
                                # time.sleep(interval)
                                # previous[bulb._ip]['brightness']=brightness
                                previous[bulb._ip]['brightness']=stepped_brightness.pop()
                        # await asyncio.sleep(0)
                # except Exception as e:
                #     print(e)

    return send_to_device


# Light collors selector, spooky, more details in the notebook
SCALE = 500
BASE_COLOR_MULTIPLIER = 2700
MIN_BRIGHTNESS = 1
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
        # print(keys)
        # print(key_to_x)
        return lambda t: key_to_x[keys[bisect_left(keys, t) - 1]]
    def make_get_next(name):
        keys = [x['start'] for x in analysis[name]]
        key_to_x = {x['start']: x for x in analysis[name]}
        # print(keys)
        # print(key_to_x)
        return lambda t, n: key_to_x[keys[bisect_left(keys, t) - 1 + n]]

    get_current_segment = make_get_current('segments')
    get_current_section = make_get_current('sections')
    get_current_beat = make_get_current('beats')

    get_next_n = make_get_next('segments')

    def make_scale(name, square=False):
        xs = [x[name] for x in analysis['sections']]
        min_xs = min(xs)
        max_xs = max(xs)
        return lambda x: (x - min_xs) / (max_xs - min_xs)

    scale_loudness = make_scale('loudness')
    scale_tempo = make_scale('tempo')
    previous_final_fucking_big_segment = None

    def get_current_color(t):
        segment = get_current_segment(t)
        section = get_current_section(t)
        beat = get_current_beat(t)
        final_fucking_big_segment = {
            'start': segment['start'],
            'duration': segment['duration'],
            'final_frigging_loudness': segment['loudness_start']
        }
        n = 1
        new_duration = 0
        numerador = segment['loudness_start'] * segment['duration']
        while new_duration < 3:       # while duration is less than 3 seconds, keep bunching up segments
            # try:
            # except:
            #     next_segment = get_next_n(t, n-1)

            next_segment = get_next_n(t, n)
            new_duration += final_fucking_big_segment['duration'] + next_segment['duration']
            print(f"duration: {next_segment['duration']}\t|| [Loudnessesesses] start: {next_segment['loudness_start']}\tmax: {next_segment['loudness_max']}\tmax_time: {next_segment['loudness_max_time']}")
            numerador += next_segment['loudness_start'] * next_segment['duration']
            n += 1


        final_fucking_big_segment['duration'] = new_duration
        final_fucking_big_segment['final_frigging_loudness'] = round(numerador / new_duration, 2)
        # if previous_final_fucking_big_segment.index('final_frigging_loudness') and previous_final_fucking_big_segment['final_frigging_loudness'] == final_fucking_big_segment['final_frigging_loudness']:
        #     return [final_fucking_big_segment['duration'], final_fucking_big_segment['final_frigging_loudness']

        # print(final_fucking_big_segment)
        # print(f'minha fucking big segment é: ')
        # pprint(final_fucking_big_segment)
        # exit()
        # exit()
        # beat_brightness = (t - beat['start'] + beat['duration']) / beat['duration']
        # tempo_color = BASE_COLOR_MULTIPLIER * scale_tempo(section['tempo'])
        # pitch_color = [BASE_COLOR_MULTIPLIER * p for p in segment['pitches']]

        # print(beat_brightness')
        # loudness_brighness = ((MAX_BRIGHTNESS - MIN_BRIGHTNESS) * scale_loudness(segment['loudness_start']) - 5)
        # loudness_brighness = max(loudness_brighness + (10 * scale_loudness(segment['loudness_start']) - 5),0) # very slow
        # loudness_brighness = max(round(loudness_brighness + (10 * scale_loudness(segment['loudness_start'] - sfinal_fucking_big_segment[egment['loudness_max']) / section['loudness'] - 5), 0), 0) # very quick

        loudness_brighness = round(min(max(scale_loudness(final_fucking_big_segment['final_frigging_loudness']), 0) * 100, 100),2)
        # loudness_brighness = max(round(loudness_brighness + 10 * scale_loudness(final_fucking_big_segment['final_frigging_loudness'] / section['loudness'] - 5), 0),0) # very quick
        # print(loudness_brighness, scale_loudness(section['loudness']))
        # loudness_brighness = max(round(loudness_brighness * scale_loudness(section['loudness']),0), 0)
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
        # print(beat_color, loudnzzess_brighness)

        # try:
        #     del segment['pitches']
        #     del segment['timbre']
        # except:
        #     pass
        # print(segment)
        # print(section)
        # print(beat)
        # print()

        return [final_fucking_big_segment['duration'], loudness_brighness]

    return get_current_color


def get_empty_color(leds: int) -> Color:
    return 1, 1


# Events listener, device controller
CONTROLLER_TICK = 0.05
CONTROLLER_ERROR_DELAY = 1


async def _events_to_color(leds: int, events_queue: asyncio.Queue[Event]) -> AsyncIterable[Color]:
    get_current_color = None
    start_time = 0
    event = EventStop()
    while True:
        await asyncio.sleep(CONTROLLER_TICK)
        # await asyncio.sleep(0)

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
            retval = get_current_color(time.time() - start_time)
            await asyncio.sleep(retval[0])
            yield retval
            await asyncio.sleep(0)



async def lights_controller(bulbs,
                            leds: int,
                            events_queue: asyncio.Queue[Event]) -> NoReturn:
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
    user_id = os.environ['USER_ID']
    client_id = os.environ['CLIENT_ID']
    client_secret = os.environ['CLIENT_SECRET']
    leds = int(os.environ['LEDS'])

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

    events_queue = asyncio.Queue()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.gather(
        spotify_changes_listener(user_id, client_id, client_secret, events_queue),
        # *[lights_controller(bulb, leds, events_queue) for bulb in bulbs]
        lights_controller(bulbs, leds, events_queue),
    ))


if __name__ == '__main__':
    main()
