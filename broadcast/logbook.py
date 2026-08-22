"""logbook — Air-check / playlist logging.

Records played-track events to a JSON log and provides reporting:
  logbook record    Append a play event
  logbook today      Print today's log
  logbook export     Export to CSV for reporting
  logbook stats      Show tracks played per hour (rotation compliance)

The log format is documented in broadcast/formats.md.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, date
from pathlib import Path

import click

try:
    from mutagen import File as MutagenFile
    _HAS_MUTAGEN = True
except Exception:  # pragma: no cover
    _HAS_MUTAGEN = False

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wav", ".wma"}


# ── helpers ─────────────────────────────────────────────────────────────

def _first(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, (list, tuple)):
        return str(val[0]) if val else None
    return str(val)


def _load_log(path: str) -> list[dict]:
    """Load the logbook JSON file (returns [] if it doesn't exist)."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_log(path: str, entries: list[dict]) -> None:
    """Save the full logbook."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def _get_tags_from_file(path: str) -> dict:
    """Extract tags and duration from an audio file for logging."""
    info = {"artist": None, "title": None, "duration": 0.0}
    if not _HAS_MUTAGEN:
        return info
    try:
        audio = MutagenFile(path, easy=True)
        if audio is not None:
            if audio.info:
                info["duration"] = float(audio.info.length)
            if audio.tags:
                info["artist"] = _first(audio.tags.get("artist")) or _first(audio.tags.get("TPE1"))
                info["title"] = _first(audio.tags.get("title")) or _first(audio.tags.get("TIT2"))
    except Exception:
        pass
    return info


def _guess_from_filename(stem: str) -> tuple[str | None, str | None]:
    """Guess artist and title from 'Artist - Title' filename."""
    if " - " in stem:
        parts = stem.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return None, stem


# ── stats ───────────────────────────────────────────────────────────────

def compute_hourly_stats(entries: list[dict], target_date: str | None = None) -> dict:
    """Compute tracks-played-per-hour for a given date (or today).

    Returns a dict with:
      - hours: {0: count, 1: count, ... 23: count}
      - total: total tracks that day
      - by_category: {category: count}
      - by_source: {source: count}
    """
    if target_date is None:
        target_date = date.today().isoformat()

    hours: dict[int, int] = {i: 0 for i in range(24)}
    by_category: dict[str, int] = {}
    by_source: dict[str, int] = {}
    total = 0

    for entry in entries:
        ts = entry.get("timestamp", "")
        if not ts.startswith(target_date):
            continue
        total += 1
        try:
            hour = int(ts[11:13])
        except (ValueError, IndexError):
            continue
        hours[hour] = hours.get(hour, 0) + 1
        cat = entry.get("category") or "(none)"
        by_category[cat] = by_category.get(cat, 0) + 1
        src = entry.get("source") or "(unknown)"
        by_source[src] = by_source.get(src, 0) + 1

    return {
        "date": target_date,
        "hours": hours,
        "total": total,
        "by_category": by_category,
        "by_source": by_source,
    }


def render_heatmap(stats: dict) -> str:
    """Render the hourly stats as an ASCII heat map."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f"LOGBOOK STATS — {stats['date']}")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"Total tracks played: {stats['total']}")
    lines.append("")

    # Heat map
    max_count = max(stats["hours"].values()) if stats["hours"] else 0
    lines.append("Hourly heat map:")
    lines.append("")
    for hour in range(24):
        count = stats["hours"].get(hour, 0)
        bar_len = count * 2
        bar = "█" * bar_len
        label = f"  {hour:02d}:00 | {count:3d} {bar}"
        lines.append(label)

    lines.append("")

    # Category breakdown
    if stats["by_category"]:
        lines.append("By category:")
        for cat in sorted(stats["by_category"].keys()):
            count = stats["by_category"][cat]
            lines.append(f"  {cat:10s}  {count:3d}")
        lines.append("")

    # Source breakdown
    if stats["by_source"]:
        lines.append("By source:")
        for src in sorted(stats["by_source"].keys()):
            count = stats["by_source"][src]
            lines.append(f"  {src:12s}  {count:3d}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


# ── CSV export ──────────────────────────────────────────────────────────

def export_csv(entries: list[dict], path: str, target_date: str | None = None) -> int:
    """Export entries to CSV. Returns number of rows written."""
    cols = ["timestamp", "artist", "title", "category", "duration",
            "source", "path"]
    count = 0
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        for entry in entries:
            if target_date and not entry.get("timestamp", "").startswith(target_date):
                continue
            row = {col: entry.get(col, "") for col in cols}
            writer.writerow(row)
            count += 1
    return count


# ── CLI ─────────────────────────────────────────────────────────────────

@click.group(
    name="logbook",
    help=(
        "Air-check / playlist logging.\n\n"
        "Record played tracks, view today's log, export to CSV, and check\n"
        "rotation compliance with hourly stats."
    ),
)
@click.option("--file", "log_file", default="logbook.json",
              type=click.Path(dir_okay=False),
              help="Path to the logbook JSON file (default: logbook.json).")
@click.pass_context
def cli(ctx, log_file):
    """Entry point for the logbook command group."""
    ctx.ensure_object(dict)
    ctx.obj["file"] = log_file


@cli.command(help="Record a played track.")
@click.argument("track_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--category", default=None, help="Rotation category (A, B, C, NEW, etc.).")
@click.option("--source", default="manual",
              help="What triggered the play (manual, playlistgen, schedule, studio).")
@click.option("--artist", default=None, help="Override the artist tag.")
@click.option("--title", default=None, help="Override the title tag.")
@click.option("--at", "at_time", default=None,
              help="Record at a specific timestamp (YYYY-MM-DD HH:MM:SS) instead of now.")
@click.pass_context
def record(ctx, track_path, category, source, artist, title, at_time):
    log_file = ctx.obj["file"]

    # Get tags from file
    tags = _get_tags_from_file(track_path)
    artist = artist or tags["artist"]
    title = title or tags["title"]
    duration = tags["duration"]

    # Fall back to filename if no tags
    if not artist or not title:
        stem = Path(track_path).stem
        g_artist, g_title = _guess_from_filename(stem)
        if not artist:
            artist = g_artist
        if not title:
            title = g_title

    # Timestamp
    if at_time:
        try:
            ts = datetime.strptime(at_time, "%Y-%m-%d %H:%M:%S")
            timestamp = ts.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            click.echo(f"Error: invalid timestamp format. Use YYYY-MM-DD HH:MM:SS", err=True)
            raise SystemExit(1)
    else:
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    entry = {
        "timestamp": timestamp,
        "path": str(Path(track_path).resolve()),
        "artist": artist,
        "title": title,
        "category": category,
        "duration": round(duration, 1),
        "source": source,
    }

    entries = _load_log(log_file)
    entries.append(entry)
    _save_log(log_file, entries)

    click.echo(f"Recorded: {artist} - {title} at {timestamp}")


@cli.command(help="Print today's log (or a specific date with --date).")
@click.option("--date", "target_date", default=None,
              help="Show a specific date (YYYY-MM-DD). Default: today.")
@click.pass_context
def today(ctx, target_date):
    log_file = ctx.obj["file"]
    entries = _load_log(log_file)
    if not entries:
        click.echo("(log is empty)")
        return

    if target_date is None:
        target_date = date.today().isoformat()

    day_entries = [e for e in entries if e.get("timestamp", "").startswith(target_date)]
    if not day_entries:
        click.echo(f"No entries for {target_date}")
        return

    click.echo(f"Log for {target_date} ({len(day_entries)} tracks)")
    click.echo("-" * 60)
    for e in day_entries:
        ts = e.get("timestamp", "")
        time_part = ts[11:19] if len(ts) >= 19 else ts
        artist = e.get("artist") or "?"
        title = e.get("title") or "?"
        cat = e.get("category") or "-"
        dur = e.get("duration", 0)
        click.echo(f"  {time_part}  [{cat:4s}]  {artist} - {title}  ({dur:.0f}s)")


@cli.command(name="export", help="Export the log to CSV.")
@click.option("--csv", "csv_path", required=True,
              type=click.Path(dir_okay=False),
              help="Output CSV file path.")
@click.option("--date", "target_date", default=None,
              help="Export only a specific date (YYYY-MM-DD). Default: all entries.")
@click.pass_context
def export(ctx, csv_path, target_date):
    log_file = ctx.obj["file"]
    entries = _load_log(log_file)
    if not entries:
        click.echo("(log is empty)")
        return
    count = export_csv(entries, csv_path, target_date)
    scope = f" for {target_date}" if target_date else ""
    click.echo(f"Exported {count} entries{scope} to {csv_path}")


@cli.command(help="Show tracks played per hour (rotation compliance heat map).")
@click.option("--hour", "show_hour", is_flag=True,
              help="Show per-hour heat map (default view).")
@click.option("--date", "target_date", default=None,
              help="Stats for a specific date (YYYY-MM-DD). Default: today.")
@click.pass_context
def stats(ctx, show_hour, target_date):
    log_file = ctx.obj["file"]
    entries = _load_log(log_file)
    if not entries:
        click.echo("(log is empty)")
        return

    if target_date is None:
        target_date = date.today().isoformat()

    s = compute_hourly_stats(entries, target_date)
    click.echo(render_heatmap(s))


if __name__ == "__main__":
    cli()