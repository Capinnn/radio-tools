# radio-tools

Personal radio station stack. This repository is the control plane for a
continuous home-broadcast MP3 station: a scheduling/broadcast toolkit, a
browser-based studio console, and the streaming engine that puts the audio on
the air.

## Components

| Component  | What it does                                              | Get started                      |
|------------|-----------------------------------------------------------|----------------------------------|
| `broadcast/` | CLI toolkit: `playlistgen`, `schedule`, `trackcheck`, `logbook` plus utilities. Builds rotation playlists and manages the music library. | [`broadcast/formats.md`](broadcast/formats.md) |
| `studio/`   | Flask single-page broadcast console: library, queue, rotation-driven Auto-DJ, crossfades, talk-over, scheduler. | [`studio/README.md`](studio/README.md) |
| `liquidsoap/` | Broadcast engine: runs a continuous stream from the generated playlist into Icecast, with rotation and live-assist. | [`liquidsoap/README.md`](liquidsoap/README.md) |

The `broadcast/` package is installed as the `broadcast` distribution and
provides the `playlistgen`, `schedule`, `trackcheck`, `logbook`,
`broadcast-clock`, `intro-outro`, and `countdown` commands.

## How the pieces fit together

```
 music library ──► playlistgen rotation ──► liquidsoap ──► icecast ──► listeners
        ▲                │                                            │
        │                │  writes rotation.json                      │
        │                ▼                                            │
   studio (control surface) ── queue / Auto-DJ / live-assist           │
        │                                                             │
        └──── schedule ──► broadcast clock ──────────────────────────┘
```

`playlistgen` turns the music library into ordered playlists under a
rotation config. `liquidsoap` reads the latest playlist, applies ReplayGain
and crossfade, and streams `/radio.mp3` to Icecast. `studio` runs as the
operator's control surface on top of the same data files.

## Install

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

The `studio` subproject keeps its own environment and requirements; see
[`studio/README.md`](studio/README.md).

## Test

Each component has its own test suite.

```bash
# broadcast toolkit (root tests/)
.venv/bin/python -m pytest -q

# studio console
cd studio && .venv/bin/python -m pytest tests -q

# liquidsoap chain (uses the repo virtualenv)
cd liquidsoap && ../.venv/bin/python -m pytest tests -q
```

## Shared JSON contract

`broadcast/formats.md` is the shared contract between the `broadcast` toolkit
and the `studio` console. Both read and write the same files, so the two
packages stay in sync without a code dependency. Changes to a rotation file,
schedule, or playlist sidecar must be reflected there.
