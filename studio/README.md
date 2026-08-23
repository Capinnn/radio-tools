# RADIO STUDIO

A single-page broadcast console: library, queue, rotation-driven Auto-DJ,
crossfades, talk-over and a scheduler. Flask + vanilla JS + Web Audio, state in
flat JSON files, no database and no external assets.

## Run it

### Linux / macOS

```bash
cd studio
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py --scan
```

### Windows

```cmd
cd studio
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py --scan
```

Or double-click `start.bat` (it activates the virtualenv and runs `python app.py`).

Then open <http://127.0.0.1:5110>.

`--scan` reads the music folder before starting; you can also press **Scan** in
the app at any time. `--port` and `--host` override the defaults (5110 and
127.0.0.1). Use `--restart` to stop any existing listener on the port and start
a fresh instance.

## First run

1. **Settings → Music folder** — an absolute path to your collection, e.g.
   `C:\Users\You\Music` on Windows or `/home/you/Music` on Linux. Subfolders are included. Leave it blank to use
   `studio/music/`.
2. **Save & scan** — reads tags from every `.mp3`, `.flac`, `.m4a`, `.wav` and
   `.ogg` it finds.
3. Click tracks to queue them, then press **Play**. You can also drag audio
   files from your file manager onto the window to import them.

## The console

| Area | What it does |
| --- | --- |
| **Library** | Search across title/artist/album/genre, filter by genre, artist, rotation category and BPM range, sort seven ways. Click a row to queue it, double-click to play it now, hover the pencil to edit its tags. |
| **Queue** | Drag rows to reorder, click a row to play it immediately, the × removes it. **Save queue** stores it as a named playlist; playlists load (replace) or **Add** (append). |
| **Auto-DJ** | Keeps the queue topped up using the rotation rules. Auto-filled entries are marked `auto`. **Fill now** runs one pass regardless. |
| **Transport** | Big play/pause, prev/next, draggable seek bar with elapsed and remaining. |
| **Fade in / out** | 2s ramps by default (Settings changes the length) so you can talk over the top or out of a track. |
| **Talk over** | Ducks the music to 20% (configurable) while engaged. Hold `T`, or click the button to latch it. |
| **Crossfade** | 0–12s, equal-power. Automatically shortened for tracks too brief to absorb it. |
| **Output meter** | Live L/R RMS with peak hold, −40 dB to 0 dB. |
| **Mic** | Optional Web Audio mic mix for monitoring on this machine. It sits after the ducking stage, so talk-over never ducks the mic. Denied permission leaves the level slider disabled and says so. |

## Rotation

Each category carries three rules:

- **Spins/hour** — the target for a normal hour.
- **Artist gap** — minutes before the same artist may repeat.
- **Daypart weights** — multipliers for Overnight, Morning Drive, Midday,
  Afternoon Drive and Evening. Power at `1.4` in drive time and `0.6` overnight
  means exactly that.

Auto-DJ scores each category by `daypart weight × unmet share of its hourly
target`, picks one, then takes the coldest eligible track from it. If the rules
are tighter than the library can satisfy it relaxes them one at a time (repeat
window first, then artist gap) rather than stalling, and says which rule it had
to drop. A library with no categories assigned plays from everything; a library
that *is* categorised never plays uncategorised tracks by accident.

Assign a track to a category in the track editor (pencil icon in the library).

## Schedule

`at HH:MM play playlist X` or `start category Y`, once or daily, replacing or
appending to the queue. Events fire while the page is open — this is a browser
console, not a background broadcast server.

## Keyboard

| Key | Action |
| --- | --- |
| `Space` | Play / pause |
| `F` | Fade out |
| `Shift + F` | Fade in |
| `T` | Talk over (hold, or tap the button to latch) |
| `← →` | Seek 5 seconds |
| `Shift + ← →` | Previous / next track |
| `↑ ↓` | Master volume |
| `A` | Toggle Auto-DJ |
| `/` | Focus search |
| `Esc` | Close dialog / clear search |
| `?` | Shortcut list |

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

`tests/test_core.py` covers scanning, tag round-trips, search, playlists and the
rotation engine; `tests/test_api.py` covers the HTTP endpoints including Range
streaming and uploads.

`tests/make_test_tracks.py` writes five short sine-wave WAVs (distinct pitches
and durations) into `music/` so you can exercise the console without touching
real audio:

```bash
.venv/bin/python tests/make_test_tracks.py            # into studio/music
.venv/bin/python tests/make_test_tracks.py /tmp/demo  # somewhere else
```

## Files

```
app.py          Flask routes — thin HTTP layer
core.py         store, scanning, search, rotation engine (no Flask, unit-tested)
data/*.json     library, queue, playlists, rotation, schedule, history, config
music/          default music folder
static/, templates/
```

Editing a track writes the tags back into the file itself (ID3 for mp3 and wav,
Vorbis comments for flac and ogg, iTunes atoms for m4a) as well as into the
index, so a rescan does not undo your edits. If a file cannot hold a tag the
edit still persists in the index and the app tells you.
