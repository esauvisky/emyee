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

## Web app

`python main.py` serves the control page on http://0.0.0.0:8080/ (change with `--web HOST:PORT`); there is no terminal panel. The page shows:

- the live colour, song, current mode and what comes next (phrase, drop, silence);
- a whole-song overview and a scrolling timeline of phrases (shaded by energy), parts and drops, the quiet, flow, silence, drop lead-in and drop hit bars, Spotify's loudness envelope (Spotify gives no audio, so this stands in for a waveform), beats and bars, and a trace of what the lights actually did. Hover the timeline to see why each decision was made. Click the overview to jump to a part, click or drag the timeline to seek (clicking its top lane jumps to that phrase), or use the previous/next part and phrase buttons. Seeking moves Spotify's playback, which needs Spotify Premium and the `user-modify-playback-state` permission: the first start after this change asks you to log in to Spotify again;
- light selection with an identify blink, max brightness and sync offset;
- switches for each lighting rule, numeric settings for the rules with Reset and Re-analyse song, and the live log.

Every rule is described in [RULES.md](RULES.md).

## Customization

Playback uses the atmospheric beat-pulse implementation in `light_controller.py`. Edit `ATMOSPHERIC_DB` in `atmospheric.py`; every pulse is `(strength, attack, hold, release)`, with timing measured in fractions of one beat. Because each pulse owns its envelope, one profile can mix slow swells, instant flashes, held peaks, and soft releases. Parts detected in `sections.py` are cut into phrases counted from their first bar (`PHRASE_BARS_CALM` = 4 bars in calm parts, `PHRASE_BARS_LOUD` = 8 in loud ones); each phrase gets its own energy and colour, and "section" below means a phrase. Each section picks the profile whose pulse cycle divides its beat count most evenly, and the cycle restarts on the section's first beat so accents stay on the section's own grid. `sections.py` splits the track into the runs of bars that are most uniform in loudness, timbre and harmony (pitch-class content, `CHROMA_WEIGHT`; an exact change-point fit, `BOUNDARY_PENALTY` per part), and each boundary then snaps to the bar with the biggest bar-to-bar change within `SNAP_BARS` (1), so a part starts on the bar the sound jumps on. Bars whose loudest hit is `QUIET_BAR_DB` under the median loudest hit of the `QUIET_LOOKBACK_BARS` before them (breakdowns) hold one colour without pulsing, at a level set by how much quieter they are: just past the threshold they hold at the phrase's mid level, `QUIET_DARK_DB` deeper they go dark; a bare kick is not quiet. Bar loudness counts every segment sounding in the bar, so a chord struck just before the bar line fills it. A near-silent gap of at least `SILENCE_MIN_S`, `SILENCE_TRACK_DB` under the track's loud bars and `SILENCE_LOCAL_DB` under the bars just before it (the breath before a drop), falls fast to a dim level (`SILENCE_DIM_BRIGHTNESS`) until the next beat; only a gap of at least `SILENCE_DARK_MIN_S` goes on to 0%. Each drop moment is a lead-in bar and the hit bar after it: the bar before a drop part, or a break bar (`BREAK_DB` under the bars on both sides) and the bar after it. The web app picks the lead-in effect (fade out, blackout, colour burst then dark, riser) and the hit effect (strobe, flash, white flash); RULES.md lists them. A drop part is a part whose first `DROP_WINDOW_BARS` (4) sound far closer to the track's peak sound (the loudness and timbre of its loudest quarter of bars) than the bars before it: the distance has to at least halve (`DROP_MAX_RATIO`), shrink by `DROP_MIN_FALL` and end within `DROP_MAX_ARRIVAL`, all in `sections.py`. Over the last `HUE_FADE_BARS` (1) before a colour change the hue crosses linearly into the new colour, landing on the section's first beat, while the beats keep pulsing: each pulse command carries the colour the crossing has reached when it lands. A section shorter than `MIN_COLOR_SECTION_BARS` (4) gets no colour of its own: the crossing into the section after it starts on its first bar and runs across it. Brightness is planned in perceptual levels: `band_for` in `atmospheric.py` gives each section a band from its activity (loudness and onset density), narrow and dim when calm (about 15–40%, `CALM_TOP_LEVEL` and `CALM_BOTTOM_SHARE`), near black to full when active (only sections with activity ≥ `OFF_MIN_ACTIVITY` may drop under `VISIBLE_MIN` (2%), which reads as off), and pulse strengths place each beat's peak inside that band. The standalone `pattern_tester.py` remains available for manually experimenting with the patterns in `patterns.py`, but those patterns are not used during Spotify playback.

## License

This project is licensed under the [MIT License](LICENSE). Not that it really matters, because nobody's going to use this anyway.
