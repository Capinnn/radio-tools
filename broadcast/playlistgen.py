"""playlistgen — Build broadcast-ready playlists from a music library.

Reads a folder of audio files (or a pre-built library index) and applies
rotation rules: category spins-per-hour targets, artist/title/category
gaps, and optional daypart weight adjustments.  Emits an M3U file plus a
JSON sidecar with full metadata.

Usage examples:

  # Generate a 1-hour playlist from a music folder
  playlistgen /music --rotation rotation.json -o playlist.m3u

  # 30-minute slot starting at 14:00 with a seed for reproducibility
  playlistgen /music --rotation rotation.json --hour 14 --slot 30min \\
      --seed 42 -o slot.m3u

  # Use a pre-built library index instead of scanning a folder
  playlistgen --library library.json --rotation rotation.json -o pl.m3u
"""

from __future__ import annotations

import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path

import click

try:
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3
    from mutagen.mp3 import MP3
    from mutagen.oggvorbis import OggVorbis
    _HAS_MUTAGEN = True
except Exception:  # pragma: no cover
    _HAS_MUTAGEN = False


# ── helpers ─────────────────────────────────────────────────────────────

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wav", ".wma"}


def _detect_category(path: str, artist: str, title: str) -> str | None:
    """Guess a category from the filename/folder structure.

    Looks for a parent folder whose name matches a known category code
    (A, B, C, NEW, etc.) or a bracket prefix like [A] in the filename.
    Returns None if no category can be inferred.
    """
    p = Path(path)
    # Check parent folder names
    for parent in p.parts[:-1]:
        upper = parent.strip().upper()
        if upper in {"A", "B", "C", "D", "NEW", "GOLD", "CURRENT"}:
            return upper
    # Check bracket prefix in filename, e.g. "[A] Artist - Title.mp3"
    stem = p.stem
    if stem.startswith("[") and "]" in stem:
        code = stem[1:stem.index("]")].strip().upper()
        if code:
            return code
    return None


def _get_duration(path: str) -> float:
    """Return track duration in seconds (0.0 if undetermined)."""
    if not _HAS_MUTAGEN:
        return 0.0
    try:
        audio = MutagenFile(path, easy=True)
        if audio is not None and audio.info is not None:
            return float(audio.info.length)
    except Exception:
        pass
    return 0.0


def _get_tags(path: str) -> dict:
    """Extract artist, title, album, replaygain from a file."""
    tags: dict = {"artist": None, "title": None, "album": None,
                  "duration": 0.0, "replaygain_track_gain": None}
    if not _HAS_MUTAGEN:
        return tags
    try:
        audio = MutagenFile(path, easy=True)
        if audio is None:
            return tags
        tags["duration"] = float(audio.info.length) if audio.info else 0.0
        if audio.tags:
            t = audio.tags
            tags["artist"] = _first(t.get("artist")) or _first(t.get("TPE1"))
            tags["title"] = _first(t.get("title")) or _first(t.get("TIT2"))
            tags["album"] = _first(t.get("album")) or _first(t.get("TALB"))
            rg = _first(t.get("replaygain_track_gain")) or _first(t.get("TXXX:replaygain_track_gain"))
            if rg is not None:
                tags["replaygain_track_gain"] = rg
    except Exception:
        pass
    return tags


def _first(val) -> str | None:
    """Return the first element of a list-like, or the value itself."""
    if val is None:
        return None
    if isinstance(val, (list, tuple)):
        return str(val[0]) if val else None
    return str(val)


def scan_folder(folder: str) -> list[dict]:
    """Walk *folder* and return a list of track dicts (the library index)."""
    tracks: list[dict] = []
    root = Path(folder)
    if not root.exists():
        raise FileNotFoundError(f"Folder not found: {folder}")
    for file_path in sorted(root.rglob("*")):
        if file_path.suffix.lower() in AUDIO_EXTENSIONS:
            tags = _get_tags(str(file_path))
            track = {
                "path": str(file_path.resolve()),
                "artist": tags["artist"] or _guess_artist_from_name(file_path.stem),
                "title": tags["title"] or _guess_title_from_name(file_path.stem),
                "album": tags["album"],
                "duration": tags["duration"],
                "category": _detect_category(str(file_path), tags["artist"] or "", tags["title"] or ""),
                "replaygain_track_gain": tags["replaygain_track_gain"],
            }
            tracks.append(track)
    return tracks


def _guess_artist_from_name(stem: str) -> str:
    """Guess artist from 'Artist - Title' filename convention."""
    if " - " in stem:
        return stem.split(" - ", 1)[0].strip()
    return stem.strip()


def _guess_title_from_name(stem: str) -> str:
    """Guess title from 'Artist - Title' filename convention."""
    if " - " in stem:
        return stem.split(" - ", 1)[1].strip()
    return stem.strip()


def load_library(path: str) -> list[dict]:
    """Load a library index from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_rotation(path: str) -> dict:
    """Load a rotation config from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── rotation engine ─────────────────────────────────────────────────────

class RotationEngine:
    """Apply rotation rules to pick an ordered sequence of tracks.

    The engine uses a deterministic RNG seeded from --seed so the same
    inputs always produce the same playlist.
    """

    def __init__(self, tracks: list[dict], rotation: dict, seed: int = 0,
                 daypart: str | None = None):
        self.tracks = tracks
        self.rotation = rotation
        self.rng = random.Random(seed)
        self.daypart = daypart
        self.rules = rotation.get("rules", {})
        self.artist_gap = int(self.rules.get("artist_gap", 2))
        self.title_gap = int(self.rules.get("title_gap", 1))
        self.category_gap = int(self.rules.get("category_gap", 1))
        self.categories = rotation.get("categories", {})
        self.weights = self._get_weights(daypart)

    def _get_weights(self, daypart: str | None) -> dict[str, float]:
        """Return the weight table for the given daypart, or defaults."""
        if daypart and daypart in self.rotation.get("dayparts", {}):
            return self.rotation["dayparts"][daypart].get("weights", {})
        return {}

    def _category_weight(self, category: str | None) -> float:
        """Weighted multiplier for a category in the current daypart."""
        if category and category in self.weights:
            return float(self.weights[category])
        return 1.0

    def _sph(self, category: str | None) -> float:
        """Target spins per hour for a category, adjusted by daypart weight."""
        if category and category in self.categories:
            base = float(self.categories[category].get("sph", 0))
            return base * self._category_weight(category)
        return 0.0

    def _is_valid(self, track: dict, playlist: list[dict],
                  remaining_indices: set[int]) -> bool:
        """Check whether *track* satisfies all gap rules relative to *playlist*."""
        if not playlist:
            return True
        n = len(playlist)

        # Artist gap
        if self.artist_gap > 0 and track.get("artist"):
            artist = track["artist"].lower()
            lookback = min(self.artist_gap, n)
            for i in range(n - lookback, n):
                if playlist[i].get("artist", "").lower() == artist:
                    return False

        # Title gap
        if self.title_gap > 0 and track.get("title"):
            title = track["title"].lower()
            lookback = min(self.title_gap, n)
            for i in range(n - lookback, n):
                if playlist[i].get("title", "").lower() == title:
                    return False

        # Category gap (prevent back-to-back same category)
        if self.category_gap > 0 and track.get("category"):
            cat = track["category"]
            lookback = min(self.category_gap, n)
            for i in range(n - lookback, n):
                if playlist[i].get("category") == cat:
                    return False

        return True

    def _score(self, track: dict, playlist: list[dict]) -> float:
        """Score a candidate track: higher sph categories get picked more often.

        Uses a weighted random selection: score = sph * random().
        Tracks that haven't been played yet get a small boost.
        """
        sph = self._sph(track.get("category"))
        # Uncategorised tracks get a small default so they're not invisible
        if sph == 0:
            sph = 0.5
        return sph * (1.0 + self.rng.random())

    def generate(self, target_duration: float = 3600.0) -> list[dict]:
        """Pick tracks until total duration reaches *target_duration* seconds.

        Returns the ordered list of selected tracks.
        """
        if not self.tracks:
            return []

        playlist: list[dict] = []
        used: set[int] = set()
        total = 0.0
        max_tracks = len(self.tracks) * 3  # safety valve

        while total < target_duration and len(playlist) < max_tracks:
            candidates: list[tuple[float, int]] = []
            for i, track in enumerate(self.tracks):
                if i in used and len(used) >= len(self.tracks):
                    # All tracks used — reset to allow repeats
                    pass
                if not self._is_valid(track, playlist, used):
                    continue
                score = self._score(track, playlist)
                candidates.append((score, i))

            if not candidates:
                # No valid candidates (all filtered by gap rules).
                # Relax: pick any unused track, or if all used, pick best-scored.
                for i, track in enumerate(self.tracks):
                    if i not in used:
                        candidates.append((self._score(track, playlist), i))
                if not candidates:
                    # Every track used and all filtered — allow repeats
                    used.clear()
                    for i, track in enumerate(self.tracks):
                        candidates.append((self._score(track, playlist), i))

            if not candidates:
                break

            # Pick the highest-scored candidate
            candidates.sort(key=lambda c: c[0], reverse=True)
            _, best_idx = candidates[0]
            chosen = dict(self.tracks[best_idx])
            playlist.append(chosen)
            used.add(best_idx)
            total += chosen.get("duration", 0) or 0.0

            # If track has 0 duration we can't measure progress — guard
            if chosen.get("duration", 0) == 0 and len(playlist) >= len(self.tracks):
                break

        return playlist


# ── output ──────────────────────────────────────────────────────────────

def write_m3u(tracks: list[dict], output_path: str) -> None:
    """Write an M3U playlist file."""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for track in tracks:
            duration = int(track.get("duration", 0) or 0)
            artist = track.get("artist", "")
            title = track.get("title", "")
            label = f"{artist} - {title}" if artist and title else track.get("path", "")
            f.write(f"#EXTINF:{duration},{label}\n")
            f.write(f"{track['path']}\n")


def write_json_sidecar(tracks: list[dict], output_path: str,
                       seed: int, daypart: str | None,
                       target_duration: float) -> None:
    """Write the JSON sidecar next to the M3U file."""
    sidecar = Path(output_path).with_suffix(".json")
    total = sum(t.get("duration", 0) or 0.0 for t in tracks)
    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "seed": seed,
        "daypart": daypart,
        "target_duration": target_duration,
        "actual_duration": total,
        "tracks": [
            {
                "position": i + 1,
                "path": t["path"],
                "artist": t.get("artist"),
                "title": t.get("title"),
                "category": t.get("category"),
                "duration": t.get("duration", 0),
            }
            for i, t in enumerate(tracks)
        ],
    }
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── CLI ─────────────────────────────────────────────────────────────────

def _parse_slot(slot: str) -> int:
    """Parse a slot string like '30min' or '1h' into seconds."""
    slot = slot.strip().lower()
    if slot.endswith("min"):
        return int(slot[:-3]) * 60
    if slot.endswith("h"):
        return int(slot[:-1]) * 3600
    if slot.endswith("s"):
        return int(slot[:-1])
    # bare number → minutes
    try:
        return int(slot) * 60
    except ValueError:
        raise click.BadParameter(f"Could not parse slot duration: {slot}")


@click.command(
    name="playlistgen",
    help=(
        "Build a broadcast-ready playlist from a music library.\n\n"
        "Give it a folder of audio files (or --library JSON index) and a\n"
        "rotation config, and it picks tracks using category spins-per-hour,\n"
        "artist/title gap rules, and optional daypart weights.\n\n"
        "Output is an M3U file plus a .json sidecar with full metadata."
    ),
)
@click.argument("source", required=False,
                type=click.Path(exists=False, file_okay=False, dir_okay=True,
                                path_type=str))
@click.option("--library", "library_path", default=None,
              type=click.Path(exists=True, dir_okay=False),
              help="Path to a pre-built library index (JSON) instead of scanning a folder.")
@click.option("--rotation", "rotation_path", default=None,
              type=click.Path(exists=False, dir_okay=False),
              help="Path to rotation config JSON (categories, rules, dayparts).")
@click.option("-o", "--output", default="playlist.m3u",
              type=click.Path(dir_okay=False),
              help="Output M3U file path. A .json sidecar is written next to it.")
@click.option("--hour", type=int, default=None,
              help="Program-clock hour (0-23). Implies a slot starting at HH:00.")
@click.option("--slot", default="1h",
              help="Slot duration: '30min', '1h', '2h', etc. Default: 1 hour.")
@click.option("--daypart", default=None,
              help="Daypart name to apply weight overrides (e.g. 'Morning').")
@click.option("--seed", default=0, type=int,
              help="Random seed for deterministic output. Same seed = same playlist.")
@click.option("--scan", is_flag=True,
              help="Force folder scan even if --library is given (refresh tags).")
@click.option("--dump-library", is_flag=True,
              help="Scan the folder, write library.json, and exit (no playlist).")
def cli(source, library_path, rotation_path, output, hour, slot,
        daypart, seed, scan, dump_library):
    """Entry point for the playlistgen command."""
    # Validate inputs
    if not source and not library_path:
        click.echo("Error: provide a folder path or --library FILE.", err=True)
        raise SystemExit(1)

    if not rotation_path and not dump_library:
        click.echo("Error: --rotation FILE is required to build a playlist.", err=True)
        raise SystemExit(1)

    if rotation_path and not Path(rotation_path).exists() and not dump_library:
        click.echo(f"Error: rotation file not found: {rotation_path}", err=True)
        raise SystemExit(1)

    # Load or scan the library
    if library_path and not scan:
        try:
            tracks = load_library(library_path)
        except (json.JSONDecodeError, OSError) as exc:
            click.echo(f"Error loading library: {exc}", err=True)
            raise SystemExit(1)
    elif source:
        if not Path(source).exists():
            click.echo(f"Error: folder not found: {source}", err=True)
            raise SystemExit(1)
        try:
            tracks = scan_folder(source)
        except Exception as exc:
            click.echo(f"Error scanning folder: {exc}", err=True)
            raise SystemExit(1)
    else:
        click.echo("Error: no folder or library to work with.", err=True)
        raise SystemExit(1)

    if not tracks:
        click.echo("No audio files found.", err=True)
        raise SystemExit(1)

    # --dump-library: write index and exit
    if dump_library:
        out = Path("library.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(tracks, f, indent=2, ensure_ascii=False)
        click.echo(f"Wrote {len(tracks)} tracks to {out}")
        return

    # Load rotation
    try:
        rotation = load_rotation(rotation_path)
    except (json.JSONDecodeError, OSError) as exc:
        click.echo(f"Error loading rotation config: {exc}", err=True)
        raise SystemExit(1)

    # Determine target duration
    target = _parse_slot(slot)

    # If --hour is given and no --daypart, try to infer daypart from rotation
    if hour is not None and not daypart:
        daypart = _infer_daypart(rotation, hour)

    # Generate
    engine = RotationEngine(tracks, rotation, seed=seed, daypart=daypart)
    playlist = engine.generate(target_duration=target)

    if not playlist:
        click.echo("Error: could not generate a playlist (check rotation config and library).", err=True)
        raise SystemExit(1)

    # Write output
    write_m3u(playlist, output)
    write_json_sidecar(playlist, output, seed=seed, daypart=daypart,
                       target_duration=target)

    total = sum(t.get("duration", 0) or 0.0 for t in playlist)
    click.echo(f"Generated playlist: {len(playlist)} tracks, "
               f"{total:.0f}s / {target:.0f}s target")
    click.echo(f"  M3U:  {output}")
    click.echo(f"  JSON: {Path(output).with_suffix('.json')}")
    if daypart:
        click.echo(f"  Daypart: {daypart}")
    click.echo(f"  Seed: {seed}")


def _infer_daypart(rotation: dict, hour: int) -> str | None:
    """Try to infer a daypart name from the rotation config given an hour."""
    dayparts = rotation.get("dayparts", {})
    for name, config in dayparts.items():
        start = config.get("start", "00:00")
        end = config.get("end", "24:00")
        sh = int(start.split(":")[0])
        eh = int(end.split(":")[0])
        if sh <= eh:
            if sh <= hour < eh:
                return name
        else:
            # Overnight wrap (e.g. 20:00–06:00)
            if hour >= sh or hour < eh:
                return name
    return None


if __name__ == "__main__":
    cli()