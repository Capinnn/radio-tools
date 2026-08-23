# radio-tools

A personal radio station stack in one repo: a CLI toolkit for building playlists and managing the library, a browser-based studio console for hands-on control, and a streaming engine that puts the audio on the air as a continuous MP3 stream.

## The stack

| Layer | What it does | Port | Platforms |
|-------|--------------|------|-----------|
| `broadcast/` | CLI toolkit: `playlistgen`, `schedule`, `trackcheck`, `logbook`, and utilities. Builds rotation playlists and manages the music library. | n/a | Linux, Windows |
| `studio/` | Flask browser console: library, queue, rotation-driven Auto-DJ, crossfades, talk-over, scheduler. | 5110 | Linux, Windows |
| `liquidsoap/` | Streaming engine: Liquidsoap reads the playlist, applies ReplayGain and crossfade, and sends `/radio.mp3` to Icecast. | 8000 | Linux full; Windows station stream full via the `radio` CLI; live-assist is Linux only |

`playlistgen` turns the music library into ordered playlists under a rotation config. Liquidsoap reads the latest playlist and streams it to Icecast. `studio` runs as the operator's control surface on top of the same data files. The shared JSON contract is in [`broadcast/formats.md`](broadcast/formats.md).

## Quick start on Linux

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Start the studio console:

```bash
cd studio
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py --scan
```

Then open <http://127.0.0.1:5110>.

Start the engine:

```bash
cd liquidsoap
cp config/secrets.env.example config/secrets.env
chmod 600 config/secrets.env
# edit config/secrets.env: set ICECAST_SOURCE_PASSWORD to 12+ characters
radio gen-playlist --source /absolute/path/to/music
radio start
```

Verify with `radio status` and listen at <http://127.0.0.1:8000/radio.mp3>.

Run a quick end-to-end check without leaving the chain running:

```bash
radio smoke
```

## Quick start on Windows

Use the Windows installer and validator in `windows/`:

```powershell
.\windows\install.ps1
.\windows\validate-windows.ps1
```

See [`windows/README.md`](windows/README.md) for the full walkthrough, download links for Python 3.11+, Liquidsoap, and Icecast, and the firewall note. Install the repo to a path without spaces, for example `C:\radio-tools`.

After install:

```powershell
cd studio
.venv\Scripts\python app.py --scan
```

Then open <http://127.0.0.1:5110>. From the repo root, generate a playlist and start the stream with `radio gen-playlist --source C:\path\to\music` and `radio start`. Listen at <http://127.0.0.1:8000/radio.mp3>.

## Engines vs scripts

The `radio` CLI in `liquidsoap/engine/` is the cross-platform lifecycle manager for the Liquidsoap + Icecast chain. It is the default engine on both Linux and Windows.

The legacy bash scripts in `liquidsoap/bin/` still work on Linux and remain documented under a "legacy" section in [`liquidsoap/README.md`](liquidsoap/README.md). New setup should use `radio`.

## Development

Each layer has its own test suite.

```bash
# broadcast toolkit (root tests/)
.venv/bin/python -m pytest -q

# studio console
cd studio && .venv/bin/python -m pytest tests -q

# liquidsoap engine chain
cd liquidsoap && ../.venv/bin/python -m pytest tests -q
```

## Deploying the stream

Icecast serves the stream on `/radio.mp3` at port 8000. If you want other devices on the LAN to listen, open port 8000 in your firewall. The repo defaults to localhost-only operation where possible, so plan the firewall change deliberately rather than leaving it open by default.
