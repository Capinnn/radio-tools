"""Validate the playlistgen JSON sidecar consumed by the broadcast chain."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


class PlaylistValidationError(ValueError):
    """Raised when a playlist or sidecar violates the shared format."""


@dataclass(frozen=True)
class PlaylistTrack:
    position: int
    path: str
    artist: str | None
    title: str | None
    category: str | None
    duration: float


@dataclass(frozen=True)
class PlaylistSidecar:
    generated_at: datetime
    seed: int
    daypart: str | None
    target_duration: float
    actual_duration: float
    tracks: tuple[PlaylistTrack, ...]


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlaylistValidationError(f"{field} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise PlaylistValidationError(f"{field} must be a finite non-negative number")
    return result


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PlaylistValidationError(f"{field} must be a string or null")
    return value


def load_playlist_sidecar(
    path: str | Path, *, require_files: bool = False
) -> PlaylistSidecar:
    """Load and validate a sidecar in the format emitted by playlistgen."""

    sidecar_path = Path(path)
    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlaylistValidationError(f"cannot read {sidecar_path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise PlaylistValidationError("playlist sidecar must be a JSON object")

    generated_at_raw = payload.get("generated_at")
    if not isinstance(generated_at_raw, str):
        raise PlaylistValidationError("generated_at must be a local ISO-8601 string")
    try:
        generated_at = datetime.fromisoformat(generated_at_raw)
    except ValueError as exc:
        raise PlaylistValidationError("generated_at is not valid ISO-8601") from exc
    if generated_at.tzinfo is not None:
        raise PlaylistValidationError("generated_at must use the station's local time")

    seed = payload.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise PlaylistValidationError("seed must be an integer")

    daypart = _optional_string(payload.get("daypart"), "daypart")
    target_duration = _number(payload.get("target_duration"), "target_duration")
    actual_duration = _number(payload.get("actual_duration"), "actual_duration")

    raw_tracks = payload.get("tracks")
    if not isinstance(raw_tracks, list):
        raise PlaylistValidationError("tracks must be an array")
    if not raw_tracks:
        raise PlaylistValidationError("tracks must contain at least one entry")

    tracks: list[PlaylistTrack] = []
    for expected_position, raw_track in enumerate(raw_tracks, start=1):
        prefix = f"tracks[{expected_position - 1}]"
        if not isinstance(raw_track, dict):
            raise PlaylistValidationError(f"{prefix} must be an object")
        if raw_track.get("position") != expected_position:
            raise PlaylistValidationError(
                f"{prefix}.position must be {expected_position}"
            )
        track_path = raw_track.get("path")
        if not isinstance(track_path, str) or not track_path.strip():
            raise PlaylistValidationError(f"{prefix}.path must be a non-empty string")
        if require_files and not Path(track_path).is_file():
            raise PlaylistValidationError(f"track file does not exist: {track_path}")
        tracks.append(
            PlaylistTrack(
                position=expected_position,
                path=track_path,
                artist=_optional_string(raw_track.get("artist"), f"{prefix}.artist"),
                title=_optional_string(raw_track.get("title"), f"{prefix}.title"),
                category=_optional_string(
                    raw_track.get("category"), f"{prefix}.category"
                ),
                duration=_number(raw_track.get("duration"), f"{prefix}.duration"),
            )
        )

    duration_sum = sum(track.duration for track in tracks)
    if not math.isclose(duration_sum, actual_duration, rel_tol=1e-6, abs_tol=0.05):
        raise PlaylistValidationError(
            "actual_duration does not match the sum of track durations"
        )

    return PlaylistSidecar(
        generated_at=generated_at,
        seed=seed,
        daypart=daypart,
        target_duration=target_duration,
        actual_duration=actual_duration,
        tracks=tuple(tracks),
    )


def load_m3u_paths(path: str | Path) -> tuple[str, ...]:
    """Return non-comment entries from an extended M3U playlist."""

    m3u_path = Path(path)
    try:
        lines = m3u_path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PlaylistValidationError(f"cannot read {m3u_path}: {exc}") from exc
    entries: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return tuple(entries)


def validate_playlist_pair(
    sidecar_path: str | Path,
    m3u_path: str | Path,
    *,
    require_files: bool = False,
) -> PlaylistSidecar:
    """Validate a sidecar and ensure its ordered paths match the paired M3U."""

    sidecar = load_playlist_sidecar(sidecar_path, require_files=require_files)
    m3u_paths = load_m3u_paths(m3u_path)
    json_paths = tuple(track.path for track in sidecar.tracks)
    if m3u_paths != json_paths:
        raise PlaylistValidationError(
            "M3U entries do not match JSON sidecar track paths in order"
        )
    return sidecar


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a playlistgen JSON sidecar and its paired M3U."
    )
    parser.add_argument("--sidecar", required=True, type=Path)
    parser.add_argument("--m3u", required=True, type=Path)
    parser.add_argument("--require-files", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        sidecar = validate_playlist_pair(
            args.sidecar, args.m3u, require_files=args.require_files
        )
    except PlaylistValidationError as exc:
        print(f"playlist validation failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Validated {len(sidecar.tracks)} tracks "
        f"({sidecar.actual_duration:.1f}s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
