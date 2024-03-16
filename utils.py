import random
import aiohttp
from loguru import logger
import sys
from typing import Dict, Any

# Define a type alias for Spotify's raw response for clarity
RawSpotifyResponse = Dict[str, Any]
CONTROLLER_TICK = 0.01
API_CURRENT_PLAYING = 'https://api.spotify.com/v1/me/player/currently-playing'
API_AUDIO_ANALYSIS = 'https://api.spotify.com/v1/audio-analysis/'
SPOTIFY_CHANGES_LISTENER_DELAY = 0.001
SPOTIFY_CHANGES_LISTENER_FAILURE_DELAY = 0.1
SPOTIFY_REDIRECT_URI = 'http://localhost:8000/'
SPOTIFY_SCOPE = 'user-read-currently-playing,user-read-playback-state'
API_REQUEST_INTERVAL = 5
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
]

def setup_logging(level="DEBUG", show_module=False):
    """
    Setups better log format for loguru.
    """
    logger.remove()  # Remove the default logger
    log_fmt = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
    log_fmt += "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - " if show_module else ""
    log_fmt += "<level>{message}</level>"
    logger.add(sys.stderr, level=level, format=log_fmt, colorize=True, backtrace=True, diagnose=True)

def get_new_color(current_color):
    """
    Generates a new color, ensuring it is different from the current color.
    """
    colors = list(COLORS)
    if current_color in colors:
        colors.remove(current_color)  # Remove the current color to ensure the new color is different
    new_color = random.choice(colors)
    # Optionally, adjust the new color slightly to add variety
    adjusted_color = tuple(max(0, min(255, component + random.randint(-20, 20))) for component in new_color)
    return adjusted_color

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

def get_next_item(items, current_time, key="start"):
    items_sorted_by_start = sorted(items, key=lambda x: x[key])
    remaining_items = [item for item in items_sorted_by_start if item[key] > current_time]
    return remaining_items[1] if len(remaining_items) > 1 else None

def get_current_item(items, current_time, key="start"):
    items_sorted_by_start = sorted(items, key=lambda x: x[key])
    remaining_items = [item for item in items_sorted_by_start if item[key] > current_time]
    return remaining_items[0] if len(remaining_items) > 0 else None
