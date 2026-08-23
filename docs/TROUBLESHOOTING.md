# Troubleshooting

This is a running list of the problems that come up again when installing or
running the stack.

## Icecast port 8000 is already in use

If `radio start` fails with an error about binding to port 8000, something else
is listening there.

Check on Linux:

```bash
ss -tlnp | grep 8000
```

Check on Windows (PowerShell):

```powershell
Get-NetTCPConnection -LocalPort 8000 | Select-Object LocalPort, OwningProcess
```

To move Icecast to a different port, export `ICECAST_PORT` before running `radio
start`, or edit `config/secrets.env`. Update `studio` and any listeners to the
same port.

## Liquidsoap validation fails

`radio start --dry-run` and the real start both validate the Liquidsoap script
before spawning the process. If validation fails, read the full error in
`liquidsoap/logs/liquidsoap-check.log`. Common causes:

- a missing `playlist.m3u` file (run `radio gen-playlist` first),
- an invalid path or missing audio file,
- a Liquidsoap version mismatch.

## Secrets are missing or too short

The engine needs `ICECAST_SOURCE_PASSWORD` at minimum. It must be at least 12
characters and use URL-safe punctuation. Copy the example file:

```bash
cp liquidsoap/config/secrets.env.example liquidsoap/config/secrets.env
```

Then edit `liquidsoap/config/secrets.env` and replace the placeholder with a real
password.

## Studio port 5110 is busy

If another process is already on 5110, `app.py` prints an error and exits. Use
`--port` to pick a different port:

```bash
python app.py --port 5111
```

Or use `--restart` to stop the existing listener and start fresh.

## Windows: paths with spaces break things

Install the repo to a path without spaces, such as `C:\radio-tools`. PowerShell
and some subprocess calls handle spaces poorly, and the path probes in the engine
are simpler when the root contains no spaces.

## Windows: firewall prompt the first time Icecast runs

Icecast listens on port 8000 by default. The first time it runs on Windows,
the Defender firewall may ask whether to allow it. Allow it on private networks
if you want other devices on the LAN to listen. Do not expose port 8000 to the
public internet without additional security.

## Windows: Icecast warns about the webroot

When the checked-in `liquidsoap/config/icecast.xml` is rendered for Windows, the
template still contains the Linux web/admin paths. `radio start` rewrites those
paths in the runtime copy to the `share\icecast\web` and `share\icecast\admin`
directories inside your Icecast install. If it cannot find them, it prints
`Icecast webroot not found; using template paths` and Icecast may refuse to
start.

Fix it by setting `ICECAST_WEBROOT` and `ICECAST_ADMINROOT` to the actual
`web` and `admin` folders of your Icecast install, or by moving the Icecast
install to one of the standard paths the engine searches. `radio paths --show`
prints what the next start will use.

## General: `radio` command not found

After `pip install -e .`, the `radio` script should be on your PATH inside the
active virtualenv. On Linux, activate with `. .venv/bin/activate`. On Windows,
activate with `.venv\Scripts\activate`. If you prefer not to activate, use the
full path: `.venv/bin/radio` on Linux or `.venv\Scripts\radio.exe` on Windows.
