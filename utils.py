import random
import aiohttp
from loguru import logger
import sys
from typing import Dict, Any, List

# Define a type alias for Spotify's raw response for clarity
RawSpotifyResponse = Dict[str, Any]
CONTROLLER_TICK = 0.001
MIN_SEGMENT_DURATION = 0.1
API_CURRENT_PLAYING = 'https://api.spotify.com/v1/me/player/currently-playing'
API_AUDIO_ANALYSIS = 'https://api.spotify.com/v1/audio-analysis/'
SPOTIFY_CHANGES_LISTENER_DELAY = 0.1
SPOTIFY_CHANGES_LISTENER_FAILURE_DELAY = 1
SPOTIFY_REDIRECT_URI = 'http://localhost:8000/'
SPOTIFY_SCOPE = 'user-read-currently-playing,user-read-playback-state'
API_REQUEST_INTERVAL = 1
COLORS = [(255, 102, 129), (204, 0, 203), (232, 62, 62), (102, 0, 102), (0, 0, 204), (59, 0, 104), (0, 0, 102),
          (0, 203, 204), (76, 126, 128), (0, 102, 102), (102, 102, 0), (204, 0, 0), (102, 0, 0), (203, 204, 0),
          (204, 172, 0), (204, 132, 0), (0, 204, 0), (0, 102, 0)]

# Define a vibrant color palette (Hue, Saturation)
VIBRANT_COLOR_PALETTE = [
    (0, 80),    # Red
    (30, 80),   # Orange
    (60, 80),   # Yellow
    (120, 80),  # Green
    (180, 80),  # Cyan
    (220, 80),  # Blue
    (300, 80),  # Magenta
]



def get_vibrant_color(current_hue):
    palette = VIBRANT_COLOR_PALETTE.copy()
    palette = [color for color in palette if abs(color[0] - current_hue) >= 30]
    if not palette:
        palette = VIBRANT_COLOR_PALETTE  # Reset if no colors are sufficiently different
    new_color = random.choice(palette)
    return new_color


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

def merge_short_segments(segments: List[Dict[str, Any]], min_duration: float = MIN_SEGMENT_DURATION) -> List[Dict[str, Any]]:
    """
    Merges consecutive segments whose total duration is less than min_duration.

    :param segments: List of segment dictionaries from Spotify's audio analysis.
    :param min_duration: Minimum duration for a segment in seconds.
    :return: New list of segments with short segments merged.
    """
    if not segments:
        return []

    merged_segments = []
    current_segment = segments[0].copy()

    for next_segment in segments[1:]:
        # Check if the current segment is below the minimum duration
        if current_segment['duration'] < min_duration:
            # Merge with the next segment
            current_segment['duration'] += next_segment['duration']
            # Optionally, update loudness attributes by averaging or other logic
            current_segment['loudness_start'] = (current_segment['loudness_start'] + next_segment['loudness_start']) / 2
            current_segment['loudness_max'] = max(current_segment['loudness_max'], next_segment.get('loudness_max', current_segment['loudness_max']))
            current_segment['loudness_end'] = next_segment.get('loudness_end', current_segment['loudness_end'])
            # Update other attributes as needed (pitches, timbre, etc.)
            current_segment['pitches'] = [(c + n) / 2 for c, n in zip(current_segment['pitches'], next_segment['pitches'])]
            current_segment['timbre'] = [(c + n) / 2 for c, n in zip(current_segment['timbre'], next_segment['timbre'])]
            # Continue merging if still below min_duration
        else:
            merged_segments.append(current_segment)
            current_segment = next_segment.copy()

    # Append the last segment
    merged_segments.append(current_segment)

    return merged_segments


def visualize_segments(bars: List[float]):

    import matplotlib.pyplot as plt
    import seaborn as sns
    import pandas as pd
    # Convert to DataFrame
    df_bars = pd.DataFrame(bars)


    # Extract durations
    confidences = df_bars['confidence']
    # Set plot style
    sns.set(style="whitegrid")

    # Plot histogram
    plt.figure(figsize=(10, 6))
    sns.histplot(confidences, bins=50, kde=True)
    plt.title('Distribution of Segment confidences')
    plt.xlabel('confidence (seconds)')
    plt.ylabel('Frequency')
    plt.show()
    # Calculate basic statistics
    mean_confidence = confidences.mean()
    median_confidence = confidences.median()
    std_confidence = confidences.std()
    min_confidence = confidences.min()
    max_confidence = confidences.max()

    print(f"Mean confidence: {mean_confidence:.4f} seconds")
    print(f"Median confidence: {median_confidence:.4f} seconds")
    print(f"Standard Deviation: {std_confidence:.4f} seconds")
    print(f"Min confidence: {min_confidence:.4f} seconds")
    print(f"Max confidence: {max_confidence:.4f} seconds")
    # Calculate percentiles
    percentiles = [10, 25, 50, 75, 90]
    percentile_values = confidences.quantile([p/100 for p in percentiles])

    print("Percentile confidences:")
    for p, value in zip(percentiles, percentile_values):
        print(f"{p}th percentile: {value:.4f} seconds")
    # Calculate percentiles
    percentiles = [10, 25, 50, 75, 90]
    percentile_values = confidences.quantile([p/100 for p in percentiles])

    print("Percentile confidences:")
    for p, value in zip(percentiles, percentile_values):
        print(f"{p}th percentile: {value:.4f} seconds")
    # Define outlier threshold (e.g., below 5th percentile)
    outlier_threshold = confidences.quantile(0.05)
    print(f"Outlier Threshold (5th percentile): {outlier_threshold:.4f} seconds")

    # Count outliers
    outliers = confidences[confidences < outlier_threshold]
    print(f"Number of Outliers: {len(outliers)}")

    min_segment_confidence = confidences.quantile(0.25)
    print(f"Optimal MIN_SEGMENT_confidence set to 25th percentile: {min_segment_confidence:.4f} seconds")

    min_segment_confidence = max(mean_confidence - std_confidence, 0.1)  # Ensure it's not negative
    print(f"Optimal MIN_SEGMENT_confidence set to Mean - Std Dev: {min_segment_confidence:.4f} seconds")

    from sklearn.cluster import KMeans
    import numpy as np

    # Reshape data for clustering
    X = confidences.values.reshape(-1, 1)

    # Apply K-Means with 2 clusters
    kmeans = KMeans(n_clusters=2, random_state=42).fit(X)

    # Identify which cluster is shorter
    cluster_centers = kmeans.cluster_centers_.flatten()
    short_cluster = cluster_centers.argmin()
    min_segment_confidence = X[kmeans.labels_ == short_cluster].max()  # Maximum confidence in short cluster

    print(f"Optimal MIN_SEGMENT_confidence set using K-Means: {min_segment_confidence:.4f} seconds")

    def dynamic_min_segment_confidence(recent_confidences, window_size=100, multiplier=1.0):
        """
        Calculates a dynamic MIN_SEGMENT_confidence based on the moving average of recent confidences.

        :param recent_confidences: List or array of recent segment confidences.
        :param window_size: Number of recent confidences to consider.
        :param multiplier: Multiplier to adjust the threshold.
        :return: Calculated MIN_SEGMENT_confidence.
        """
        if len(recent_confidences) < window_size:
            window = recent_confidences
        else:
            window = recent_confidences[-window_size:]
        moving_avg = np.mean(window)
        return moving_avg * multiplier

    # Example usage
    recent_confidences = confidences.tolist()  # Or maintain a separate list for recent confidences
    min_segment_confidence = dynamic_min_segment_confidence(recent_confidences, window_size=100, multiplier=0.8)
    print(f"Dynamic MIN_SEGMENT_confidence: {min_segment_confidence:.4f} seconds")

# utils.py
from typing import Tuple

def _beats_in_window(beats: List[Dict[str, Any]], start: float, end: float) -> List[Dict[str, Any]]:
    # Beats whose start is within the bar window
    return [b for b in beats if start <= b['start'] < end]

def _beats_per_bar(beats: List[Dict[str, Any]], bar: Dict[str, Any]) -> int:
    return len(_beats_in_window(beats, bar['start'], bar['start'] + bar['duration']))

def _next_bar_index(bars: List[Dict[str, Any]], t: float) -> int:
    # First bar whose window still covers t or starts after it
    for i, b in enumerate(bars):
        if b['start'] <= t < b['start'] + b['duration']:
            return i
        if b['start'] > t:
            return i
    return len(bars) - 1

def min_segment_duration_from_future_measures(
    beats: List[Dict[str, Any]],
    bars: List[Dict[str, Any]],
    current_time: float,
    lookahead_bars: int = 16,
    half_factor: float = 0.24,
) -> Tuple[float, int, float]:
    """
    Scan forward from the bar at/after current_time. Collect consecutive bars
    that have the same beats-per-measure as the first such bar. Average their
    durations, then return a MIN_SEGMENT_DURATION that's a bit less than half
    a measure to favor changes every ~two beats.

    Returns:
        (min_segment_duration_seconds, beats_per_measure, avg_measure_duration_seconds)
    """
    if not bars:
        # absolute fallback
        return max(0.05, CONTROLLER_TICK * 8), 4, 0.5

    i0 = _next_bar_index(bars, current_time)
    first_bar = bars[i0]
    bpm_first = _beats_per_bar(beats, first_bar) or 4  # fallback if detection fails

    total_dur = 0.0
    count = 0
    for b in bars[i0 : min(len(bars), i0 + lookahead_bars)]:
        bpm_here = _beats_per_bar(beats, b) or bpm_first
        if bpm_here != bpm_first and count > 0:
            break
        total_dur += b['duration']
        count += 1

    avg_measure = (total_dur / count) if count else first_bar['duration']

    # "A little less than half a measure"
    min_seg = avg_measure * half_factor

    # Safety lower bound – don’t let it get absurdly small
    min_seg = max(min_seg, CONTROLLER_TICK * 8, 0.05)
    return float(min_seg), int(bpm_first), float(avg_measure)
