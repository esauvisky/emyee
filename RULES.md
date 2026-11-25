# Lighting rules

Every rule the controller applies while a track plays, in the order a track
goes through them. Constants are named in `code` and live in
`light_controller.py` unless another file is given. Rules marked with a
**toggle** can be switched off live from the web app (`main.py --web`) to
judge them by eye; the rest are structural and always run.

## 1. Structure

### Bar grid and downbeat — toggle `downbeat_fix` (`sections.py`)
Spotify's bar grid has two kinds of error:
- it can sit a beat or two off the real downbeat for a whole track: on many
  techno tracks new parts, drops and breakdowns all land on beat 2 of its
  bars;
- it slips mid-track: a bar of 1 or 2 beats moves every bar after it, though
  the beats themselves stay perfectly steady (Spicy's 1-beat bar at 145.9 s).

New sounds enter on the downbeat, so the beat of the bar that the music's
changes peak on is taken as the real beat 1.
- A change is a local peak (within 2 beats) of the distance between the mean
  sound (loudness, timbre, chords) of the beats after a beat and before it,
  over windows of `DOWNBEAT_SPANS` (4, 8 and 16 beats). Each peak counts by
  its height above the median.
- Two grids are scored: Spotify's bars as they are, and a steady grid of
  Spotify's usual beat count per bar laid over its beats from its first bar.
  The one whose best beat collects the larger share of the changes wins: the
  steady grid on most electronic tracks, Spotify's on some live recordings
  whose odd bars are real.
- In 4-beat bars a strong **backbeat** settles it first: the claps or snare
  on beats 2 and 4 make every other beat of the steady grid hit louder. Per
  bar, the loudest hits of beats 1 and 3 minus those of 2 and 4; when the mean
  is at least `BACKBEAT_MIN_T` (6) standard errors from zero, the loud pair is
  taken as the real beats 2 and 4, beat 1 is one of the other two, and the
  music's changes pick which. The steady grid is used. Thunderstorm's
  backbeat (t = -19) puts beat 1 on steady beat 3, where the changes alone
  only leaned toward beat 2 (30%). A weaker alternation proves nothing: on
  some mixes the kick beats are the louder ones.
- Otherwise beat 1 moves later only when one beat collects at least
  `DOWNBEAT_MIN_SHARE` (35%) of the change and `DOWNBEAT_MIN_LEAD` (1.5×)
  what the grid's beat 1 collects. If no beat stands out, it stays.
- Everything built on bars follows: parts, phrases, colour changes, drop
  effects, quiet and flow bars. Switching the toggle re-runs the song's
  analysis; off keeps Spotify's bars untouched.

### Parts (`sections.py`)
The track is split into **parts**: runs of bars that are most uniform in
loudness, timbre and harmony (pitch-class content). The split is an exact
change-point fit over bars.
- Each boundary costs `BOUNDARY_PENALTY` (11.0), so a boundary must cut that
  much error out of the fit: two 8-bar parts split once they sit about 5 dB
  apart with the same timbre and chords.
- Each boundary then snaps to the bar with the biggest bar-to-bar change
  within `SNAP_BARS` (1) of it, so a part starts on the bar the sound jumps
  on. Parts don't follow a fixed 4-bar grid: Spotify's bars drift by a beat
  or two mid-track, and a grid counted in them pulled boundaries a bar off.
- Parts are at least `MIN_PART_BARS` (2) long.
- Weights: loudness in units of `LOUDNESS_UNIT_DB` (3 dB), timbre scaled by
  `TIMBRE_WEIGHT` (2.0), chords by `CHROMA_WEIGHT` (3.0). Silence is clamped
  at `LOUDNESS_FLOOR_DB` (-40 dB).

### Drops (`sections.py`)
A part is a **drop** when its first `DROP_WINDOW_BARS` (4) sound much closer
to the track's peak sound than the 4 bars before it. The peak sound is the
mean loudness and timbre of the loudest `DROP_PEAK_SHARE` (25%) of bars. All
of these must hold:
- the distance to the peak sound at least halves (`DROP_MAX_RATIO`, 0.5);
- it shrinks by at least `DROP_MIN_FALL` (1.25, about 4 dB);
- it ends within `DROP_MAX_ARRIVAL` (3.0, about 9 dB);
- the part is at least `DROP_MIN_BARS` (4) long.
The first part is never a drop.

### Energy
Each part and each phrase gets an energy from 0 to 1: 60% its loudness rank
in the track, 40% its onset-density rank (segments per second). Only spans
that are both loud and busy reach 1. Energy maps to five levels (very calm,
calm, medium, energetic, peak), which choose the pulse profile.

### Phrases
Parts are cut into **phrases** counted from their first bar:
`PHRASE_BARS_LOUD` (8) bars in parts with energy ≥ `PHRASE_LOUD_ENERGY`
(0.5), `PHRASE_BARS_CALM` (4) in calmer ones. A leftover shorter than
`PHRASE_MIN_BARS` (2) joins its neighbour. The phrase length doubles until
a phrase lasts at least `PHRASE_MIN_S` (7 s), so fast or 3/4 songs with bars
of about a second don't change colour every few seconds: over 99 tracks the
median is 4.6 colour changes a minute, at most 8. A part that isn't a whole number
of phrases has a lead-in or a tail: its phrases count back from its last bar
instead of its first when the texture changes (loudness and timbre, bar to
bar; chords change on almost every bar in a loop) at the phrase starts that
gives are at least `PHRASE_GRID_MARGIN` (1.25×) those counting from its first
bar. Then the lead-in sits at the start, like a 4-bar build before three
8-bar phrases. A phrase is the unit for
energy, pulse profile and colour.

### Song character — toggle `calm_style` (`sections.py`)
Before this rule every genre swung the same: measured on 99 tracks, the light
moved about 0.28 of its range on every beat in ambient, jazz and bossa nova as
much as in house, because energy is ranked inside each song. So each song gets
a **drive** score, 0 calm to 1 driving, from three things Spotify measures for
the whole track: its loudness, the median beat confidence and the median
timbre brightness (a logistic fit, `DRIVE_*`, on 67 tracks labelled by ear,
90% right when each is left out of the fit).
- Under `CALM_DRIVE` (0.5) the song plays **calm style**: each bar breathes
  once on its first beat instead of pulsing on every beat, its strength set
  by the bar's loudness rank in the track; phrase energies are scaled by
  `CALM_ENERGY_SCALE` (0.6), so the bands stay gentler; colour crossings take
  `CALM_HUE_FADE_BARS` (2); there are no drop effects. On the tracks
  labelled calm the swing per beat halves (0.27 → 0.13) and the commands sent
  halve; driving tracks are unchanged.
- The web app's Song character picker forces a song calm or driving; the
  choice is kept per track in `track_overrides.json` and re-runs the song's
  analysis. Busy bebop (Seatbelts) is what the score gets wrong.

## 2. Brightness (`atmospheric.py`)

### Perceptual levels
Brightness is planned in perceptual levels (0 to 1) and squared on the way
out (`PERCEPTUAL_GAMMA`, 2.0), so equal steps look equal.

### Brightness band
Each phrase swings inside a band set by its energy (`band_for`):

| Energy | Band |
|---|---|
| 0 | 15–40% |
| 0.25 | 11–52% |
| 0.5 | 6–66% |
| 0.75 | 2–82% |
| 1 | 0–100% |

Set by `CALM_TOP_LEVEL` and `CALM_BOTTOM_SHARE`. Below energy
`OFF_MIN_ACTIVITY` (0.9) the band never drops under `VISIBLE_MIN` (2%),
which reads as off.

### Track fade — toggle `track_fade`
During Spotify's reported fade in and fade out, all brightness is scaled
down linearly, to no less than 5%.

### Max brightness
A ceiling on everything sent, from the CLI arrow keys or the web slider.

## 3. Beat modes

Every beat is handled by exactly one mode, checked in this order.

### Silence dimming — toggle `silence`
A run of segments at least `SILENCE_MIN_S` (0.25 s) long whose loudest
moment is `SILENCE_TRACK_DB` (24 dB) under the track's loud bars (90th
percentile) **and** `SILENCE_LOCAL_DB` (15 dB) under the median loudest hit
of the `SILENCE_LOOKBACK_BARS` (4) before it is a near-silent gap: the breath
before a drop. The first bar has no bars before it, so it is judged against
its own loudest hit. The local test keeps a quiet intro from dimming.

At the gap's start the lights fall to `SILENCE_DIM_BRIGHTNESS` (4%), or to
the phrase's resting level if that is lower, over `SILENCE_FADE_S` (0.15 s),
capped at a third of the gap. The colour is kept. Only a gap at least
`SILENCE_DARK_MIN_S` (1 s) long then carries on to 0% over
`SILENCE_DARK_FADE_S` (0.4 s). Beats inside the gap are skipped, and the
first beat after it relights in whatever mode it is in. A pulse whose release
would land in the gap releases to the gap's dim level (0% for a dark gap).

### Drops: lead-in and hit — chosen in the web app's Drops section
A **drop moment** is a lead-in bar and the hit bar after it. Two things make
one:
- a **break**: a bar whose loudness is at least `BREAK_DB` (6 dB) under both
  the median of the `BREAK_LOOKBACK_BARS` (4) before it and the bar after it,
  with the bar after back within `BREAK_RETURN_DB` (3 dB) of the bars before.
  The music drains away, then slams back (Spicy's bar 58 fades from -12 to
  -32 dB before bar 59 hits). The break is the lead-in, the bar after it the
  hit.
- a **drop part** (above): the bar before it is the lead-in, its first bar the
  hit. When a drop part starts on a break, the break wins.

A hit on a quiet or flow bar is no hit, and a song in calm style plays no
drop effects. A bar that is already a hit isn't
also a lead-in (back-to-back drops). The lead-in and hit effects own their
bars over the flow and quiet modes; a silence still dims the beats inside
it.

**Lead-in effects** (`DROP_LEADS`, default `fade_out`):
- `none`: the bar plays by the normal rules.
- `fade_out`: brightness fades from where it is to 0% across the bar, so the
  hit comes out of the dark.
- `blackout`: cut to 0% on the bar's first beat (`DROP_CUT_S`, 50 ms), dark
  until the hit.
- `burst_then_off`: a new palette colour every eighth note at the phrase's
  band top, then dark for the last beat.
- `riser`: each beat steps brightness up from the band's bottom to full and
  the colour one palette step round the wheel.

**Hit effects** (`DROP_HITS`, default `strobe`):
- `none`: the bar plays by the normal rules.
- `strobe`: a new palette colour every eighth note across the bar, held at
  the phrase's band top.
- `flash`: the first beat jumps to full in a new colour and decays over the
  beat; pulses resume after it.
- `white_flash`: the first beat jumps to full white and decays back into the
  colour it interrupted.

**Ideas not built yet**, for when these feel limited:
- a build over the last 4–8 bars, not just one: brightness or the pulse
  swing ramping up, or the colour turning faster each bar;
- accelerating pulses (quarter notes, then eighths, then sixteenths), the
  light version of a snare roll. The lamps merge commands closer than
  `MIN_COMMAND_GAP_S` (200 ms), so sixteenths above about 75 BPM can't be
  sent; eighths are the fastest that work at 125 BPM;
- freeze: the colour and level hold still through the lead-in, then the hit
  breaks it;
- one bulb at a time: lamps lit in turn round the room during the lead-in,
  all together on the hit (needs the lamps' positions);
- a hit that holds full brightness for a whole bar before the normal swing
  returns;
- white-out: saturation draining to white through the lead-in, colour
  snapping back on the hit.

### Flow — toggle `flow`
A bar is **flow** when it has no transients to pulse on (a vocal over a
pad). All three must hold:
- mean segment onset rise under `FLOW_MAX_ONSET_DB` (8 dB);
- mean beat confidence under `FLOW_MAX_BEAT_CONFIDENCE` (0.5);
- bar loudness at least `FLOW_QUIET_DB` (6 dB) under the track's loud bars
  (90th percentile). A brickwalled drop has soft onsets and loses the beat
  tracker, but it is the loudest part of the song.
Runs shorter than `FLOW_MIN_BARS` (2) don't flip the mode either way. A flow
bar sends one command that settles brightness over the whole bar at a level
set by the bar's loudness rank inside the band.

### Flow hue drift — toggle `flow_hue_drift`
Each flow bar also drifts the hue to the nearest palette colour
at least `FLOW_HUE_STEP_MIN` (20°) up the wheel, so long flow
runs move one way round the wheel. Skipped while a phrase colour change is
crossing the bar.

### Quiet hold — toggle `quiet`
A bar whose loudest hit is `QUIET_BAR_DB` (5 dB) under the median loudest
hit of the `QUIET_LOOKBACK_BARS` (8) before it is a breakdown. It holds one
colour without pulsing. Its level depends on how deep the run is: just past
the threshold it holds at the phrase's mid level, `QUIET_DARK_DB` (15 dB)
deeper it holds at 0%, linear in between. Judged on the loudest hit, so a
bare kick with silence between hits is not quiet. Bar loudness counts every
segment sounding during the bar, so a chord struck just before the bar line
fills it.

### Pulse
Everything else pulses on every beat.
- **Profile:** `ATMOSPHERIC_DB` in `atmospheric.py` has profiles per energy
  level. Each pulse is `(strength, attack, hold, release)`, timings as
  fractions of a beat. The profile whose cycle divides the phrase's beat
  count most evenly wins; the cycle restarts on the phrase's first beat.
- **Peak:** strength places the beat's peak inside the band; the release
  falls to the band's bottom.
- **Loudness pulse — toggle `loudness_pulse`:** strength is scaled by the
  segment's loudness within the track (50% to 100%), so softer hits peak
  lower.
- **Slow tempo ramps — toggle `slow_tempo_ramps`:** beats longer than 0.45 s
  floor attack and release at a share of the beat that grows to half a beat
  at 60 BPM, so slow songs don't flash between levels.
- **Calm easing:** energy levels under "energetic" ease into every beat
  (attack ≥ 20%, hold ≤ 10%) and decay all the way to the next one.
- **Device limits:** attack is at least `MIN_ATTACK_S` (30 ms); the release
  goes out at least `MIN_COMMAND_GAP_S` (200 ms) after the attack and before
  the next beat, because closer commands get merged or dropped by the lamp.

## 4. Colour

### Phrase colour changes — toggle `colour_changes`
Every phrase crosses into a new colour. Which colour comes from colour
identity (below); with that off, a random palette colour 40–`MAX_HUE_STEP`
(90°) from the current one, so it reads as a change without sweeping through
unrelated colours. The hue crosses linearly over the last `HUE_FADE_BARS` (1) bar
before the phrase, landing on its first beat, while beats keep pulsing: each
command carries the colour the crossing has reached when it lands.

### Colour identity — toggle `colour_identity`
Parts that sound alike share a home colour, so a returning chorus or drop
comes back in its colour. Each part's mean sound (loudness, timbre and
chords, as the part split measures them) joins the first earlier group within
`IDENTITY_DISTANCE` (2.0) of it, or starts a new group. Over 99 tracks this
gives a median of 8 colours a song, with 43% of parts returning to an earlier
group; adjacent parts sit 3.05 apart (median), so they rarely merge.
- The first group's colour is the palette colour nearest the song's key on
  the circle of fifths round the wheel (C at 0°, G at 30°, D at 60°…), so a
  song always opens in the same colour. Each new group takes the palette
  colour farthest from the groups before it.
- Each phrase takes its part's home colour, unless that is the colour already
  showing (a part of the same group just before, or the phrase before in a
  long part): then the palette colour nearest `IDENTITY_NEIGHBOUR_STEP` (40°)
  up from home. Every phrase still changes colour.

### Short phrases
A phrase shorter than `MIN_COLOR_SECTION_BARS` (4) gets no colour of its
own: the crossing into the next full phrase starts on its first bar and runs
across it.

## 5. Timing
- Commands are scheduled ahead and sent early by each lamp's measured
  latency plus `--sync-offset`.
- A beat less than 50 ms away when it is scheduled is skipped (catch-up
  after a progress jump).
- Song time off from the running clock by more than `SEEK_THRESHOLD`
  (1.5 s, `utils.py`) is a seek: everything scheduled is cancelled and the
  phrase is re-entered.
