"""utilities — Small broadcast helper tools.

Three subcommands exposed as separate console entry points:

  broadcast-clock   Station clock: local + UTC, 24h/12h
  intro-outro       Report intro/outro durations for talk-over planning
  countdown         Big countdown timer to a target wall-clock time
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import click

try:
    from mutagen import File as MutagenFile
    _HAS_MUTAGEN = True
except Exception:  # pragma: no cover
    _HAS_MUTAGEN = False


# ── broadcast-clock ─────────────────────────────────────────────────────

@click.command(
    name="broadcast-clock",
    help=(
        "Print the station clock: local time and UTC, in 24h and 12h.\n\n"
        "Use --watch to refresh every second until you press Ctrl-C."
    ),
)
@click.option("--watch", is_flag=True,
              help="Continuously update every second (Ctrl-C to stop).")
@click.option("--format", "fmt", type=click.Choice(["24h", "12h", "both"]),
              default="both", help="Time format to display. Default: both.")
def broadcast_clock_cli(watch, fmt):
    """Entry point for broadcast-clock."""
    def _render() -> str:
        now_local = datetime.now()
        now_utc = datetime.now(timezone.utc)

        lines = []
        lines.append("=" * 40)
        lines.append("STATION CLOCK")
        lines.append("=" * 40)

        if fmt in ("24h", "both"):
            lines.append(f"  Local (24h): {now_local.strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append(f"  UTC   (24h): {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")

        if fmt in ("12h", "both"):
            lines.append(f"  Local (12h): {now_local.strftime('%Y-%m-%d %I:%M:%S %p')}")
            lines.append(f"  UTC   (12h): {now_utc.strftime('%Y-%m-%d %I:%M:%S %p')}")

        # Day of week
        dow = now_local.strftime("%A")
        lines.append(f"  Day:         {dow}")

        # Compute seconds since midnight
        midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        secs = (now_local - midnight).total_seconds()
        lines.append(f"  Secs since midnight: {int(secs):,}")

        lines.append("=" * 40)
        return "\n".join(lines)

    if watch:
        try:
            while True:
                # Clear screen and print
                click.echo("\033[2J\033[H", nl=False)
                click.echo(_render())
                time.sleep(1)
        except KeyboardInterrupt:
            click.echo("\nClock stopped.")
    else:
        click.echo(_render())


# ── intro-outro ─────────────────────────────────────────────────────────

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wav", ".wma"}


def _ffprobe_duration(path: str) -> float | None:
    """Get total duration via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return None


def _ffprobe_audio_info(path: str) -> dict | None:
    """Get audio stream info via ffprobe (duration, sample_rate, channels)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", path],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout)
            streams = data.get("streams", [])
            audio_stream = None
            for s in streams:
                if s.get("codec_type") == "audio":
                    audio_stream = s
                    break
            if audio_stream:
                duration = float(audio_stream.get("duration", 0) or
                                 data.get("format", {}).get("duration", 0))
                return {
                    "duration": duration,
                    "sample_rate": int(audio_stream.get("sample_rate", 0)),
                    "channels": int(audio_stream.get("channels", 0)),
                    "codec": audio_stream.get("codec_name", "unknown"),
                }
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError, Exception):
        pass
    return None


def _wav_duration(path: str) -> float | None:
    """Get duration of a WAV file using struct (no ffprobe needed)."""
    try:
        with open(path, "rb") as f:
            # Read RIFF header
            riff = f.read(12)
            if riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
                return None
            # Read chunks to find fmt and data
            fmt_data = None
            data_size = None
            while True:
                chunk_header = f.read(8)
                if len(chunk_header) < 8:
                    break
                chunk_id = chunk_header[:4]
                chunk_size = struct.unpack("<I", chunk_header[4:8])[0]
                if chunk_id == b"fmt ":
                    fmt_data = f.read(chunk_size)
                    if chunk_size % 2:
                        f.read(1)  # padding
                elif chunk_id == b"data":
                    data_size = chunk_size
                    break
                else:
                    f.read(chunk_size)
                    if chunk_size % 2:
                        f.read(1)  # padding
            if fmt_data and data_size is not None:
                # Parse fmt chunk: audio_format(2), channels(2),
                # sample_rate(4), byte_rate(4), block_align(2), bits_per_sample(2)
                audio_format = struct.unpack("<H", fmt_data[0:2])[0]
                channels = struct.unpack("<H", fmt_data[2:4])[0]
                sample_rate = struct.unpack("<I", fmt_data[4:8])[0]
                bits_per_sample = struct.unpack("<H", fmt_data[14:16])[0]
                byte_rate = sample_rate * channels * bits_per_sample // 8
                if byte_rate > 0:
                    return data_size / byte_rate
    except Exception:
        pass
    return None


def _mutagen_duration(path: str) -> float | None:
    """Get duration via mutagen as a fallback."""
    if not _HAS_MUTAGEN:
        return None
    try:
        audio = MutagenFile(path)
        if audio and audio.info:
            return float(audio.info.length)
    except Exception:
        pass
    return None


def _get_duration(path: str) -> float | None:
    """Get duration trying ffprobe → mutagen → WAV struct."""
    d = _ffprobe_duration(path)
    if d is not None:
        return d
    d = _mutagen_duration(path)
    if d is not None:
        return d
    if path.lower().endswith(".wav"):
        d = _wav_duration(path)
        if d is not None:
            return d
    return None


def _get_intro_outro(path: str, n_seconds: float) -> dict:
    """Report intro and outro durations for talk-over planning.

    For the intro: the first *n_seconds* are the "talk-over" zone.
    For the outro: the last *n_seconds* are the "fade/talk-over" zone.

    With ffprobe we can also report the average volume of those zones
    (lower = easier to talk over). Without ffprobe we just report the
    time ranges.
    """
    duration = _get_duration(path)
    if duration is None or duration <= 0:
        return {"path": path, "error": "Could not determine duration"}

    intro_end = min(n_seconds, duration)
    outro_start = max(duration - n_seconds, 0)

    result = {
        "path": path,
        "filename": Path(path).name,
        "total_duration": round(duration, 1),
        "intro": {
            "start": 0.0,
            "end": round(intro_end, 1),
            "duration": round(intro_end, 1),
        },
        "outro": {
            "start": round(outro_start, 1),
            "end": round(duration, 1),
            "duration": round(duration - outro_start, 1),
        },
    }

    # Try to get loudness info via ffprobe for the intro/outro zones
    # (this helps identify talk-over-friendly sections)
    info = _ffprobe_audio_info(path)
    if info:
        result["codec"] = info["codec"]
        result["sample_rate"] = info["sample_rate"]
        result["channels"] = info["channels"]

    return result


@click.command(
    name="intro-outro",
    help=(
        "Report the first and last N seconds of each audio file for\n"
        "talk-over planning. Shows intro/outro time ranges and file\n"
        "codec info. Useful for finding tracks with long instrumental\n"
        "intros/outros suitable for talking over.\n\n"
        "Give it a folder or individual files. --seconds controls the\n"
        "window size (default: 12 seconds)."
    ),
)
@click.argument("source", type=click.Path(exists=True))
@click.option("--seconds", "n_seconds", type=float, default=12.0,
              help="Intro/outro window in seconds (default: 12).")
@click.option("--min-intro", "min_intro", type=float, default=0,
              help="Only show tracks with intro >= this many seconds.")
@click.option("--recursive", "-r", is_flag=True,
              help="Scan subdirectories if SOURCE is a folder.")
def intro_outro_cli(source, n_seconds, min_intro, recursive):
    """Entry point for intro-outro."""
    src = Path(source)
    files: list[Path] = []

    if src.is_file():
        files = [src]
    elif src.is_dir():
        if recursive:
            files = sorted(f for f in src.rglob("*") if f.suffix.lower() in AUDIO_EXTENSIONS)
        else:
            files = sorted(f for f in src.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS)
    else:
        click.echo(f"Error: {source} is not a file or directory.", err=True)
        raise SystemExit(1)

    if not files:
        click.echo("No audio files found.")
        return

    click.echo(f"Intro/Outro Report (window: {n_seconds}s)")
    click.echo("=" * 70)
    click.echo(f"{'File':<40s} {'Total':>6s}  {'Intro':>6s}  {'Outro':>6s}")
    click.echo("-" * 70)

    for f in files:
        info = _get_intro_outro(str(f), n_seconds)
        if "error" in info:
            click.echo(f"  {f.name:<40s}  ERROR: {info['error']}")
            continue
        intro_dur = info["intro"]["duration"]
        if min_intro > 0 and intro_dur < min_intro:
            continue
        total = info["total_duration"]
        outro_dur = info["outro"]["duration"]
        click.echo(f"  {f.name:<40s}  {total:>5.0f}s  {intro_dur:>5.1f}s  {outro_dur:>5.1f}s")

    click.echo("=" * 70)


# ── countdown ───────────────────────────────────────────────────────────

@click.command(
    name="countdown",
    help=(
        "Big countdown timer to a target wall-clock time.\n\n"
        "Give a target time as HH:MM:SS (today) or HH:MM:SS with --tomorrow.\n"
        "The display updates every second. Press Ctrl-C to stop.\n\n"
        "Example: countdown 15:00:00  (count down to 3pm today)"
    ),
)
@click.argument("target", type=str)
@click.option("--tomorrow", is_flag=True,
              help="Count down to tomorrow at the given time.")
def countdown_cli(target, tomorrow):
    """Entry point for countdown."""
    # Parse target time
    try:
        parts = target.strip().split(":")
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            h, m, s = int(parts[0]), int(parts[1]), 0
        else:
            raise ValueError("Expected HH:MM:SS or HH:MM")
    except ValueError:
        click.echo(f"Error: invalid time format. Use HH:MM:SS", err=True)
        raise SystemExit(1)

    now = datetime.now()
    target_dt = now.replace(hour=h, minute=m, second=s, microsecond=0)
    if tomorrow:
        target_dt += timedelta(days=1)
    elif target_dt <= now:
        # Target is in the past today → assume tomorrow
        target_dt += timedelta(days=1)

    try:
        while True:
            now = datetime.now()
            remaining = target_dt - now
            total_secs = remaining.total_seconds()

            if total_secs <= 0:
                click.echo("\033[2J\033[H", nl=False)
                click.echo(_render_countdown(0, target_dt))
                click.echo("\n  *** TIME REACHED ***")
                break

            click.echo("\033[2J\033[H", nl=False)
            click.echo(_render_countdown(int(total_secs), target_dt))
            time.sleep(1)
    except KeyboardInterrupt:
        click.echo("\nCountdown stopped.")


def _render_countdown(remaining_secs: int, target_dt: datetime) -> str:
    """Render a large countdown display."""
    hours = remaining_secs // 3600
    mins = (remaining_secs % 3600) // 60
    secs = remaining_secs % 60

    # Big digits using block characters
    DIGITS = {
        '0': [
            "█████",
            "█   █",
            "█   █",
            "█   █",
            "█████",
        ],
        '1': [
            "  █  ",
            " ██  ",
            "  █  ",
            "  █  ",
            "  █  ",
        ],
        '2': [
            "█████",
            "    █",
            "█████",
            "█    ",
            "█████",
        ],
        '3': [
            "█████",
            "    █",
            "█████",
            "    █",
            "█████",
        ],
        '4': [
            "█   █",
            "█   █",
            "█████",
            "    █",
            "    █",
        ],
        '5': [
            "█████",
            "█    ",
            "█████",
            "    █",
            "█████",
        ],
        '6': [
            "█████",
            "█    ",
            "█████",
            "█   █",
            "█████",
        ],
        '7': [
            "█████",
            "    █",
            "  █  ",
            " █   ",
            " █   ",
        ],
        '8': [
            "█████",
            "█   █",
            "█████",
            "█   █",
            "█████",
        ],
        '9': [
            "█████",
            "█   █",
            "█████",
            "    █",
            "█████",
        ],
        ':': [
            "     ",
            "  █  ",
            "     ",
            "  █  ",
            "     ",
        ],
    }

    time_str = f"{hours:02d}:{mins:02d}:{secs:02d}"

    lines = []
    lines.append("=" * 50)
    lines.append("COUNTDOWN")
    lines.append("=" * 50)
    lines.append(f"  Target: {target_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append()

    # Render big digits
    for row in range(5):
        line = "  "
        for ch in time_str:
            if ch in DIGITS:
                line += DIGITS[ch][row] + " "
            else:
                line += "     "
        lines.append(line)

    lines.append()
    lines.append(f"  Time remaining: {hours}h {mins}m {secs}s")
    lines.append("=" * 50)
    return "\n".join(lines)


# Allow `python -m broadcast.utilities` to work
if __name__ == "__main__":
    import sys
    cmds = {"broadcast-clock": broadcast_clock_cli,
            "intro-outro": intro_outro_cli,
            "countdown": countdown_cli}
    if len(sys.argv) > 1 and sys.argv[1] in cmds:
        cmds[sys.argv[1]]()
    else:
        click.echo("Usage: python -m broadcast.utilities {broadcast-clock|intro-outro|countdown}")