# Emyee

Emyee is a Python application that synchronizes your smart lights with the music you're playing on Spotify. It analyzes the audio data from the Spotify API, including loudness, beats, and sections, and uses that information to create dynamic lighting effects that enhance your music listening experience.

_Or at least, that's what it's supposed to do. In reality, it might just give you a migraine._

## Features

- **Audio Analysis**: Utilizes Spotify's audio analysis API to extract detailed information about the currently playing song, including beats, sections, and loudness levels.
_Because you definitely need to know the exact loudness of that high-pitched scream in your favorite death metal song._

- **Smart Lights Integration**: Supports integration with Yeelight smart bulbs (additional support for other lighting systems can be added in).

- **Smooth Transitions**: Provides smooth transitions between colors and brightness levels, creating an immersive lighting experience. _Or, if you're unlucky, a jarring, seizure-inducing light show._

## Prerequisites

- Python 3.10+
- Spotify account
- Yeelight smart bulbs

## Installation

1. Clone the repository:

```
git clone https://github.com/your-username/spotify-lights.git
```

2. Install the required Python packages:

```
pip install -r requirements.txt
```


3. Set up your Spotify credentials by creating a `.env` file in the project root with the following contents:

```
USER_ID=your_spotify_user_id
CLIENT_ID=your_spotify_client_id
CLIENT_SECRET=your_spotify_client_secret
```

Replace the placeholders with your actual Spotify credentials, good luck with that.

## Usage

1. Connect your Yeelight bulbs to your local network. Good luck with that too.
2. Run the application:

```
python main.py
```

The application will discover and initialize the Yeelight bulbs on your network, and start synchronizing the lights with the music playing on your Spotify account, i hope.

## Customization

Playback uses the atmospheric beat-pulse implementation in `light_controller.py`. Edit `ATMOSPHERIC_DB` in `atmospheric.py`; every pulse is `(strength, attack, hold, release)`, with timing measured in fractions of one beat. Because each pulse owns its envelope, one profile can mix slow swells, instant flashes, held peaks, and soft releases. Parts detected in `sections.py` are cut into phrases counted from their first bar (`PHRASE_BARS_CALM` = 4 bars in calm parts, `PHRASE_BARS_LOUD` = 8 in loud ones); each phrase gets its own energy and colour, and "section" below means a phrase. Each section picks the profile whose pulse cycle divides its beat count most evenly, and the cycle restarts on the section's first beat so accents stay on the section's own grid. `sections.py` splits the track into the runs of bars that are most uniform in loudness, timbre and harmony (pitch-class content, `CHROMA_WEIGHT`; an exact change-point fit, `BOUNDARY_PENALTY` per part), so a boundary lands on the bar the sound changes on; `OFF_GRID_PENALTY` makes parts prefer lengths that are a multiple of `PHRASE_GRID_BARS` (4), which keeps a fill bar with the part it ends. Bars `QUIET_BAR_DB` under the median of the `QUIET_LOOKBACK_BARS` before them (breakdowns) hold at their resting level instead of pulsing. The loudest bar of each part with energy ≥ `STROBE_MIN_ENERGY` (never a phrase's last bar) strobes: a new colour every eighth note, brightness held at the section's peak. Over the last `HUE_FADE_BARS` (1) before a colour change the hue crosses linearly into the new colour, landing on the section's first beat, while the beats keep pulsing: each pulse command carries the colour the crossing has reached when it lands. A section shorter than `MIN_COLOR_SECTION_BARS` (4) gets no colour of its own: the crossing into the section after it starts on its first bar and runs across it. Brightness is planned in perceptual levels: `band_for` in `atmospheric.py` gives each section a band from its activity (loudness and onset density), narrow and high when calm, near black to full when active (only sections with activity ≥ `OFF_MIN_ACTIVITY` may drop under `VISIBLE_MIN` (2%), which reads as off), and pulse strengths place each beat's peak inside that band. The standalone `pattern_tester.py` remains available for manually experimenting with the patterns in `patterns.py`, but those patterns are not used during Spotify playback.

## License

This project is licensed under the [MIT License](LICENSE). Not that it really matters, because nobody's going to use this anyway.
