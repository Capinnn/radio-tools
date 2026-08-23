# Platform support

The radio stack has three layers. This matrix shows what works on Linux and what
works on Windows today.

| Layer | Linux support | Windows support | Notes |
|-------|-------------|-----------------|-------|
| `broadcast/` CLI toolkit | Full | Full | Pure Python 3.11+ with `click` and `mutagen`. Installed via `pip install -e .` on both platforms. |
| `studio/` Flask console | Full | Full | Browser console; WebAudio playback works everywhere. Runs from its own `.venv` on both platforms. |
| `liquidsoap/` engine | Full | Full (station stream) | The `radio` CLI (`liquidsoap/engine/`) runs the `station.liq` playlist stream on both, resolving `icecast.exe` / `liquidsoap.exe` and the Icecast web/admin roots automatically. `scripts/live.liq` PulseAudio live-assist remains Linux only. |

## Windows packaging

See [`windows/README.md`](../windows/README.md) for the Windows quick start,
prerequisite links, and firewall notes.

After installing, validate the whole stack in one shot from the repo root:

```powershell
.\windows\validate-windows.ps1
```

It prints PASS/FAIL for Python, the virtualenv, the installed packages, both
binaries, the Icecast `web`/`admin` directories, and a `radio start --dry-run`
that renders the runtime config without starting anything. It exits non-zero if
any check fails. `radio paths --show` prints the same resolved paths on their
own. See [`liquidsoap/engine/README.md`](../liquidsoap/engine/README.md) for
the search order and the environment overrides (`ICECAST_BIN`,
`LIQUIDSOAP_BIN`, `ICECAST_WEBROOT`, `ICECAST_ADMINROOT`).

## Linux packaging

See the root [`README.md`](../README.md) and the per-layer readmes in
`broadcast/`, `studio/`, and `liquidsoap/`.

## Notes

- The same JSON contract in `broadcast/formats.md` is used on both platforms.
- The playlist generator writes identical `playlist.m3u` / `playlist.json`
  sidecars on both platforms.
- Icecast defaults to port `8000` on both platforms; remember to allow it in the
  Windows Defender firewall for private networks.
- The checked-in `liquidsoap/config/icecast.xml` carries Debian's
  `/usr/share/icecast2/{web,admin}` paths. On Windows, `radio start` rewrites
  those two tags in the rendered runtime copy to the `share\icecast\web` and
  `share\icecast\admin` directories of the detected Icecast install; the
  template on disk is never modified.
- On Linux, live-assist is provided by `liquidsoap/scripts/live.liq`
  (PulseAudio/JACK). On Windows, use the station script and the studio console
  queue/Auto-DJ for live-style programming.
