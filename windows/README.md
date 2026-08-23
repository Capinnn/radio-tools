# Windows packaging for radio-tools

This directory makes the radio stack installable and runnable on Windows. It
only touches packaging files — it does not modify `broadcast/`, `studio/` or
`liquidsoap/` source code.

## What is here

| File | Purpose |
|------|---------|
| `install.ps1` | One-shot, idempotent installer. Creates the repo and studio virtualenvs, installs `broadcast` in editable mode, installs studio deps, generates `liquidsoap/config/secrets.env` from the example, and prints the quick start. |
| `check-prereqs.ps1` | CI-ready check. Verifies Python 3.11+, `liquidsoap.exe`, and `icecast.exe` / `icecast2.exe`, then exits non-zero with a readable list of what is missing. `install.ps1` reuses this script for its final sanity check. |
| `README.md` | Windows quick-start guide (download links, install steps, firewall note, known limitations). |

## Quick start

1. Install the prerequisites (see below).
2. Clone or extract `radio-tools` to a path without spaces, e.g. `C:\radio-tools`.
3. Open PowerShell 5.1 or PowerShell 7 in that directory.
4. Run the installer:

   ```powershell
   .\windows\install.ps1
   ```

5. Start the studio console:

   ```powershell
   cd studio
   .venv\Scripts\python app.py --scan
   ```

   Open <http://127.0.0.1:5110>.

6. Generate the first playlist:

   ```powershell
   cd ..\..
   .venv\Scripts\python -m broadcast.playlistgen studio\music --rotation liquidsoap\config\rotation.json --hour (Get-Date -Format HH) --slot 1h --output liquidsoap\data\playlist.m3u
   ```

7. Start the engine. The cross-platform `radio` CLI lives in `liquidsoap\engine\`
and is maintained by a separate agent. After the installer finishes, follow
`liquidsoap\engine\README.md` for the exact `radio start` command on Windows.

8. Listen at <http://127.0.0.1:8000/radio.mp3>.

## Prerequisites

| Prerequisite | Why it is needed | Download |
|--------------|------------------|----------|
| Python 3.11+ | Broadcast toolkit (`broadcast`), playlist generator, studio backend | <https://www.python.org/downloads/> |
| Liquidsoap (official Windows build) | Audio streaming engine | <https://www.liquidsoap.info/doc-dev/install.html> |
| Icecast (official Windows build) | MP3 stream server | <https://icecast.org/download/> |

Installers for Liquidsoap and Icecast usually add themselves to `PATH`. If they
do not, the scripts also look under:

- `C:\Program Files\Liquidsoap\`
- `C:\Program Files\Icecast\`
- `C:\Program Files (x86)\Liquidsoap\`
- `C:\Program Files (x86)\Icecast\`

During Python setup, check **"Add python.exe to PATH"** (or the equivalent
option). The installer uses `py -3.11` first, then `py -3`, then `python`.

## Firewall

Icecast listens on port `8000` by default. The first time it runs, Windows may
show a firewall prompt. Allow it on **private networks** if you want other
devices on the same LAN to listen. Do not expose port 8000 to the public
internet without additional security.

## Known limitations

- **Live assist via PulseAudio** (`scripts/live.liq`) is Linux only. On Windows,
  use the station script (`station.liq`) and the Auto-DJ/queue workflow in the
  studio console.
- **WebAudio playback** in the studio works everywhere; the browser handles
  audio output and microphone mixing.
- Native live input sources require platform-specific Liquidsoap input modules
  that may not be included in the official Windows installer. Test with the
  `radio status` / `radio test` commands documented in `liquidsoap\engine\README.md`.

## Re-running the installer

`install.ps1` is idempotent. It skips virtualenv creation if one exists, skips
`pip install` upgrades that are already satisfied, and leaves an existing
`secrets.env` untouched. Run it again safely after pulling updates or moving the
repo.

## CI check

```powershell
.\windows\check-prereqs.ps1
$LASTEXITCODE
```

`$LASTEXITCODE` is `0` when Python, Liquidsoap, and Icecast are all found. Any
missing item is listed with a download link.
