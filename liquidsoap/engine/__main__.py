"""Cross-platform radio engine manager — CLI entry point.

This module implements the ``radio`` command, a pure-stdlib Python replacement
for the bash scripts in ``liquidsoap/bin/`` that works on both Linux and
Windows.

Only the standard library is used: ``subprocess``, ``os``, ``sys``, ``time``,
``urllib``, ``signal``, ``pathlib``, ``json``, ``argparse``, ``shutil``.

Commands
--------
    radio start [--live] [--force-root]
    radio stop [--force-root]
    radio status
    radio restart [--live] [--force-root]
    radio smoke [--duration N] [--keep]
    radio gen-playlist [OPTIONS]
    radio bin-paths
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ── paths ──────────────────────────────────────────────────────────────

ENGINE_DIR = Path(__file__).resolve().parent          # liquidsoap/engine/
ROOT_DIR = ENGINE_DIR.parent                            # liquidsoap/
REPO_DIR = ROOT_DIR.parent                              # radio-tools/
LOG_DIR = ROOT_DIR / "logs"
DATA_DIR = ROOT_DIR / "data"
CONFIG_DIR = ROOT_DIR / "config"
SCRIPTS_DIR = ROOT_DIR / "scripts"
BIN_DIR = ROOT_DIR / "bin"
LIB_DIR = ROOT_DIR / "lib"

CONFIG_TEMPLATE = CONFIG_DIR / "icecast.xml"
SECRETS_FILE = CONFIG_DIR / "secrets.env"
RUNTIME_CONFIG = LOG_DIR / "icecast.runtime.xml"
ICECAST_PID_FILE = LOG_DIR / "icecast.pid"
LIQUIDSOAP_PID_FILE = LOG_DIR / "liquidsoap.pid"

IS_WINDOWS = os.name == "nt"
IS_POSIX = not IS_WINDOWS

# Safe charset for secrets (matches start.sh's regex):
#   [-A-Za-z0-9._~!@%+=:,/]
_SAFE_SECRET_RE = re.compile(r"^[-A-Za-z0-9._~!@%+=:,/]+$")
_SAFE_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9.-]+$")

# MP3 frame sync: 0xFF followed by byte whose top 3 bits are 0b111
# (i.e. (byte & 0xE0) == 0xE0).


# ── errors ─────────────────────────────────────────────────────────────


class EngineError(Exception):
    """Fatal error — printed to stderr, exits nonzero."""


# ── config / secrets ───────────────────────────────────────────────────


def load_secrets() -> dict[str, str]:
    """Load config/secrets.env (KEY=VALUE lines) with environment override.

    The process environment takes precedence over the file, matching the
    bash script's behaviour: external env values override file values.
    """
    config: dict[str, str] = {}

    # 1. Read the secrets file if it exists.
    if SECRETS_FILE.is_file():
        for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise EngineError(f"malformed line in {SECRETS_FILE}: {line}")
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key:
                config[key] = value

    # 2. Environment override precedence — explicit env vars win over file.
    for key in (
        "ICECAST_SOURCE_PASSWORD",
        "ICECAST_ADMIN_PASSWORD",
        "ICECAST_RELAY_PASSWORD",
        "ICECAST_HOSTNAME",
        "ICECAST_PORT",
        "ICECAST_HOST",
    ):
        env_val = os.environ.get(key)
        if env_val is not None and env_val != "":
            config[key] = env_val

    # 3. Apply defaults.
    source_pw = config.get("ICECAST_SOURCE_PASSWORD", "")
    if not source_pw:
        raise EngineError(
            "set ICECAST_SOURCE_PASSWORD or create config/secrets.env"
        )
    config.setdefault("ICECAST_ADMIN_PASSWORD", source_pw)
    config.setdefault("ICECAST_RELAY_PASSWORD", source_pw)
    config.setdefault("ICECAST_HOSTNAME", "radio.example.invalid")
    config.setdefault("ICECAST_PORT", "8000")
    config.setdefault("ICECAST_HOST", "127.0.0.1")

    return config


def validate_secrets(config: dict[str, str]) -> None:
    """Validate secret values: >=12 chars, safe charset for XML substitution."""
    for name in ("ICECAST_SOURCE_PASSWORD", "ICECAST_ADMIN_PASSWORD",
                 "ICECAST_RELAY_PASSWORD"):
        value = config[name]
        if len(value) < 12:
            raise EngineError(f"{name} must be at least 12 characters")
        if not _SAFE_SECRET_RE.match(value):
            raise EngineError(
                f"{name} contains characters unsafe for XML substitution"
            )

    hostname = config["ICECAST_HOSTNAME"]
    if not _SAFE_HOSTNAME_RE.match(hostname):
        raise EngineError("ICECAST_HOSTNAME is invalid")

    port_str = config["ICECAST_PORT"]
    if not port_str.isdigit():
        raise EngineError("ICECAST_PORT must be numeric")
    port = int(port_str)
    if not (1 <= port <= 65535):
        raise EngineError("ICECAST_PORT is out of range")


def render_config(config: dict[str, str]) -> str:
    """Render icecast.xml runtime config from the template via string
    substitution (NOT envsubst).

    Replaces ``${KEY}`` placeholders with values from *config*.
    """
    template = CONFIG_TEMPLATE.read_text(encoding="utf-8")
    substitutions = {
        "ICECAST_SOURCE_PASSWORD": config["ICECAST_SOURCE_PASSWORD"],
        "ICECAST_ADMIN_PASSWORD": config["ICECAST_ADMIN_PASSWORD"],
        "ICECAST_RELAY_PASSWORD": config["ICECAST_RELAY_PASSWORD"],
        "ICECAST_HOSTNAME": config["ICECAST_HOSTNAME"],
        "ICECAST_PORT": config["ICECAST_PORT"],
        "RADIO_LOG_DIR": str(LOG_DIR),
    }
    rendered = template
    for key, value in substitutions.items():
        rendered = rendered.replace(f"${{{key}}}", value)
    return rendered


# ── process management (cross-platform) ───────────────────────────────


def is_process_alive(pid: int) -> bool:
    """Check if a process is alive — cross-platform.

    On POSIX: ``os.kill(pid, 0)`` (signal 0 = no signal, just check).
    Additionally checks ``/proc/$pid/status`` for zombie state on Linux,
    since a zombie still responds to ``kill -0`` but is not meaningfully
    alive. On Windows: ``ctypes.OpenProcess`` + ``GetExitCodeProcess``.
    """
    if pid <= 0:
        return False
    if IS_POSIX:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        # On Linux, a zombie (State: Z) still responds to kill -0 but is
        # not alive. Check /proc/$pid/status to distinguish.
        try:
            status_path = Path(f"/proc/{pid}/status")
            if status_path.exists():
                for line in status_path.read_text().splitlines():
                    if line.startswith("State:"):
                        # State: Z (zombie) means the process is defunct.
                        state = line.split()
                        if len(state) >= 2 and state[1] == "Z":
                            return False
                        return True
        except (OSError, UnicodeDecodeError, IndexError):
            pass
        return True
    else:
        # Windows: use ctypes to check if the process is still running.
        import ctypes
        import ctypes.wintypes

        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.wintypes.DWORD()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            )
            if not ok:
                return False
            # STILL_ACTIVE = 259
            return exit_code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)


def read_pid_file(pid_file: Path) -> int | None:
    """Read a PID file and return the integer PID, or None if invalid/missing."""
    if not pid_file.is_file():
        return None
    try:
        text = pid_file.read_text().strip()
        pid = int(text)
        if pid > 0:
            return pid
    except (ValueError, OSError):
        pass
    return None


def verify_pid_cmdline(pid: int, expected_substring: str) -> bool:
    """POSIX-only safety check: verify the pid's cmdline contains *expected*.

    Reads ``/proc/$pid/cmdline`` and checks if *expected_substring* is in it.
    Returns True on POSIX if the check passes (or /proc is unavailable).
    Always returns True on Windows (no /proc equivalent).
    """
    if IS_WINDOWS:
        # No /proc on Windows; skip this verification.
        return True
    try:
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        if not cmdline_path.exists():
            # /proc not available (non-Linux POSIX); skip.
            return True
        cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode(
            "utf-8", errors="replace"
        )
        return expected_substring in cmdline
    except (OSError, UnicodeDecodeError):
        return True


def terminate_pid(pid: int, *, grace_seconds: float = 10.0) -> bool:
    """Terminate a process gracefully, then forcefully after a grace window.

    On POSIX: SIGTERM, wait, then SIGKILL.
    On Windows: ``taskkill /PID <pid> /T /F`` (tree kill).  We use taskkill
    for the hard kill because it reliably terminates child processes in the
    process tree (Icecast may spawn helpers).  The soft attempt uses
    ``ctypes.TerminateProcess`` which is the Win32 API equivalent of SIGTERM
    for a graceful close on the process handle.

    Returns True if the process is no longer alive.
    """
    if not is_process_alive(pid):
        return True

    if IS_POSIX:
        # Soft kill: SIGTERM
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not is_process_alive(pid):
                return True
            time.sleep(0.2)

        # Hard kill: SIGKILL
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        time.sleep(0.3)
        return not is_process_alive(pid)
    else:
        # Windows: soft attempt via ctypes TerminateProcess, then taskkill /T /F.
        import ctypes

        # Soft attempt: TerminateProcess (still fairly hard, but gives the
        # process a brief moment before we escalate to tree-kill).
        handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)  # TERMINATE
        if handle:
            try:
                ctypes.windll.kernel32.TerminateProcess(handle, 1)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)

        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not is_process_alive(pid):
                return True
            time.sleep(0.2)

        # Hard kill: taskkill /PID <pid> /T /F (kill entire process tree)
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.3)
        return not is_process_alive(pid)


# ── binary resolution ──────────────────────────────────────────────────


def resolve_icecast_bin() -> str | None:
    """Resolve the Icecast binary path.

    Precedence: env ICECAST_BIN > PATH lookup > platform defaults.
    """
    env_bin = os.environ.get("ICECAST_BIN")
    if env_bin:
        return env_bin

    # PATH lookup
    which = shutil.which("icecast2") or shutil.which("icecast")
    if which:
        return which

    # Platform defaults
    if IS_POSIX:
        for candidate in ("/usr/bin/icecast2", "/usr/bin/icecast"):
            if Path(candidate).exists():
                return candidate
    else:
        # Windows: %ProgramFiles%/Icecast2 2.4.4/bin/icecast.exe etc.
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)",
                                           r"C:\Program Files (x86)")
        search_bases = [program_files, program_files_x86]
        for base in search_bases:
            base_path = Path(base)
            if not base_path.exists():
                continue
            # Match Icecast*/bin/icecast.exe
            for icecast_dir in sorted(base_path.glob("Icecast*")):
                exe = icecast_dir / "bin" / "icecast.exe"
                if exe.exists():
                    return str(exe)
                # Some builds put icecast.exe at the top level
                exe = icecast_dir / "icecast.exe"
                if exe.exists():
                    return str(exe)

    return None


def resolve_liquidsoap_bin() -> str | None:
    """Resolve the Liquidsoap binary path.

    Precedence: env LIQUIDSOAP_BIN > PATH lookup > platform defaults.
    """
    env_bin = os.environ.get("LIQUIDSOAP_BIN")
    if env_bin:
        return env_bin

    # PATH lookup
    which = shutil.which("liquidsoap")
    if which:
        return which

    # Platform defaults
    if IS_POSIX:
        for candidate in ("/usr/bin/liquidsoap", "/usr/local/bin/liquidsoap"):
            if Path(candidate).exists():
                return candidate
    else:
        # Windows: liquidsoap.exe on PATH (covered by which above), or
        # common install locations.
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            candidate = Path(local_appdata) / "liquidsoap" / "liquidsoap.exe"
            if candidate.exists():
                return str(candidate)
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        candidate = Path(program_files) / "liquidsoap" / "liquidsoap.exe"
        if candidate.exists():
            return str(candidate)

    return None


# ── HTTP helpers (urllib, not curl) ────────────────────────────────────


def fetch_url(url: str, timeout: float = 2.0) -> bytes | None:
    """Fetch a URL and return the response body, or None on failure."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return None


def fetch_status_json(port: int, host: str = "127.0.0.1") -> dict | None:
    """Fetch and parse Icecast status-json.xsl."""
    url = f"http://{host}:{port}/status-json.xsl"
    body = fetch_url(url, timeout=3.0)
    if body is None:
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None


def check_source_mount(port: int, host: str = "127.0.0.1",
                       mount: str = "/radio.mp3") -> bool:
    """Check if the /radio.mp3 mount appears in the Icecast status JSON."""
    doc = fetch_status_json(port, host)
    if doc is None:
        return False
    source = doc.get("icestats", {}).get("source", {})
    # source can be a dict (single mount) or a list (multiple mounts)
    if isinstance(source, dict):
        return source.get("listenurl", "").endswith(mount)
    if isinstance(source, list):
        for s in source:
            if isinstance(s, dict) and s.get("listenurl", "").endswith(mount):
                return True
    return False


def wait_for_icecast(pid: int, port: int, host: str = "127.0.0.1",
                    max_attempts: int = 40, interval: float = 0.25) -> bool:
    """Poll Icecast readiness via status-json.xsl."""
    for _ in range(max_attempts):
        if not is_process_alive(pid):
            return False
        if fetch_url(f"http://{host}:{port}/status-json.xsl",
                     timeout=1.0) is not None:
            return True
        time.sleep(interval)
    return False


def wait_for_source(pid: int, port: int, host: str = "127.0.0.1",
                    max_attempts: int = 60, interval: float = 0.25) -> bool:
    """Wait for the /radio.mp3 mount to appear in Icecast status."""
    for _ in range(max_attempts):
        if not is_process_alive(pid):
            return False
        if check_source_mount(port, host):
            return True
        time.sleep(interval)
    return False


# ── start / stop / status ──────────────────────────────────────────────


def check_root(force_root: bool) -> None:
    """Refuse root on POSIX unless --force-root."""
    if IS_POSIX and hasattr(os, "geteuid") and os.geteuid() == 0:
        if not force_root:
            raise EngineError(
                "refusing to run as root; pass --force-root only if you "
                "accept the risk"
            )


def ensure_dirs() -> None:
    """Create log and data directories."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def write_pid_file(pid_file: Path, pid: int) -> None:
    pid_file.write_text(f"{pid}\n", encoding="utf-8")


def remove_pid_file(pid_file: Path) -> None:
    try:
        pid_file.unlink(missing_ok=True)
    except OSError:
        pass


def check_already_running() -> None:
    """Check if Icecast or Liquidsoap are already running (pid files exist)."""
    for pid_file, label in (
        (ICECAST_PID_FILE, "Icecast"),
        (LIQUIDSOAP_PID_FILE, "Liquidsoap"),
    ):
        pid = read_pid_file(pid_file)
        if pid is not None and is_process_alive(pid):
            raise EngineError(
                f"already running ({label} PID {pid}, file: {pid_file})"
            )
        remove_pid_file(pid_file)


def spawn_process(args: list[str], log_file: Path, env: dict[str, str]) -> int:
    """Spawn a detached background process and return its PID.

    On POSIX: uses ``subprocess.Popen`` with ``start_new_session=True`` and
    redirects stdout/stderr to *log_file*.
    On Windows: uses ``subprocess.Popen`` with ``CREATE_NEW_PROCESS_GROUP``
    and ``DETACHED_PROCESS`` flags.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    stdout = open(log_file, "a", encoding="utf-8", errors="replace")

    kwargs: dict[str, Any] = {
        "stdout": stdout,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "env": env,
        "cwd": str(ROOT_DIR),
    }

    if IS_POSIX:
        kwargs["start_new_session"] = True
    else:
        # Windows: detach from the console so the child survives.
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS

    proc = subprocess.Popen(args, **kwargs)
    stdout.close()
    return proc.pid


def do_start(mode: str = "station", force_root: bool = False) -> dict[str, int]:
    """Start Icecast and Liquidsoap.

    Returns a dict with 'icecast' and 'liquidsoap' PIDs.
    Mirrors start.sh exactly.
    """
    check_root(force_root)
    ensure_dirs()

    icecast_bin = resolve_icecast_bin()
    if not icecast_bin:
        raise EngineError("icecast executable not found (set ICECAST_BIN)")
    liquidsoap_bin = resolve_liquidsoap_bin()
    if not liquidsoap_bin:
        raise EngineError(
            "liquidsoap executable not found (set LIQUIDSOAP_BIN)"
        )

    check_already_running()

    # Load and validate config.
    config = load_secrets()
    validate_secrets(config)

    # Render runtime config.
    rendered = render_config(config)
    RUNTIME_CONFIG.write_text(rendered, encoding="utf-8")
    if IS_POSIX:
        try:
            os.chmod(RUNTIME_CONFIG, 0o600)
        except OSError:
            pass

    # Set up environment for child processes.
    child_env = os.environ.copy()
    child_env["ICECAST_SOURCE_PASSWORD"] = config["ICECAST_SOURCE_PASSWORD"]
    child_env["ICECAST_ADMIN_PASSWORD"] = config["ICECAST_ADMIN_PASSWORD"]
    child_env["ICECAST_RELAY_PASSWORD"] = config["ICECAST_RELAY_PASSWORD"]
    child_env["ICECAST_HOSTNAME"] = config["ICECAST_HOSTNAME"]
    child_env["ICECAST_PORT"] = config["ICECAST_PORT"]
    child_env["ICECAST_HOST"] = config["ICECAST_HOST"]
    child_env["RADIO_LOG_DIR"] = str(LOG_DIR)
    child_env["RADIO_ROOT"] = str(ROOT_DIR)
    child_env.setdefault(
        "RADIO_PLAYLIST", str(DATA_DIR / "playlist.m3u")
    )
    child_env.setdefault(
        "RADIO_PLAYLIST_TRIGGER", str(DATA_DIR / "playlist.trigger")
    )

    # Touch the playlist trigger.
    trigger = Path(child_env["RADIO_PLAYLIST_TRIGGER"])
    trigger.touch()

    port = int(config["ICECAST_PORT"])
    host = config.get("ICECAST_HOST", "127.0.0.1")

    # Validate the Liquidsoap script.
    script = SCRIPTS_DIR / f"{mode}.liq"
    if not script.exists():
        raise EngineError(f"Liquidsoap script not found: {script}")

    check_log = LOG_DIR / "liquidsoap-check.log"
    check_result = subprocess.run(
        [liquidsoap_bin, "--check", str(script)],
        stdout=open(check_log, "a"),
        stderr=subprocess.STDOUT,
        cwd=str(ROOT_DIR),
        env=child_env,
        timeout=30,
        check=False,
    )
    if check_result.returncode != 0:
        raise EngineError(
            f"Liquidsoap validation failed; see {check_log}"
        )

    # State for abort cleanup.
    icecast_pid: int | None = None
    liquidsoap_pid: int | None = None

    def abort(message: str) -> None:
        if liquidsoap_pid is not None:
            terminate_pid(liquidsoap_pid, grace_seconds=5.0)
            remove_pid_file(LIQUIDSOAP_PID_FILE)
        if icecast_pid is not None:
            terminate_pid(icecast_pid, grace_seconds=5.0)
            remove_pid_file(ICECAST_PID_FILE)
        try:
            RUNTIME_CONFIG.unlink(missing_ok=True)
        except OSError:
            pass
        raise EngineError(message)

    # Start Icecast.
    icecast_pid = spawn_process(
        [icecast_bin, "-c", str(RUNTIME_CONFIG)],
        LOG_DIR / "icecast-console.log",
        child_env,
    )
    write_pid_file(ICECAST_PID_FILE, icecast_pid)

    if not wait_for_icecast(icecast_pid, port, host):
        abort(
            f"Icecast did not become ready on port {port}; "
            f"see {LOG_DIR / 'icecast-console.log'}"
        )

    # Start Liquidsoap.
    liquidsoap_pid = spawn_process(
        [liquidsoap_bin, str(script)],
        LOG_DIR / "liquidsoap.log",
        child_env,
    )
    write_pid_file(LIQUIDSOAP_PID_FILE, liquidsoap_pid)

    if not wait_for_source(liquidsoap_pid, port, host):
        abort(
            f"Liquidsoap did not connect /radio.mp3; "
            f"see {LOG_DIR / 'liquidsoap.log'}"
        )

    print(
        f"Started Icecast (PID {icecast_pid}) and Liquidsoap {mode} mode "
        f"(PID {liquidsoap_pid})."
    )
    print(f"Stream: http://{host}:{port}/radio.mp3")
    return {"icecast": icecast_pid, "liquidsoap": liquidsoap_pid}


def do_stop(force_root: bool = False) -> bool:
    """Stop Liquidsoap first, then Icecast.

    Returns True if all components stopped cleanly.
    """
    check_root(force_root)

    components = [
        ("Liquidsoap", LIQUIDSOAP_PID_FILE, str(SCRIPTS_DIR) + "/"),
        ("Icecast", ICECAST_PID_FILE, str(RUNTIME_CONFIG)),
    ]

    all_ok = True

    for label, pid_file, expected in components:
        pid = read_pid_file(pid_file)
        if pid is None:
            print(f"{label} is not running (no PID file).")
            continue

        if not is_process_alive(pid):
            print(f"{label} was already stopped; removing stale PID file.")
            remove_pid_file(pid_file)
            continue

        # POSIX-only cmdline verification (optional safety check).
        if IS_POSIX and not verify_pid_cmdline(pid, expected):
            print(
                f"Refusing to stop PID {pid}: it does not look like {label}.",
                file=sys.stderr,
            )
            all_ok = False
            continue

        print(f"Stopping {label} (PID {pid})...")
        if terminate_pid(pid, grace_seconds=10.0):
            remove_pid_file(pid_file)
            print(f"Stopped {label} (PID {pid}).")
        else:
            remove_pid_file(pid_file)
            print(
                f"{label} (PID {pid}) may still be running after stop attempt.",
                file=sys.stderr,
            )
            all_ok = False

    if all_ok:
        try:
            RUNTIME_CONFIG.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"Removed generated runtime config; logs retained in {LOG_DIR}.")

    return all_ok


def parse_status(doc: dict) -> dict:
    """Extract listeners, server_type, title, audio_info from status JSON."""
    src = doc.get("icestats", {}).get("source", {})
    # source can be a list if multiple mounts exist; find /radio.mp3
    if isinstance(src, list):
        src = next(
            (s for s in src if isinstance(s, dict)
             and "/radio.mp3" in s.get("listenurl", "")),
            src[0] if src else {},
        )
    if not isinstance(src, dict):
        src = {}
    return {
        "listeners": src.get("listeners", "?"),
        "server_type": src.get("server_type", "?"),
        "title": src.get("title", "?"),
        "audio_info": src.get("audio_info", "?"),
    }


def do_status() -> int:
    """Report each component UP/DOWN with PID, plus Icecast status.

    Returns exit code: 0 if both up, 1 if any down.
    """
    config = load_secrets()
    port = int(config.get("ICECAST_PORT", "8000"))
    host = config.get("ICECAST_HOST", "127.0.0.1")

    exit_code = 0

    for label, pid_file in (
        ("Icecast", ICECAST_PID_FILE),
        ("Liquidsoap", LIQUIDSOAP_PID_FILE),
    ):
        pid = read_pid_file(pid_file)
        if pid is not None and is_process_alive(pid):
            print(f"{label}: UP (PID {pid})")
        else:
            print(f"{label}: DOWN")
            exit_code = 1

    # Query Icecast status.
    doc = fetch_status_json(port, host)
    if doc is not None:
        info = parse_status(doc)
        print(f"  listeners:  {info['listeners']}")
        print(f"  server_type: {info['server_type']}")
        print(f"  title:       {info['title']}")
        print(f"  audio_info:  {info['audio_info']}")
    else:
        print("  (Icecast status unreachable)")

    return exit_code


# ── smoke test ────────────────────────────────────────────────────────


def check_mp3_sync(data: bytes) -> bool:
    """Check if *data* contains MP3 frame sync words (0xFF 0xEx)."""
    if len(data) < 2:
        return False
    for i in range(len(data) - 1):
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            return True
    return False


def stream_mp3(url: str, duration: float) -> bytes:
    """Stream from *url* for *duration* seconds, collecting raw bytes.

    Uses urllib streaming (not curl).
    """
    collected = bytearray()
    deadline = time.monotonic() + duration
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=duration + 2) as resp:
            while time.monotonic() < deadline:
                chunk = resp.read(8192)
                if not chunk:
                    break
                collected.extend(chunk)
    except (urllib.error.URLError, OSError, TimeoutError):
        pass
    return bytes(collected)


def do_smoke(duration: int = 20, keep: bool = False) -> int:
    """Run a full end-to-end smoke test.

    Starts the chain, captures *duration* seconds of MP3 audio, verifies
    Icecast reports a listener and audio/mpeg, then stops the chain.
    Exits nonzero on any failure.
    """
    if duration < 5:
        raise EngineError("--duration must be at least 5 seconds")

    print(f"=== Radio smoke test ({duration}s capture) ===")
    print("Starting chain...")
    do_start()

    stream_url = f"http://127.0.0.1:{os.environ.get('ICECAST_PORT', '8000')}/radio.mp3"
    status_url = f"http://127.0.0.1:{os.environ.get('ICECAST_PORT', '8000')}/status-json.xsl"

    try:
        # Capture MP3 bytes.
        print(f"Capturing {duration}s of stream from {stream_url} ...")
        data = stream_mp3(stream_url, duration)
        total_bytes = len(data)
        print(f"Captured {total_bytes} bytes of stream data.")

        # At 128 kbps, duration seconds ~ duration * 16000 bytes.
        # Require at least 50% of theoretical minimum.
        min_bytes = duration * 8000
        if total_bytes < min_bytes:
            raise EngineError(
                f"stream capture too short: {total_bytes} bytes "
                f"(expected at least {min_bytes} for {duration}s at 128kbps)"
            )

        # Verify MP3 sync bytes.
        if not check_mp3_sync(data):
            raise EngineError("no MP3 frame sync bytes found in captured data")
        print("MP3 frame sync bytes detected in stream data.")

        # Query Icecast status.
        # Briefly re-connect to ensure listener count >= 1.
        try:
            req = urllib.request.Request(stream_url)
            with urllib.request.urlopen(req, timeout=5) as _:
                time.sleep(1)
        except (urllib.error.URLError, OSError):
            pass

        doc = fetch_status_json(
            int(os.environ.get("ICECAST_PORT", "8000"))
        )
        if doc is None:
            raise EngineError("failed to fetch status-json.xsl")

        info = parse_status(doc)
        print("--- Icecast status ---")
        print(f"  listeners:  {info['listeners']}")
        print(f"  server_type: {info['server_type']}")
        print(f"  title:       {info['title']}")
        print(f"  audio_info:  {info['audio_info']}")

        # Assertions.
        if info["server_type"] != "audio/mpeg":
            raise EngineError(
                f"server_type is '{info['server_type']}', expected 'audio/mpeg'"
            )

        listeners = info["listeners"]
        if listeners == "?" or (isinstance(listeners, int) and listeners < 1):
            raise EngineError(
                f"listener count is {listeners}, expected at least 1"
            )

        print()
        print("=== SMOKE TEST PASSED ===")
        print(
            f"  Stream:  {total_bytes} bytes of MP3 audio captured "
            f"over {duration}s"
        )
        print(f"  Listener count: {listeners}")
        print(f"  Now-playing:    {info['title']}")
        print(f"  Audio:          {info['server_type']} ({info['audio_info']})")
        return 0

    except EngineError:
        raise
    finally:
        if not keep:
            print("Stopping chain...")
            try:
                do_stop()
            except EngineError as exc:
                print(f"smoke: stop had errors: {exc}", file=sys.stderr)


# ── gen-playlist ───────────────────────────────────────────────────────


def _parse_slot_seconds(slot: str) -> int:
    """Parse a slot string like '30min', '1h', '2h', '90s' into seconds."""
    slot = slot.strip().lower()
    if slot.endswith("min"):
        return int(slot[:-3]) * 60
    if slot.endswith("h"):
        return int(slot[:-1]) * 3600
    if slot.endswith("s"):
        return int(slot[:-1])
    try:
        return int(slot) * 60  # bare number = minutes
    except ValueError:
        raise EngineError(f"could not parse slot duration: {slot}")


def _current_hour() -> int:
    """Return the current local hour (0-23)."""
    import datetime
    return datetime.datetime.now().hour


def _current_epoch_hour() -> int:
    """Return current epoch time divided by 3600 (hourly seed)."""
    return int(time.time()) // 3600


def gen_playlist_once(
    source_dir: str | None = None,
    library_file: str | None = None,
    rotation_file: str | None = None,
    output_file: str | None = None,
    trigger_file: str | None = None,
    slot: str = "1h",
    daypart: str | None = None,
    hour: int | None = None,
    seed: int | None = None,
    dry_run: bool = False,
) -> None:
    """Generate one playlist run — ports gen-playlist.sh generate_once()."""
    if rotation_file is None:
        rotation_file = str(CONFIG_DIR / "rotation.json")
    if output_file is None:
        output_file = os.environ.get(
            "RADIO_PLAYLIST", str(DATA_DIR / "playlist.m3u")
        )
    if trigger_file is None:
        trigger_file = os.environ.get(
            "RADIO_PLAYLIST_TRIGGER", str(DATA_DIR / "playlist.trigger")
        )
    if source_dir is None and library_file is None:
        source_dir = os.environ.get(
            "RADIO_MUSIC_DIR", str(REPO_DIR / "studio" / "music")
        )

    # Validate rotation file.
    if not Path(rotation_file).is_file():
        raise EngineError(f"rotation file not found: {rotation_file}")

    if not output_file.endswith(".m3u"):
        raise EngineError("--output must end in .m3u")

    input_mode = "library" if library_file else "source"

    if input_mode == "library":
        if not Path(library_file).is_file():
            raise EngineError(f"library file not found: {library_file}")
    else:
        if not Path(source_dir).is_dir():
            raise EngineError(f"music directory not found: {source_dir}")

    if hour is not None and not (0 <= hour <= 23):
        raise EngineError("--hour must be between 0 and 23")

    run_hour = hour if hour is not None else _current_hour()
    run_seed = seed if seed is not None else _current_epoch_hour()

    # Build playlistgen command.
    if input_mode == "library":
        input_args = ["--library", library_file]
    else:
        input_args = [source_dir]

    command = [
        "playlistgen",
        *input_args,
        "--rotation", rotation_file,
        "--hour", str(run_hour),
        "--slot", slot,
        "--seed", str(run_seed),
        "--output", output_file,
    ]
    if daypart:
        command.extend(["--daypart", daypart])

    sidecar_file = output_file.rsplit(".", 1)[0] + ".json"

    if dry_run:
        # Print the planned command without executing.
        print("Dry run:", " ".join(command))
        print(
            f"Would validate {sidecar_file} against {output_file} "
            f"and touch {trigger_file}."
        )
        return

    # Ensure output and trigger directories exist.
    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    Path(trigger_file).parent.mkdir(parents=True, exist_ok=True)

    # Try to use broadcast.playlistgen directly (importable from editable install).
    try:
        from broadcast.playlistgen import (
            scan_folder,
            load_library,
            load_rotation,
            RotationEngine,
            write_m3u,
            write_json_sidecar,
        )
        from lib.playlist_loader import validate_playlist_pair

        # Load tracks.
        if input_mode == "library":
            assert library_file is not None
            tracks = load_library(library_file)
        else:
            assert source_dir is not None
            tracks = scan_folder(source_dir)

        if not tracks:
            raise EngineError("No audio files found.")

        rotation = load_rotation(rotation_file)
        target = _parse_slot_seconds(slot)

        engine = RotationEngine(
            tracks, rotation, seed=run_seed, daypart=daypart
        )
        playlist = engine.generate(target_duration=target)

        if not playlist:
            raise EngineError(
                "could not generate a playlist "
                "(check rotation config and library)"
            )

        # Write to temp files, validate, then publish atomically.
        import tempfile
        temp_dir = Path(output_file).parent / ".playlistgen.tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_m3u = temp_dir / "playlist.m3u"
        temp_json = temp_dir / "playlist.json"

        write_m3u(playlist, str(temp_m3u))
        write_json_sidecar(
            playlist, str(temp_m3u), seed=run_seed,
            daypart=daypart, target_duration=target,
        )
        # write_json_sidecar writes sidecar next to the m3u path,
        # so it will be temp_dir/playlist.json.
        validate_playlist_pair(temp_json, temp_m3u, require_files=True)

        # Publish atomically.
        shutil.move(str(temp_json), sidecar_file)
        shutil.move(str(temp_m3u), output_file)
        Path(trigger_file).touch()
        temp_dir.rmdir()

        total = sum(t.get("duration", 0) or 0.0 for t in playlist)
        print(
            f"Generated playlist: {len(playlist)} tracks, "
            f"{total:.0f}s / {target:.0f}s target"
        )
        print(f"  M3U:  {output_file}")
        print(f"  JSON: {sidecar_file}")
        if daypart:
            print(f"  Daypart: {daypart}")
        print(f"  Seed: {run_seed}")
        print(f"Published {output_file} and signaled {trigger_file}.")

    except ImportError:
        # Fall back to the playlistgen CLI command.
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=60, check=False)
        if result.returncode != 0:
            raise EngineError(
                f"playlistgen failed: {result.stdout}\n{result.stderr}"
            )
        print(result.stdout)
        # Validate and publish.
        try:
            from lib.playlist_loader import validate_playlist_pair
            validate_playlist_pair(
                sidecar_file, output_file, require_files=True
            )
        except ImportError:
            pass
        Path(trigger_file).touch()
        print(f"Published {output_file} and signaled {trigger_file}.")


def do_gen_playlist(
    source_dir: str | None = None,
    library_file: str | None = None,
    rotation_file: str | None = None,
    output_file: str | None = None,
    trigger_file: str | None = None,
    slot: str = "1h",
    daypart: str | None = None,
    hour: int | None = None,
    seed: int | None = None,
    loop: bool = False,
    dry_run: bool = False,
) -> None:
    """Generate playlist(s), optionally looping at hour boundaries."""
    import datetime

    while True:
        gen_playlist_once(
            source_dir=source_dir,
            library_file=library_file,
            rotation_file=rotation_file,
            output_file=output_file,
            trigger_file=trigger_file,
            slot=slot,
            daypart=daypart,
            hour=hour,
            seed=seed,
            dry_run=dry_run,
        )

        if not loop or dry_run:
            break

        # Sleep until the next hour boundary.
        now = datetime.datetime.now()
        wait_seconds = 3600 - now.minute * 60 - now.second
        print(f"Next generation in {wait_seconds}s at the next hour boundary.")
        time.sleep(wait_seconds)


# ── bin-paths ──────────────────────────────────────────────────────────


def do_bin_paths() -> int:
    """Print resolved paths for liquidsoap and icecast binaries."""
    icecast = resolve_icecast_bin()
    liquidsoap = resolve_liquidsoap_bin()

    print(f"icecast:    {icecast or '(not found)'}")
    print(f"liquidsoap: {liquidsoap or '(not found)'}")
    print(f"platform:   {'Windows' if IS_WINDOWS else 'POSIX'}")

    if not icecast or not liquidsoap:
        print()
        print("Set ICECAST_BIN / LIQUIDSOAP_BIN to override, or install:")
        if IS_WINDOWS:
            print("  Icecast:   https://icecast.org/download/")
            print("  Liquidsoap: https://www.liquidsoap.info/download")
        else:
            print("  Debian/Ubuntu: sudo apt install icecast2 liquidsoap")
        return 1

    return 0


# ── CLI ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="radio",
        description=(
            "Cross-platform radio engine manager for the Liquidsoap + "
            "Icecast broadcast chain."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # start
    p_start = sub.add_parser("start", help="Start Icecast and Liquidsoap.")
    p_start.add_argument("--live", action="store_true",
                         help="Use the live-assist script (PulseAudio/JACK).")
    p_start.add_argument("--force-root", action="store_true",
                         help="Allow running as root (POSIX only).")

    # stop
    p_stop = sub.add_parser("stop", help="Stop Liquidsoap and Icecast.")
    p_stop.add_argument("--force-root", action="store_true",
                        help="Allow running as root (POSIX only).")

    # status
    sub.add_parser("status", help="Report component status and Icecast info.")

    # restart
    p_restart = sub.add_parser("restart", help="Stop then start.")
    p_restart.add_argument("--live", action="store_true",
                           help="Use the live-assist script.")
    p_restart.add_argument("--force-root", action="store_true",
                           help="Allow running as root (POSIX only).")

    # smoke
    p_smoke = sub.add_parser("smoke", help="Run an end-to-end smoke test.")
    p_smoke.add_argument("--duration", type=int, default=20,
                         help="Capture duration in seconds (default: 20).")
    p_smoke.add_argument("--keep", action="store_true",
                         help="Leave the chain running after the test.")

    # gen-playlist
    p_gen = sub.add_parser(
        "gen-playlist",
        help="Generate a playlist using broadcast/playlistgen.",
    )
    p_gen.add_argument("--source", type=str, default=None,
                       help="Scan an audio directory.")
    p_gen.add_argument("--library", type=str, default=None,
                       help="Use a broadcast-format library JSON index.")
    p_gen.add_argument("--rotation", type=str, default=None,
                       help="Rotation JSON file.")
    p_gen.add_argument("--output", type=str, default=None,
                       help="Destination M3U file.")
    p_gen.add_argument("--trigger", type=str, default=None,
                       help="Reload trigger file.")
    p_gen.add_argument("--slot", type=str, default="1h",
                       help="Slot duration (default: 1h).")
    p_gen.add_argument("--daypart", type=str, default=None,
                       help="Apply a named daypart's weights.")
    p_gen.add_argument("--hour", type=int, default=None,
                       help="Program-clock hour 0-23.")
    p_gen.add_argument("--seed", type=int, default=None,
                       help="Deterministic seed.")
    p_gen.add_argument("--loop", action="store_true",
                       help="Repeat at the next hour boundary.")
    p_gen.add_argument("--dry-run", action="store_true",
                       help="Print planned commands without writing.")

    # bin-paths
    sub.add_parser("bin-paths", help="Print resolved binary paths.")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "start":
            mode = "live" if args.live else "station"
            do_start(mode=mode, force_root=args.force_root)
            return 0

        elif args.command == "stop":
            do_stop(force_root=args.force_root)
            return 0

        elif args.command == "status":
            return do_status()

        elif args.command == "restart":
            do_stop(force_root=args.force_root)
            do_start(
                mode="live" if args.live else "station",
                force_root=args.force_root,
            )
            return 0

        elif args.command == "smoke":
            return do_smoke(duration=args.duration, keep=args.keep)

        elif args.command == "gen-playlist":
            do_gen_playlist(
                source_dir=args.source,
                library_file=args.library,
                rotation_file=args.rotation,
                output_file=args.output,
                trigger_file=args.trigger,
                slot=args.slot,
                daypart=args.daypart,
                hour=args.hour,
                seed=args.seed,
                loop=args.loop,
                dry_run=args.dry_run,
            )
            return 0

        elif args.command == "bin-paths":
            return do_bin_paths()

        else:
            parser.print_help()
            return 2

    except EngineError as exc:
        print(f"radio: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())