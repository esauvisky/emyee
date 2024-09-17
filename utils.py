import random
import aiohttp
from loguru import logger
import sys
from typing import Dict, Any, List

# Define a type alias for Spotify's raw response for clarity
RawSpotifyResponse = Dict[str, Any]
CONTROLLER_TICK = 0.001
MIN_SEGMENT_DURATION = 0.2
API_CURRENT_PLAYING = 'https://api.spotify.com/v1/me/player/currently-playing'
API_AUDIO_ANALYSIS = 'https://api.spotify.com/v1/audio-analysis/'
SPOTIFY_CHANGES_LISTENER_DELAY = 0.001
SPOTIFY_CHANGES_LISTENER_FAILURE_DELAY = 1
SPOTIFY_REDIRECT_URI = 'http://localhost:8000/'
SPOTIFY_SCOPE = 'user-read-currently-playing,user-read-playback-state'
API_REQUEST_INTERVAL = 0.5
COLORS = [(255, 102, 129), (204, 0, 203), (232, 62, 62), (102, 0, 102), (0, 0, 204), (59, 0, 104), (0, 0, 102),
          (0, 203, 204), (76, 126, 128), (0, 102, 102), (102, 102, 0), (204, 0, 0), (102, 0, 0), (203, 204, 0),
          (204, 172, 0), (204, 132, 0), (0, 204, 0), (0, 102, 0)]

def setup_logging(log_lvl="DEBUG", options={}):
    file = options.get("file", False)
    function = options.get("function", False)
    process = options.get("process", False)
    thread = options.get("thread", False)

    log_fmt = (u"<n><d><level>{time:HH:mm:ss.SSS} | " +
               f"{'{file:>15.15}' if file else ''}" +
               f"{'{function:>15.15}' if function else ''}" +
               f"{':{line:<4} | ' if file or function else ''}" +
               f"{'{process.name:>12.12} | ' if process else ''}" +
               f"{'{thread.name:<11.11} | ' if thread else ''}" +
               u"{level:1.1} | </level></d></n><level>{message}</level>")

    logger.configure(
        handlers=[{
            "sink": lambda x: print(x, end=""),
            "level": log_lvl,
            "format": log_fmt,
            "colorize": True,
            "backtrace": True,
            "diagnose": True
        }],
        levels=[
            {"name": "TRACE", "color": "<white><dim>"},
            {"name": "DEBUG", "color": "<cyan><dim>"},
            {"name": "INFO", "color": "<white>"}
        ]
    )  # type: ignore # yapf: disable

def get_new_color(current_color):
    """
    Generates a new color, ensuring it is different from the current color.
    """
    # index = COLORS.index(current_color)
    # new_color = COLORS[(index+1) % len(COLORS)]

    colors = list(COLORS)
    if current_color in colors:
        colors.remove(current_color)  # Remove the current color to ensure the new color is different
    new_color = random.choice(colors)
    ## Optionally, adjust the new color slightly to add variety
    # adjusted_color = tuple(max(0, min(255, component + random.randint(-20, 20))) for component in new_color)
    return new_color


async def get_current_playing(session: aiohttp.ClientSession, token: str) -> RawSpotifyResponse:
    """
    Retrieves the currently playing track from Spotify.
    """
    headers = {'Authorization': f'Bearer {token}'}
    url = 'https://api.spotify.com/v1/me/player/currently-playing'
    async with session.get(url, headers=headers) as response:
        return await response.json()


async def get_audio_analysis(session: aiohttp.ClientSession, token: str, track_id: str) -> RawSpotifyResponse:
    """
    Retrieves the audio analysis for a given track ID from Spotify.
    """
    headers = {'Authorization': f'Bearer {token}'}
    url = f'https://api.spotify.com/v1/audio-analysis/{track_id}'
    async with session.get(url, headers=headers) as response:
        return await response.json()

def get_random_item(items):
    return random.choice(items)

def get_next_item(items, current_time, key="start"):
    items_sorted_by_start = sorted(items, key=lambda x: x[key])
    remaining_items = [item for item in items_sorted_by_start if item[key] > current_time]
    return remaining_items[1] if len(remaining_items) > 1 else None

def get_current_item(items, current_time, key="start"):
    items_sorted_by_start = sorted(items, key=lambda x: x[key])
    remaining_items = [item for item in items_sorted_by_start if item[key] > current_time]
    return remaining_items[0] if len(remaining_items) > 0 else None


def calculate_segment_loudness(segment: Dict[str, Any]) -> float:
    """
    Calculates the average loudness of a segment.

    :param segment: A segment dictionary from Spotify's audio analysis.
    :return: Average loudness.
    """
    loudness_start = segment['loudness_start']
    loudness_max = segment['loudness_max']
    duration = segment['duration']

    if duration == 0:
        return loudness_start

    # Simple average of start and max loudness
    average_loudness = (loudness_start + loudness_max) / 2
    return average_loudness

def merge_short_segments_recursive(segments: List[Dict[str, Any]], min_duration: float = MIN_SEGMENT_DURATION) -> List[Dict[str, Any]]:
    """
    Recursively merges consecutive segments until all segments meet the minimum duration.

    :param segments: List of segment dictionaries.
    :param min_duration: Minimum duration for a segment in seconds.
    :return: Merged list of segments.
    """
    if not segments:
        return []

    merged_segments = []
    current_segment = segments[0].copy()

    for next_segment in segments[1:]:
        if current_segment['duration'] < min_duration:
            # Merge with the next segment
            current_segment['duration'] += next_segment['duration']
            current_segment['loudness_start'] = (current_segment['loudness_start'] + next_segment['loudness_start']) / 2
            current_segment['loudness_max'] = max(current_segment['loudness_max'], next_segment.get('loudness_max', current_segment['loudness_max']))
            current_segment['loudness_end'] = next_segment.get('loudness_end', current_segment['loudness_end'])
            current_segment['pitches'] = [
                (c + n) / 2 for c, n in zip(current_segment['pitches'], next_segment['pitches'])
            ]
            current_segment['timbre'] = [
                (c + n) / 2 for c, n in zip(current_segment['timbre'], next_segment['timbre'])
            ]
        else:
            merged_segments.append(current_segment)
            current_segment = next_segment.copy()

    merged_segments.append(current_segment)

    # Check if any merged segments are still below the threshold
    if any(seg['duration'] < min_duration for seg in merged_segments):
        if len(merged_segments) == 1:
            # Only one segment left, cannot merge further
            return merged_segments
        else:
            # Merge again recursively
            return merge_short_segments_recursive(merged_segments, min_duration)
    else:
        return merged_segments
