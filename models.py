from dataclasses import dataclass
from typing import Dict, Any, Union, List, Tuple

# Type alias for raw responses from Spotify's API to improve readability
RawSpotifyResponse = Dict[str, Any]

# Define a type alias for colors, which are represented as tuples of three integers (RGB values)
Color = Tuple[int, int, int]

@dataclass
class EventSongChanged:
    """
    Represents an event where a new song has started playing.

    Attributes:
        analysis: A dictionary containing the analysis of the current song from Spotify's API.
        progress_time_ms: The progress in seconds of the current song.
    """
    analysis: RawSpotifyResponse
    progress_time_ms: float

@dataclass
class EventAdjustProgressTime:
    """
    Represents an event to adjust the start time of the current song.

    This can be useful to synchronize the application's state with the actual playback time.

    Attributes:
        progress_time_ms: The progress in seconds of the current song.
    """
    progress_time_ms: float

@dataclass
class EventStop:
    """
    Represents an event indicating that playback has stopped.
    """
    pass

@dataclass
class Device:
    """
    Represents a smart light device.

    Attributes:
        ip_address: The IP address of the device.
        port: The port number used for communication with the device.
        model: The model identifier of the device.
    """
    ip_address: str
    port: int
    model: str

@dataclass
class ColorTransition:
    """
    Represents a transition to a new color for a device.

    Attributes:
        color: The new color to transition to, represented as an RGB tuple.
        duration: The duration of the transition in milliseconds.
    """
    color: Color
    duration: int

# Define a type alias for a list of color transitions, which can be used to represent a sequence of color changes
ColorTransitions = List[ColorTransition]

# Define a union type for all possible event types that can be processed by the application
Event = Union[EventSongChanged, EventAdjustProgressTime, EventStop]
