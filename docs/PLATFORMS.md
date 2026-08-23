# Platform support

The radio stack has three layers. This matrix shows what works on Linux and what
works on Windows today.

| Layer | Linux support | Windows support | Notes |
|-------|-------------|-----------------|-------|
| `broadcast/` CLI toolkit | Full | Full | Pure Python 3.11+ with `click` and `mutagen`. Installed via `pip install -e .` on both platforms. |
| `studio/` Flask console | Full | Full | Browser console; WebAudio playback works everywhere. Runs from its own `.venv` on both platforms. |
| `liquidsoap/` engine | Full | Partial | `station.liq` playlist stream works on both. `scripts/live.liq` PulseAudio live-assist is Linux only. Windows uses `liquidsoap\engine\` `radio` CLI. |

## Windows packaging

See [`windows/README.md`](../windows/README.md) for the Windows quick start,
prerequisite links, and firewall notes.

## Linux packaging

See the root [`README.md`](../README.md) and the per-layer readmes in
`broadcast/`, `studio/`, and `liquidsoap/`.

## Notes

- The same JSON contract in `broadcast/formats.md` is used on both platforms.
- The playlist generator writes identical `playlist.m3u` / `playlist.json`
  sidecars on both platforms.
- Icecast defaults to port `8000` on both platforms; remember to allow it in the
  Windows Defender firewall for private networks.
- On Linux, live-assist is provided by `liquidsoap/scripts/live.liq`
  (PulseAudio/JACK). On Windows, use the station script and the studio console
  queue/Auto-DJ for live-style programming.
