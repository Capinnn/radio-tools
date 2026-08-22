"""schedule — Dayparting / weekly program clock planner.

Defines a weekly grid of dayparts (Monday 06-10 = "Morning", etc.), each
mapped to a rotation file or a fixed playlist.  Provides three commands:

  schedule init   Create a default schedule.json
  schedule show   Print the program clock as ASCII art
  schedule next   Print what should be playing right now and what's next

The schedule file format is documented in broadcast/formats.md.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import click

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
BLOCK_HOURS = [0, 3, 6, 9, 12, 15, 18, 21]  # 8 blocks of 3 hours

DEFAULT_DAYPARTS = {
    "Morning":   {"start": "06:00", "end": "10:00", "rotation": "rotation.json"},
    "Middrive":  {"start": "10:00", "end": "15:00", "rotation": "rotation.json"},
    "Evening":   {"start": "15:00", "end": "20:00", "rotation": "rotation.json"},
    "Overnight": {"start": "20:00", "end": "06:00", "rotation": "rotation.json"},
}

DEFAULT_GRID = {
    "mon": ["Overnight", "Overnight", "Morning", "Middrive", "Middrive", "Evening", "Evening", "Overnight"],
    "tue": ["Overnight", "Overnight", "Morning", "Middrive", "Middrive", "Evening", "Evening", "Overnight"],
    "wed": ["Overnight", "Overnight", "Morning", "Middrive", "Middrive", "Evening", "Evening", "Overnight"],
    "thu": ["Overnight", "Overnight", "Morning", "Middrive", "Middrive", "Evening", "Evening", "Overnight"],
    "fri": ["Overnight", "Overnight", "Morning", "Middrive", "Middrive", "Evening", "Evening", "Overnight"],
    "sat": ["Overnight", "Overnight", "Morning", "Middrive", "Middrive", "Evening", "Evening", "Overnight"],
    "sun": ["Overnight", "Overnight", "Morning", "Middrive", "Middrive", "Evening", "Evening", "Overnight"],
}


# ── file I/O ────────────────────────────────────────────────────────────

def load_schedule(path: str) -> dict:
    """Load a schedule JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_schedule(path: str, data: dict) -> None:
    """Save a schedule JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── grid logic ──────────────────────────────────────────────────────────

def _day_to_index(day: str) -> int:
    """Convert a 3-letter day code to a 0-based index (mon=0)."""
    d = day.lower()[:3]
    if d in DAYS:
        return DAYS.index(d)
    raise ValueError(f"Unknown day: {day}")


def _hour_to_block(hour: int) -> int:
    """Convert a 0-23 hour to a block index (0-7)."""
    return min(hour // 3, 7)


def get_daypart_for(schedule: dict, day: str, hour: int) -> str | None:
    """Return the daypart name active for *day* at *hour*."""
    grid = schedule.get("grid", {})
    day_key = day.lower()[:3]
    if day_key not in grid:
        return None
    block = _hour_to_block(hour)
    return grid[day_key][block]


def get_current_daypart(schedule: dict, now: datetime | None = None) -> tuple[str | None, dict | None]:
    """Return (daypart_name, daypart_config) for the current moment."""
    if now is None:
        now = datetime.now()
    day = DAYS[now.weekday()]
    hour = now.hour
    name = get_daypart_for(schedule, day, hour)
    if name is None:
        return None, None
    dp = schedule.get("dayparts", {}).get(name)
    return name, dp


def get_next_daypart(schedule: dict, now: datetime | None = None) -> tuple[str | None, datetime, dict | None]:
    """Return (daypart_name, start_time, config) for the next daypart change."""
    if now is None:
        now = datetime.now()
    # Search forward hour by hour until the daypart changes
    current_name, _ = get_current_daypart(schedule, now)
    for offset in range(1, 169):  # up to a week
        future = now + timedelta(hours=offset)
        future = future.replace(minute=0, second=0, microsecond=0)
        name, _ = get_current_daypart(schedule, future)
        if name != current_name and name is not None:
            return name, future, schedule.get("dayparts", {}).get(name)
    return None, now, None


def _parse_time(t: str) -> tuple[int, int]:
    """Parse 'HH:MM' into (hours, minutes)."""
    h, m = t.split(":")
    return int(h), int(m)


# ── ASCII art rendering ─────────────────────────────────────────────────

def render_ascii_grid(schedule: dict) -> str:
    """Render the weekly grid as ASCII art."""
    grid = schedule.get("grid", {})
    dayparts = schedule.get("dayparts", {})

    # Column headers: time blocks
    header = "      |"
    for i, bh in enumerate(BLOCK_HOURS):
        header += f" {bh:02d}-{bh+3:02d} "
        if i < len(BLOCK_HOURS) - 1:
            header += "|"
    header += " |"

    # Separator line
    sep = "------+" + "+".join(["------"] * 8) + "+"

    # Also build a legend
    lines = [header, sep]
    for day_key in DAYS:
        label = DAY_LABELS[DAYS.index(day_key)]
        row = f" {label}  |"
        blocks = grid.get(day_key, ["?"] * 8)
        for i, name in enumerate(blocks):
            # Abbreviate daypart names to fit in 6 chars
            short = _abbreviate(name, 6)
            row += f" {short:^4s} "
            if i < 7:
                row += "|"
        row += " |"
        lines.append(row)
        lines.append(sep)

    # Legend
    lines.append("")
    lines.append("Legend:")
    for name, config in dayparts.items():
        start = config.get("start", "??")
        end = config.get("end", "??")
        rot = config.get("rotation", "")
        pl = config.get("playlist", "")
        detail = f"rotation: {rot}" if rot else f"playlist: {pl}"
        lines.append(f"  {name:12s}  {start}-{end}  ({detail})")

    return "\n".join(lines)


def _abbreviate(name: str, width: int) -> str:
    """Abbreviate a daypart name to fit within *width* characters."""
    if len(name) <= width:
        return name
    # Try taking first width chars
    return name[:width]


# ── CLI ─────────────────────────────────────────────────────────────────

@click.group(
    name="schedule",
    help=(
        "Dayparting / weekly program clock planner.\n\n"
        "Define a weekly grid of dayparts and rotations, show the program\n"
        "clock as ASCII art, and check what should be playing now."
    ),
)
@click.option("--file", "schedule_file", default="schedule.json",
              type=click.Path(dir_okay=False),
              help="Path to schedule.json (default: schedule.json in cwd).")
@click.pass_context
def cli(ctx, schedule_file):
    """Entry point for the schedule command group."""
    ctx.ensure_object(dict)
    ctx.obj["file"] = schedule_file


@cli.command(help="Create a default schedule.json in the current directory.")
@click.option("--force", is_flag=True, help="Overwrite if the file already exists.")
@click.pass_context
def init(ctx, force):
    path = Path(ctx.obj["file"])
    if path.exists() and not force:
        click.echo(f"Error: {path} already exists. Use --force to overwrite.", err=True)
        raise SystemExit(1)
    data = {"dayparts": DEFAULT_DAYPARTS, "grid": DEFAULT_GRID}
    save_schedule(str(path), data)
    click.echo(f"Created {path} with default dayparts and grid.")
    click.echo("Edit it to match your station's program clock.")


@cli.command(help="Print the program clock as ASCII art.")
@click.pass_context
def show(ctx):
    path = ctx.obj["file"]
    if not Path(path).exists():
        click.echo(f"Error: schedule file not found: {path}", err=True)
        click.echo("Run 'schedule init' first.", err=True)
        raise SystemExit(1)
    try:
        sched = load_schedule(path)
    except (json.JSONDecodeError, OSError) as exc:
        click.echo(f"Error loading schedule: {exc}", err=True)
        raise SystemExit(1)
    click.echo(render_ascii_grid(sched))


@cli.command(help="Print what should be playing now and what's next.")
@click.option("--at", "at_time", default=None,
              help="Check a specific time (format: YYYY-MM-DD HH:MM).")
@click.pass_context
def next(ctx, at_time):
    path = ctx.obj["file"]
    if not Path(path).exists():
        click.echo(f"Error: schedule file not found: {path}", err=True)
        click.echo("Run 'schedule init' first.", err=True)
        raise SystemExit(1)
    try:
        sched = load_schedule(path)
    except (json.JSONDecodeError, OSError) as exc:
        click.echo(f"Error loading schedule: {exc}", err=True)
        raise SystemExit(1)

    if at_time:
        try:
            now = datetime.strptime(at_time, "%Y-%m-%d %H:%M")
        except ValueError:
            click.echo(f"Error: invalid time format. Use YYYY-MM-DD HH:MM", err=True)
            raise SystemExit(1)
    else:
        now = datetime.now()

    name, config = get_current_daypart(sched, now)
    next_name, next_time, next_config = get_next_daypart(sched, now)

    click.echo(f"Station clock: {now.strftime('%Y-%m-%d %H:%M:%S')} "
               f"({DAY_LABELS[now.weekday()]})")
    click.echo()

    if name:
        click.echo(f"NOW PLAYING: {name}")
        if config:
            start = config.get("start", "??")
            end = config.get("end", "??")
            click.echo(f"  Hours: {start} - {end}")
            if config.get("rotation"):
                click.echo(f"  Rotation: {config['rotation']}")
            if config.get("playlist"):
                click.echo(f"  Playlist: {config['playlist']}")
    else:
        click.echo("NOW PLAYING: (no daypart scheduled)")

    click.echo()

    if next_name:
        until = next_time - now
        hours_until = until.total_seconds() / 3600
        click.echo(f"NEXT: {next_name} at {next_time.strftime('%H:%M')} "
                   f"(in {hours_until:.1f}h)")
    else:
        click.echo("NEXT: (no upcoming daypart found)")


if __name__ == "__main__":
    cli()