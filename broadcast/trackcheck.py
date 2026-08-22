"""trackcheck — Library hygiene scanner.

Scans audio files and reports problems: missing/corrupt tags, wrong file
extensions, duplicate files (by content hash), tracks over/under duration
limits, missing ReplayGain, and tag/filename conflicts.

With --fix, it fills in EMPTY tags only (artist/title from filename when
missing).  It never overwrites existing tags and never renames files.

Usage:

  trackcheck /music                    # scan and report
  trackcheck /music --fix              # fill empty tags, then report
  trackcheck /music --min 60 --max 600 # flag tracks <1min or >10min
  trackcheck /music --json report.json # write report as JSON
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import click

try:
    from mutagen import File as MutagenFile
    from mutagen.mp3 import MP3, EasyMP3
    from mutagen.flac import FLAC
    from mutagen.oggvorbis import OggVorbis
    from mutagen.mp4 import MP4
    from mutagen.wave import WAVE
    _HAS_MUTAGEN = True
except Exception:  # pragma: no cover
    _HAS_MUTAGEN = False


AUDIO_EXTENSIONS = {
    ".mp3": "mp3",
    ".flac": "flac",
    ".ogg": "ogg",
    ".m4a": "mp4",
    ".aac": "mp4",
    ".wav": "wav",
    ".wma": "wma",
}

# Expected extension → codec mapping for "wrong extension" detection
EXTENSION_TO_FORMAT = {
    ".mp3": "MP3",
    ".flac": "FLAC",
    ".ogg": "OggVorbis",
    ".m4a": "MP4",
    ".aac": "MP4",
    ".wav": "WAVE",
}


# ── helpers ─────────────────────────────────────────────────────────────

def _first(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, (list, tuple)):
        return str(val[0]) if val else None
    return str(val)


def _guess_artist_from_name(stem: str) -> str | None:
    if " - " in stem:
        return stem.split(" - ", 1)[0].strip()
    return None


def _guess_title_from_name(stem: str) -> str | None:
    if " - " in stem:
        return stem.split(" - ", 1)[1].strip()
    return None


def _hash_file(path: str, chunk_size: int = 65536) -> str:
    """Return SHA-256 hex digest of file content."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _open_audio(path: str):
    """Open an audio file with mutagen, return (audio, format_name) or (None, None)."""
    if not _HAS_MUTAGEN:
        return None, None
    ext = Path(path).suffix.lower()
    fmt = EXTENSION_TO_FORMAT.get(ext, "Unknown")
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        return None, None
    return audio, fmt


def _get_tags(audio) -> dict:
    """Extract tags dict from a mutagen audio object."""
    tags = {"artist": None, "title": None, "album": None,
            "duration": 0.0, "replaygain_track_gain": None}
    if audio is None:
        return tags
    if audio.info:
        try:
            tags["duration"] = float(audio.info.length)
        except Exception:
            pass
    if audio.tags:
        t = audio.tags
        tags["artist"] = _first(t.get("artist")) or _first(t.get("TPE1"))
        tags["title"] = _first(t.get("title")) or _first(t.get("TIT2"))
        tags["album"] = _first(t.get("album")) or _first(t.get("TALB"))
        rg = _first(t.get("replaygain_track_gain")) or _first(t.get("TXXX:replaygain_track_gain"))
        if rg is not None:
            tags["replaygain_track_gain"] = rg
    return tags


def _fix_empty_tags(path: str, audio, stem: str) -> list[str]:
    """Fill in empty artist/title tags from the filename. Returns list of actions."""
    actions: list[str] = []
    if audio is None:
        return actions

    # Handle WAV files (ID3 tags) differently from other formats
    is_wav = type(audio).__name__ in ("WAVE", "EasyWave")
    if is_wav:
        # WAV uses ID3 frames
        from mutagen.id3 import TIT2, TPE1
        tags = audio.tags
        if tags is None:
            try:
                audio.add_tags()
                tags = audio.tags
            except Exception:
                return actions
        if tags is None:
            return actions

        artist = _first(tags.get("TPE1")) or _first(tags.get("artist"))
        title = _first(tags.get("TIT2")) or _first(tags.get("title"))
        changed = False

        if not artist:
            guess = _guess_artist_from_name(stem)
            if guess:
                tags.add(TPE1(encoding=3, text=guess))
                actions.append(f"set artist='{guess}'")
                changed = True

        if not title:
            guess = _guess_title_from_name(stem)
            if guess:
                tags.add(TIT2(encoding=3, text=guess))
                actions.append(f"set title='{guess}'")
                changed = True

        if changed:
            try:
                audio.save()
            except Exception as exc:
                actions.append(f"ERROR saving: {exc}")
        return actions

    # Non-WAV formats: use easy-mode string keys
    tags = audio.tags
    if tags is None:
        return actions

    changed = False
    artist = _first(tags.get("artist")) or _first(tags.get("TPE1"))
    title = _first(tags.get("title")) or _first(tags.get("TIT2"))

    if not artist:
        guess = _guess_artist_from_name(stem)
        if guess:
            tags["artist"] = guess
            actions.append(f"set artist='{guess}'")
            changed = True

    if not title:
        guess = _guess_title_from_name(stem)
        if guess:
            tags["title"] = guess
            actions.append(f"set title='{guess}'")
            changed = True

    if changed:
        try:
            audio.save()
        except Exception as exc:
            actions.append(f"ERROR saving: {exc}")

    return actions


# ── scanner ─────────────────────────────────────────────────────────────

def scan_library(folder: str, min_duration: float = 0.0,
                 max_duration: float = 0.0, fix: bool = False) -> dict:
    """Scan *folder* and return a report dict.

    Parameters:
      folder       Root directory to scan recursively.
      min_duration Flag tracks shorter than this (seconds). 0 = no check.
      max_duration Flag tracks longer than this (seconds). 0 = no check.
      fix          If True, fill empty tags from filename (non-destructive).

    Returns a dict with keys: issues, duplicates, stats, fixes.
    """
    root = Path(folder)
    if not root.exists():
        raise FileNotFoundError(f"Folder not found: {folder}")

    issues: list[dict] = []
    fixes: list[dict] = []
    all_files: list[Path] = []
    hashes: dict[str, list[str]] = {}  # hash → [paths]
    total_files = 0
    total_duration = 0.0

    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file():
            continue
        ext = file_path.suffix.lower()
        if ext not in AUDIO_EXTENSIONS:
            continue

        total_files += 1
        all_files.append(file_path)
        fpath = str(file_path.resolve())
        stem = file_path.stem

        # Open with mutagen
        audio = None
        fmt = None
        try:
            if _HAS_MUTAGEN:
                audio = MutagenFile(str(file_path), easy=True)
                fmt = EXTENSION_TO_FORMAT.get(ext, "Unknown")
        except Exception as exc:
            issues.append({
                "file": fpath,
                "issue": "corrupt",
                "detail": f"Could not read file: {exc}",
            })
            continue

        if audio is None:
            issues.append({
                "file": fpath,
                "issue": "unreadable",
                "detail": "mutagen could not open this file (unsupported codec?)",
            })
            continue

        tags = _get_tags(audio)
        total_duration += tags["duration"]

        # Missing tags
        if not tags["artist"]:
            issues.append({
                "file": fpath,
                "issue": "missing_tag",
                "detail": "artist tag is empty",
            })
        if not tags["title"]:
            issues.append({
                "file": fpath,
                "issue": "missing_tag",
                "detail": "title tag is empty",
            })

        # Wrong extension check: mutagen detected different format
        actual_fmt = type(audio).__name__
        if actual_fmt == "EasyMP3":
            actual_fmt = "MP3"
        expected = EXTENSION_TO_FORMAT.get(ext, "")
        if expected and actual_fmt != expected and actual_fmt != "Unknown":
            issues.append({
                "file": fpath,
                "issue": "wrong_extension",
                "detail": f"File has .{ext.lstrip('.')} extension but mutagen reads it as {actual_fmt}",
            })

        # Duration checks
        if min_duration > 0 and tags["duration"] < min_duration and tags["duration"] > 0:
            issues.append({
                "file": fpath,
                "issue": "too_short",
                "detail": f"Duration {tags['duration']:.1f}s < {min_duration}s minimum",
            })
        if max_duration > 0 and tags["duration"] > max_duration:
            issues.append({
                "file": fpath,
                "issue": "too_long",
                "detail": f"Duration {tags['duration']:.1f}s > {max_duration}s maximum",
            })

        # ReplayGain check
        if tags["replaygain_track_gain"] is None:
            issues.append({
                "file": fpath,
                "issue": "missing_replaygain",
                "detail": "No replaygain_track_gain tag found",
            })

        # Tag/filename conflict check
        if tags["artist"] and tags["title"]:
            tagged_label = f"{tags['artist']} - {tags['title']}".lower()
            filename_label = stem.lower()
            # Normalize for comparison: strip extra spaces, brackets
            import re
            tagged_clean = re.sub(r"[^a-z0-9]", "", tagged_label)
            filename_clean = re.sub(r"[^a-z0-9]", "", filename_label)
            # Only flag if they're both substantial and quite different
            if len(tagged_clean) > 3 and len(filename_clean) > 3:
                # Simple similarity: share of common chars
                if tagged_clean != filename_clean:
                    # Check if one is a substring of the other
                    if tagged_clean not in filename_clean and filename_clean not in tagged_clean:
                        issues.append({
                            "file": fpath,
                            "issue": "tag_filename_conflict",
                            "detail": f"Tags say '{tags['artist']} - {tags['title']}' but filename is '{stem}'",
                        })

        # Content hash for duplicate detection
        try:
            file_hash = _hash_file(fpath)
            hashes.setdefault(file_hash, []).append(fpath)
        except Exception:
            pass

        # Fix empty tags
        if fix and audio is not None:
            actions = _fix_empty_tags(fpath, audio, stem)
            if actions:
                fixes.append({"file": fpath, "actions": actions})

    # Find duplicates
    duplicates: list[dict] = []
    for h, paths in hashes.items():
        if len(paths) > 1:
            duplicates.append({
                "hash": h[:12] + "...",
                "files": paths,
            })

    return {
        "scanned": total_files,
        "total_duration": total_duration,
        "issues": issues,
        "duplicates": duplicates,
        "fixes": fixes,
        "stats": {
            "total_files": total_files,
            "total_duration_s": round(total_duration, 1),
            "total_issues": len(issues),
            "duplicate_groups": len(duplicates),
            "duplicate_files": sum(len(d["files"]) for d in duplicates),
            "fixes_applied": len(fixes),
        },
    }


# ── report rendering ────────────────────────────────────────────────────

def render_report(report: dict) -> str:
    """Render the scan report as human-readable text."""
    lines: list[str] = []
    stats = report["stats"]

    lines.append("=" * 60)
    lines.append("TRACKCHECK REPORT")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"Files scanned:     {stats['total_files']}")
    lines.append(f"Total duration:    {stats['total_duration_s']:.0f}s "
                 f"({stats['total_duration_s']/3600:.1f}h)")
    lines.append(f"Issues found:      {stats['total_issues']}")
    lines.append(f"Duplicate groups:  {stats['duplicate_groups']} "
                 f"({stats['duplicate_files']} files)")
    if stats["fixes_applied"] > 0:
        lines.append(f"Fixes applied:     {stats['fixes_applied']}")
    lines.append("")

    # Group issues by type
    issue_types: dict[str, list[dict]] = {}
    for issue in report["issues"]:
        issue_types.setdefault(issue["issue"], []).append(issue)

    ISSUE_LABELS = {
        "missing_tag": "Missing Tags",
        "wrong_extension": "Wrong File Extension",
        "too_short": "Too Short",
        "too_long": "Too Long",
        "missing_replaygain": "Missing ReplayGain",
        "tag_filename_conflict": "Tag/Filename Conflict",
        "corrupt": "Corrupt / Unreadable",
        "unreadable": "Unreadable",
    }

    for issue_type, label in ISSUE_LABELS.items():
        if issue_type not in issue_types:
            continue
        items = issue_types[issue_type]
        lines.append(f"--- {label} ({len(items)}) ---")
        for item in items:
            fname = Path(item["file"]).name
            lines.append(f"  {fname}")
            lines.append(f"    {item['detail']}")
        lines.append("")

    # Duplicates
    if report["duplicates"]:
        lines.append(f"--- Duplicate Files ({len(report['duplicates'])} groups) ---")
        for dup in report["duplicates"]:
            lines.append(f"  Hash {dup['hash']}:")
            for p in dup["files"]:
                lines.append(f"    {p}")
        lines.append("")

    # Fixes
    if report["fixes"]:
        lines.append(f"--- Fixes Applied ({len(report['fixes'])}) ---")
        for fix in report["fixes"]:
            fname = Path(fix["file"]).name
            lines.append(f"  {fname}: {', '.join(fix['actions'])}")
        lines.append("")

    if stats["total_issues"] == 0 and stats["duplicate_groups"] == 0:
        lines.append("No issues found. Library looks clean.")

    lines.append("=" * 60)
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────

@click.command(
    name="trackcheck",
    help=(
        "Scan audio files for library hygiene problems.\n\n"
        "Reports missing tags, wrong extensions, duplicates, duration\n"
        "outliers, missing ReplayGain, and tag/filename conflicts.\n\n"
        "Use --fix to fill EMPTY tags from the filename (artist/title).\n"
        "This never overwrites existing tags and never renames files."
    ),
)
@click.argument("folder", type=click.Path(exists=False, file_okay=False, dir_okay=True))
@click.option("--fix", is_flag=True,
              help="Fill empty artist/title tags from filename. Never overwrites existing tags.")
@click.option("--min", "min_duration", type=float, default=0,
              help="Flag tracks shorter than this (seconds). Default: no check.")
@click.option("--max", "max_duration", type=float, default=0,
              help="Flag tracks longer than this (seconds). Default: no check.")
@click.option("--json", "json_output", default=None,
              type=click.Path(dir_okay=False),
              help="Write the full report as JSON to this path.")
@click.option("--quiet", is_flag=True,
              help="Suppress the human-readable report (use with --json).")
def cli(folder, fix, min_duration, max_duration, json_output, quiet):
    """Entry point for the trackcheck command."""
    if not Path(folder).exists():
        click.echo(f"Error: folder not found: {folder}", err=True)
        raise SystemExit(1)

    try:
        report = scan_library(folder, min_duration=min_duration,
                              max_duration=max_duration, fix=fix)
    except Exception as exc:
        click.echo(f"Error scanning library: {exc}", err=True)
        raise SystemExit(1)

    if json_output:
        with open(json_output, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        click.echo(f"Report written to {json_output}")

    if not quiet:
        click.echo(render_report(report))

    # Exit code: 0 if clean, 1 if issues found
    if report["stats"]["total_issues"] > 0 or report["stats"]["duplicate_groups"] > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    cli()