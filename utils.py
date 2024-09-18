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


def visualize_segments(segments: List[float]):

    import matplotlib.pyplot as plt
    import seaborn as sns
    import pandas as pd
    # Convert to DataFrame
    df_segments = pd.DataFrame(segments)


    # Extract durations
    durations = df_segments['duration']
    # Set plot style
    sns.set(style="whitegrid")

    # Plot histogram
    plt.figure(figsize=(10, 6))
    sns.histplot(durations, bins=50, kde=True)
    plt.title('Distribution of Segment Durations')
    plt.xlabel('Duration (seconds)')
    plt.ylabel('Frequency')
    plt.show()
    # Calculate basic statistics
    mean_duration = durations.mean()
    median_duration = durations.median()
    std_duration = durations.std()
    min_duration = durations.min()
    max_duration = durations.max()

    print(f"Mean Duration: {mean_duration:.4f} seconds")
    print(f"Median Duration: {median_duration:.4f} seconds")
    print(f"Standard Deviation: {std_duration:.4f} seconds")
    print(f"Min Duration: {min_duration:.4f} seconds")
    print(f"Max Duration: {max_duration:.4f} seconds")
    # Calculate percentiles
    percentiles = [10, 25, 50, 75, 90]
    percentile_values = durations.quantile([p/100 for p in percentiles])

    print("Percentile Durations:")
    for p, value in zip(percentiles, percentile_values):
        print(f"{p}th percentile: {value:.4f} seconds")
    # Calculate percentiles
    percentiles = [10, 25, 50, 75, 90]
    percentile_values = durations.quantile([p/100 for p in percentiles])

    print("Percentile Durations:")
    for p, value in zip(percentiles, percentile_values):
        print(f"{p}th percentile: {value:.4f} seconds")
    # Define outlier threshold (e.g., below 5th percentile)
    outlier_threshold = durations.quantile(0.05)
    print(f"Outlier Threshold (5th percentile): {outlier_threshold:.4f} seconds")

    # Count outliers
    outliers = durations[durations < outlier_threshold]
    print(f"Number of Outliers: {len(outliers)}")

    min_segment_duration = durations.quantile(0.25)
    print(f"Optimal MIN_SEGMENT_DURATION set to 25th percentile: {min_segment_duration:.4f} seconds")

    min_segment_duration = max(mean_duration - std_duration, 0.1)  # Ensure it's not negative
    print(f"Optimal MIN_SEGMENT_DURATION set to Mean - Std Dev: {min_segment_duration:.4f} seconds")

    from sklearn.cluster import KMeans
    import numpy as np

    # Reshape data for clustering
    X = durations.values.reshape(-1, 1)

    # Apply K-Means with 2 clusters
    kmeans = KMeans(n_clusters=2, random_state=42).fit(X)

    # Identify which cluster is shorter
    cluster_centers = kmeans.cluster_centers_.flatten()
    short_cluster = cluster_centers.argmin()
    min_segment_duration = X[kmeans.labels_ == short_cluster].max()  # Maximum duration in short cluster

    print(f"Optimal MIN_SEGMENT_DURATION set using K-Means: {min_segment_duration:.4f} seconds")

    def dynamic_min_segment_duration(recent_durations, window_size=100, multiplier=1.0):
        """
        Calculates a dynamic MIN_SEGMENT_DURATION based on the moving average of recent durations.

        :param recent_durations: List or array of recent segment durations.
        :param window_size: Number of recent durations to consider.
        :param multiplier: Multiplier to adjust the threshold.
        :return: Calculated MIN_SEGMENT_DURATION.
        """
        if len(recent_durations) < window_size:
            window = recent_durations
        else:
            window = recent_durations[-window_size:]
        moving_avg = np.mean(window)
        return moving_avg * multiplier

    # Example usage
    recent_durations = durations.tolist()  # Or maintain a separate list for recent durations
    min_segment_duration = dynamic_min_segment_duration(recent_durations, window_size=100, multiplier=0.8)
    print(f"Dynamic MIN_SEGMENT_DURATION: {min_segment_duration:.4f} seconds")
