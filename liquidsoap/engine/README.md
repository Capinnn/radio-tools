# `radio` — cross-platform engine manager

`engine/` is the pure-stdlib Python lifecycle manager for the Liquidsoap +
Icecast chain. It replaces the bash scripts in `../bin/` and runs the same way
on Linux and Windows. It is installed as the `radio` console script by
`pip install -e .` at the repo root (`radio = engine.__main__:main`).

Only the standard library is used — `subprocess`, `os`, `sys`, `time`,
`urllib`, `signal`, `pathlib`, `json`, `argparse`, `shutil`, `fnmatch`, `re`.

## Command reference

```
radio start [--live] [--force-root] [--dry-run]
    Start Icecast then Liquidsoap. Default mode is station rotation;
    --live selects the live-assist script (Linux only — it needs
    PulseAudio/JACK). --dry-run validates without starting anything.

radio stop [--force-root] [--dry-run]
    Stop Liquidsoap first, then Icecast. PID files and the rendered runtime
    config are removed; logs are retained. --dry-run reports what would be
    stopped and signals nothing.

radio status
    Report each component UP/DOWN with PID, plus Icecast listener count,
    server type, now-playing title, and audio info.

radio restart [--live] [--force-root]
    Stop the chain, then start it again.

radio smoke [--duration N] [--keep]
    End-to-end smoke test: start, capture N seconds of MP3 audio (default 20),
    verify MP3 sync bytes and that Icecast reports audio/mpeg with >=1
    listener, then stop.

radio gen-playlist [OPTIONS]
    Generate a playlist using broadcast/playlistgen. Flags mirror the legacy
    gen-playlist.sh: --source, --library, --rotation, --output, --trigger,
    --slot, --daypart, --hour, --seed, --loop, --dry-run.

radio bin-paths [--verbose]
    Print the resolved icecast and liquidsoap paths. --verbose lists every
    candidate path tried when one is not found.

radio paths [--show]
    Print the platform, both binaries, the Icecast install root, the
    web/admin directories the next start will use, and the config/log paths —
    one "key: value" per line for scripts to parse. --show additionally lists
    the candidates tried for anything missing.
```

### `radio start --dry-run`

Validates everything a real start needs and then stops:

- resolves `icecast` and `liquidsoap` (failing with the full list of paths
  tried),
- checks the Liquidsoap script for the selected mode exists,
- loads and validates the secrets (length, charset, hostname, port),
- renders the runtime `icecast.xml` **in memory**, including the platform
  webroot/adminroot rewrite.

No process is spawned, no file is written, and the runtime config is not left
behind. It prints `DRY RUN: would start icecast at ... liquidsoap ...` plus the
resolved web/admin roots and exits 0.

```console
$ radio start --dry-run
DRY RUN: would start icecast at /usr/bin/icecast2 liquidsoap /usr/bin/liquidsoap
  mode:       station (.../liquidsoap/scripts/station.liq)
  config:     .../liquidsoap/logs/icecast.runtime.xml (rendered, 1654 bytes)
  webroot:    /usr/share/icecast2/web
  adminroot:  /usr/share/icecast2/admin
  stream:     http://127.0.0.1:8000/radio.mp3
```

## Binary resolution

`radio` looks for the two binaries in a fixed order and takes the first path
that exists. `radio paths --show` (or `bin-paths --verbose`) prints the whole
list when a lookup fails, so a missing install is easy to diagnose.

**POSIX** — unchanged from the original behaviour:

1. `ICECAST_BIN` / `LIQUIDSOAP_BIN` (used as-is, never probed)
2. every `PATH` entry — `icecast2` first, then `icecast`
3. `/usr/bin`, then `/usr/local/bin`

**Windows:**

1. `ICECAST_BIN` / `LIQUIDSOAP_BIN` (used as-is, never probed)
2. every `PATH` entry (`icecast.exe`, `icecast2.exe`, `liquidsoap.exe`)
3. `%ProgramFiles%` and `%ProgramFiles(x86)%` — any subdirectory matching
   `Icecast*` / `icecast*` / `Liquidsoap*` / `liquidsoap*`, checking both
   `<dir>\bin\<exe>` and `<dir>\<exe>`. This covers the install-name variants
   in the wild: `Icecast2 2.4.4`, `Icecast 2.4.4`, `Icecast2 Win32`, the
   icecast.org builds, and versioned Liquidsoap directories.
4. `%LOCALAPPDATA%\Programs` (and plain `%LOCALAPPDATA%`) for Liquidsoap
5. fixed fallbacks — `C:\Program Files\Liquidsoap\liquidsoap.exe`,
   `C:\Program Files\Icecast2\bin\icecast.exe`, and their `(x86)` twins

## Icecast web/admin roots

`config/icecast.xml` ships the Debian paths (`/usr/share/icecast2/web` and
`/admin`). Icecast refuses to start when those directories do not exist, which
is every Windows box. `radio start` therefore rewrites the two tags in the
**rendered** runtime copy — the checked-in template is never modified:

1. `ICECAST_WEBROOT` / `ICECAST_ADMINROOT` win if set (either platform).
2. Otherwise, on Windows the install root is derived from the resolved binary
   (`C:\Program Files\Icecast2 2.4.4\bin\icecast.exe` →
   `C:\Program Files\Icecast2 2.4.4`) and searched for
   `share\icecast\web` and `share\icecast\admin` — the official Windows build
   layout — falling back to `<root>\web` and `<root>\admin`.
3. If neither exists, the template values are kept and
   `Icecast webroot not found; using template paths` is printed to stderr.

Check what a start would use without starting anything:

```powershell
radio paths --show
```

## Windows validation

After `windows\install.ps1`, run the one-shot validator from the repo root:

```powershell
.\windows\validate-windows.ps1
```

It prints PASS/FAIL per check and exits non-zero if any fail:

| Check | What it means when it fails |
|-------|-----------------------------|
| python launcher 3.11+ | Install Python 3.11 or newer and re-open the shell. |
| repo virtualenv | Run `.\windows\install.ps1`. |
| broadcast and engine installed | Re-run the installer; `pip install -e .` did not complete. |
| liquidsoap.exe resolvable | Install Liquidsoap or set `LIQUIDSOAP_BIN`. |
| icecast.exe resolvable | Install Icecast or set `ICECAST_BIN`. |
| icecast webroot / adminroot | Set `ICECAST_WEBROOT` / `ICECAST_ADMINROOT` to the `web` and `admin` directories of your Icecast install. |
| `radio start --dry-run` | Read the printed error — usually a missing or too-short `ICECAST_SOURCE_PASSWORD` in `liquidsoap\config\secrets.env`. |

The validator starts nothing: the last step is the dry run.

Live assist (`radio start --live`) stays Linux-only — `scripts/live.liq` needs
PulseAudio/JACK. On Windows use station mode plus the studio console queue.

## Tests

```bash
../../studio/.venv/bin/python -m pytest ../tests -q
```

`tests/test_engine.py` covers the lifecycle; `tests/test_engine_paths.py`
covers the platform path handling. The Windows helpers are pure functions
(`list_bin_candidates`, `resolve_bin`, `find_icecast_share_dirs`,
`icecast_install_root`, `icecast_paths_for`) that take an injected environment
dict, directory lister, and `exists` probe, and parse paths with the target
platform's flavour — so the Windows behaviour is fully tested from Linux.
