"""Core logic for RADIO STUDIO: the JSON store, library scanning, search,
playlists and the rotation engine.

Everything here is deliberately free of Flask so it can be exercised directly
from tests. `app.py` is a thin HTTP layer on top of this module.
"""

import hashlib
import json
import os
import random
import re
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import mutagen

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".wav", ".ogg"}

# Dayparts are fixed buckets; a category carries one weight per bucket. Five
# named parts of the day are far easier to reason about than 24 hourly knobs,
# and "Power heavier in drive time" is expressible directly.
DAYPARTS = [
    {"id": "overnight", "name": "Overnight", "start": 0, "end": 6},
    {"id": "morning", "name": "Morning Drive", "start": 6, "end": 10},
    {"id": "midday", "name": "Midday", "start": 10, "end": 15},
    {"id": "afternoon", "name": "Afternoon Drive", "start": 15, "end": 19},
    {"id": "evening", "name": "Evening", "start": 19, "end": 24},
]

DEFAULT_WEIGHTS = {d["id"]: 1.0 for d in DAYPARTS}

DEFAULT_CATEGORIES = [
    {
        "id": "power",
        "name": "Power",
        "color": "#ff3b30",
        "spinsPerHour": 5,
        "minArtistGap": 40,
        "weights": {"overnight": 0.6, "morning": 1.4, "midday": 1.0,
                    "afternoon": 1.4, "evening": 1.0},
    },
    {
        "id": "hot",
        "name": "Hot",
        "color": "#ff8a1e",
        "spinsPerHour": 4,
        "minArtistGap": 40,
        "weights": {"overnight": 0.8, "morning": 1.2, "midday": 1.1,
                    "afternoon": 1.2, "evening": 1.0},
    },
    {
        "id": "medium",
        "name": "Medium",
        "color": "#22d3ee",
        "spinsPerHour": 3,
        "minArtistGap": 60,
        "weights": {"overnight": 1.0, "morning": 0.9, "midday": 1.0,
                    "afternoon": 0.9, "evening": 1.0},
    },
    {
        "id": "slow",
        "name": "Slow",
        "color": "#8b5cf6",
        "spinsPerHour": 2,
        "minArtistGap": 60,
        "weights": {"overnight": 1.6, "morning": 0.5, "midday": 0.8,
                    "afternoon": 0.5, "evening": 1.3},
    },
    {
        "id": "specialty",
        "name": "Specialty",
        "color": "#34d399",
        "spinsPerHour": 1,
        "minArtistGap": 90,
        "weights": {"overnight": 1.2, "morning": 0.3, "midday": 0.6,
                    "afternoon": 0.3, "evening": 1.4},
    },
]

DEFAULT_CONFIG = {
    "musicDir": "",          # empty means "use the bundled music/ folder"
    "crossfade": 4.0,        # seconds, 0-12
    "fadeSeconds": 2.0,      # the FADE IN / FADE OUT buttons
    "volume": 0.85,
    "duckLevel": 0.2,        # talk-over target level
    "autoDj": False,
    "autoDjMinQueue": 3,     # top the queue up when it drops below this
}

HISTORY_LIMIT = 400

# Tag keys mutagen's "easy" interface exposes consistently across formats.
EDITABLE_TAGS = ("title", "artist", "album", "genre", "date", "bpm")

# WAV (and AIFF) carry raw ID3, which mutagen's easy wrapper does not cover:
# those tags take Frame objects, not strings, so they need this mapping.
ID3_FRAMES = {"title": "TIT2", "artist": "TPE1", "album": "TALB",
              "genre": "TCON", "date": "TDRC", "bpm": "TBPM"}


def _is_raw_id3(tags):
    from mutagen.id3 import ID3
    return isinstance(tags, ID3)


def _tag_getter(tags):
    """One accessor for both the easy mapping and raw ID3 frames."""
    if _is_raw_id3(tags):
        def get(*keys):
            for key in keys:
                frame = ID3_FRAMES.get(key)
                if frame and frame in tags:
                    return str(tags[frame])
            return ""
        return get

    def get(*keys):
        for key in keys:
            if tags and tags.get(key):
                return _first(tags.get(key))
        return ""
    return get


def daypart_for_hour(hour):
    """Return the daypart id covering `hour` (0-23)."""
    hour = int(hour) % 24
    for part in DAYPARTS:
        if part["start"] <= hour < part["end"]:
            return part["id"]
    return DAYPARTS[-1]["id"]


def track_id_for_path(path):
    """Stable id derived from the absolute path.

    Deriving it from the path (rather than a random uuid) means a rescan does
    not orphan the ids sitting in playlists, the queue or the schedule.
    """
    return hashlib.sha1(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:12]


def _first(value):
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return "" if value is None else str(value)


def _to_int(value):
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else 0


def read_tags(path):
    """Read tags + stream properties. Never raises for an unreadable file."""
    info = {
        "title": "", "artist": "", "album": "", "genre": "", "year": "",
        "bpm": 0, "duration": 0.0, "bitrate": 0, "sampleRate": 0,
        "channels": 0, "hasArt": False,
    }
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        audio = None

    if audio is not None:
        get = _tag_getter(audio.tags)
        info["title"] = get("title")
        info["artist"] = get("artist", "albumartist")
        info["album"] = get("album")
        info["genre"] = get("genre")
        info["year"] = get("date", "year")[:4]
        info["bpm"] = _to_int(get("bpm"))
        stream = getattr(audio, "info", None)
        if stream is not None:
            info["duration"] = round(float(getattr(stream, "length", 0) or 0), 3)
            info["bitrate"] = int(getattr(stream, "bitrate", 0) or 0)
            info["sampleRate"] = int(getattr(stream, "sample_rate", 0) or 0)
            info["channels"] = int(getattr(stream, "channels", 0) or 0)

    if not info["title"]:
        # "Artist - Title.mp3" is the common untagged case; split it when we can.
        stem = Path(path).stem
        if " - " in stem and not info["artist"]:
            left, right = stem.split(" - ", 1)
            info["artist"], info["title"] = left.strip(), right.strip()
        else:
            info["title"] = stem
    if not info["artist"]:
        info["artist"] = "Unknown Artist"

    info["hasArt"] = read_art(path) is not None
    return info


def read_art(path):
    """Return (bytes, mimetype) for embedded cover art, or None."""
    ext = Path(path).suffix.lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3
            for frame in ID3(path).getall("APIC"):
                if frame.data:
                    return frame.data, frame.mime or "image/jpeg"
        elif ext == ".flac":
            from mutagen.flac import FLAC
            for picture in FLAC(path).pictures:
                if picture.data:
                    return picture.data, picture.mime or "image/jpeg"
        elif ext == ".m4a":
            from mutagen.mp4 import MP4
            audio = MP4(path)
            covers = audio.tags.get("covr") if audio.tags else None
            if covers:
                cover = covers[0]
                is_png = getattr(cover, "imageformat", None) == MP4.Cover.FORMAT_PNG
                return bytes(cover), "image/png" if is_png else "image/jpeg"
        elif ext == ".ogg":
            import base64
            from mutagen.flac import Picture
            audio = mutagen.File(path)
            for encoded in (audio.tags or {}).get("metadata_block_picture", []):
                picture = Picture(base64.b64decode(encoded))
                if picture.data:
                    return picture.data, picture.mime or "image/jpeg"
    except Exception:
        return None
    return None


def write_tags(path, fields):
    """Write tags back into the file itself. Returns True when it stuck.

    Some files cannot hold the tags we want; the caller keeps the edit in the
    library index either way, so the UI can say so honestly.
    """
    try:
        audio = mutagen.File(path, easy=True)
        if audio is None:
            return False
        if audio.tags is None:
            audio.add_tags()

        if _is_raw_id3(audio.tags):
            from mutagen.id3 import Frames
            for key, value in fields.items():
                frame_id = ID3_FRAMES.get(key)
                if not frame_id:
                    continue
                audio.tags.delall(frame_id)
                text = str(value or "").strip()
                if text:
                    audio.tags.add(Frames[frame_id](encoding=3, text=[text]))
        else:
            for key, value in fields.items():
                if key not in EDITABLE_TAGS:
                    continue
                text = str(value or "").strip()
                if text:
                    audio.tags[key] = text
                elif key in audio.tags:
                    del audio.tags[key]

        audio.save()
        return True
    except Exception:
        return False


def search_tracks(library, query="", genre="", artist="", category="",
                  bpm_min=None, bpm_max=None, sort="artist"):
    """Filter + sort the library.

    `query` matches title, artist, album and genre case-insensitively; every
    whitespace-separated term must appear somewhere in the track.
    """
    terms = [t for t in str(query or "").lower().split() if t]
    results = []
    for track in library:
        if terms:
            haystack = " ".join(str(track.get(k, "")) for k in
                                ("title", "artist", "album", "genre")).lower()
            if not all(term in haystack for term in terms):
                continue
        if genre and str(track.get("genre", "")).lower() != str(genre).lower():
            continue
        if artist and str(track.get("artist", "")).lower() != str(artist).lower():
            continue
        if category and track.get("category") != category:
            continue
        bpm = _to_int(track.get("bpm"))
        if bpm_min is not None and bpm < int(bpm_min):
            continue
        if bpm_max is not None and (bpm == 0 or bpm > int(bpm_max)):
            continue
        results.append(track)

    keys = {
        "artist": lambda t: (str(t.get("artist", "")).lower(), str(t.get("title", "")).lower()),
        "title": lambda t: str(t.get("title", "")).lower(),
        "genre": lambda t: (str(t.get("genre", "")).lower(), str(t.get("title", "")).lower()),
        "duration": lambda t: float(t.get("duration") or 0),
        "bpm": lambda t: _to_int(t.get("bpm")),
        "added": lambda t: -float(t.get("added") or 0),
        "played": lambda t: -float(t.get("lastPlayed") or 0),
    }
    return sorted(results, key=keys.get(sort, keys["artist"]))


# ---------------------------------------------------------------------------
# Rotation engine
# ---------------------------------------------------------------------------
#
# History entries look like {"trackId", "artist", "category", "at": epoch}.
# The engine is a pure function of (library, categories, history, now) so a
# test can hand it a synthetic clock and assert on the rules directly.


def _spins_last_hour(history, category_id, now):
    cutoff = now - 3600
    return sum(1 for h in history
               if h.get("at", 0) >= cutoff and h.get("category") == category_id)


def _last_played_artist(history, artist, now):
    """Seconds since `artist` last played, or None if never (within history)."""
    artist = str(artist or "").strip().lower()
    best = None
    for entry in history:
        if str(entry.get("artist", "")).strip().lower() == artist:
            age = now - entry.get("at", 0)
            if best is None or age < best:
                best = age
    return best


def category_scores(categories, history, now):
    """Score every category for "how badly does it need a spin right now".

    score = daypart weight x unmet share of the hourly target. A category that
    has already hit its target this hour drops to a small floor rather than
    zero, so a station with one category still rotates.
    """
    part = daypart_for_hour(time.localtime(now).tm_hour)
    scores = {}
    for cat in categories:
        weight = float((cat.get("weights") or {}).get(part, 1.0))
        target = max(0.0, float(cat.get("spinsPerHour") or 0)) * max(0.0, weight)
        if target <= 0:
            scores[cat["id"]] = 0.0
            continue
        played = _spins_last_hour(history, cat["id"], now)
        deficit = max(0.0, target - played) / target
        scores[cat["id"]] = target * max(0.02, deficit)
    return scores


def _weighted_choice(items, weights, rng):
    total = sum(weights)
    if total <= 0:
        return rng.choice(items) if items else None
    roll = rng.random() * total
    for item, weight in zip(items, weights):
        roll -= weight
        if roll <= 0:
            return item
    return items[-1]


def _eligible(pool, category, history, now, exclude_ids, recent_ids,
              honour_artist_gap=True, honour_repeat=True):
    gap_seconds = max(0, int(category.get("minArtistGap") or 0)) * 60
    out = []
    for track in pool:
        if track["id"] in exclude_ids:
            continue
        if honour_repeat and track["id"] in recent_ids:
            continue
        if honour_artist_gap and gap_seconds:
            age = _last_played_artist(history, track.get("artist"), now)
            if age is not None and age < gap_seconds:
                continue
        out.append(track)
    return out


def pick_next(library, categories, history, now=None, exclude_ids=(), rng=None):
    """Choose one track according to the rotation rules.

    Returns {"track", "category", "relaxed"} or None when nothing is playable.
    `relaxed` names the first rule that had to be dropped to find a track, so
    the UI can be honest about a rotation that is too tight for the library.
    """
    now = time.time() if now is None else now
    rng = rng or random
    exclude_ids = set(exclude_ids)
    if not library:
        return None

    # Never repeat a track inside a window sized to the library, so a small
    # library still cycles instead of locking onto one song.
    window = max(1, min(25, len(library) // 2))
    recent_ids = {h.get("trackId") for h in history[:window]}

    scores = category_scores(categories, history, now)
    usable = [c for c in categories
              if scores.get(c["id"], 0) > 0
              and any(t.get("category") == c["id"] for t in library)]

    order = []
    if usable:
        remaining = list(usable)
        weights = [scores[c["id"]] for c in remaining]
        while remaining:
            chosen = _weighted_choice(remaining, weights, rng)
            index = remaining.index(chosen)
            remaining.pop(index)
            weights.pop(index)
            order.append(chosen)

    if not order:
        if any(t.get("category") for t in library):
            # The library is categorised but the rotation ruled everything out
            # (every category at zero spins, or nothing left after exclusions).
            # Stalling is the honest answer; quietly playing off-rotation is not.
            return None
        # Nothing is categorised yet, so a fresh library still plays.
        order = [{"id": None, "name": "Unclassified", "minArtistGap": 0}]

    # Relax one rule at a time, and only after every category has been tried
    # at the current strictness — otherwise a loose category would poach
    # spins from a strict one that merely needed its artist gap dropped.
    for relax_step, (artist_gap, repeat) in enumerate(
            ((True, True), (True, False), (False, False))):
        for category in order:
            pool = library if category["id"] is None else \
                [t for t in library if t.get("category") == category["id"]]
            candidates = _eligible(pool, category, history, now, exclude_ids,
                                   recent_ids, artist_gap, repeat)
            if not candidates:
                continue
            # Prefer the coldest tracks, then pick randomly inside that window
            # so the rotation is not identical every hour.
            candidates.sort(key=lambda t: float(t.get("lastPlayed") or 0))
            top = candidates[:max(1, int(len(candidates) * 0.4))]
            track = rng.choice(top)
            relaxed = (None, "repeat-window", "artist-gap")[relax_step]
            if category["id"] is None and relaxed is None:
                relaxed = "uncategorised"
            return {"track": track, "category": category["id"], "relaxed": relaxed}
    return None


def plan_next(library, categories, history, count=1, now=None,
              exclude_ids=(), rng=None):
    """Pick `count` tracks, feeding each choice back in as simulated history."""
    now = time.time() if now is None else now
    working = list(history)
    exclude = set(exclude_ids)
    picks = []
    # The clock walks forward with the plan, so each pick is judged at the
    # moment it would actually air rather than all at now.
    cursor = now
    for _ in range(max(0, int(count))):
        result = pick_next(library, categories, working, now=cursor,
                           exclude_ids=exclude, rng=rng)
        if not result:
            break
        track = result["track"]
        picks.append(result)
        exclude.add(track["id"])
        working.insert(0, {
            "trackId": track["id"],
            "artist": track.get("artist", ""),
            "category": result["category"],
            "at": cursor,
        })
        cursor += max(60.0, float(track.get("duration") or 180))
    return picks


# ---------------------------------------------------------------------------#
# Broadcast rotation bridge
# ---------------------------------------------------------------------------#
#
# The ``broadcast`` toolkit (../broadcast/playlistgen.py) has a richer rotation
# engine than the studio's built-in picker: spins-per-hour targets, daypart
# weight overrides, and artist/title/category gap rules.  The studio and the
# broadcast package use different category vocabularies — the studio has
# named categories (power, hot, medium, slow, specialty) while broadcast uses
# short codes (A, B, C, NEW, ...).  The bridge below translates the studio's
# settings into a broadcast-format rotation.json + library.json, invokes
# ``playlistgen`` as a subprocess, and maps the generated playlist back to
# studio track ids by matching file paths.

# Studio category id -> broadcast category code.  The codes are conventional
# radio rotation tiers: A=current/heavy, B=medium, C=light, D=slow, NEW=new.
# The mapping is order-stable so the same studio config always produces the
# same broadcast rotation file.
CATEGORY_CODE_MAP = {
    "power": "A",
    "hot": "B",
    "medium": "C",
    "slow": "D",
    "specialty": "NEW",
}

# Studio daypart id -> broadcast daypart name (the names playlistgen expects).
DAYPART_NAME_MAP = {
    "overnight": "Overnight",
    "morning": "Morning",
    "midday": "Middrive",
    "afternoon": "Afternoon",
    "evening": "Evening",
}


class RotationError(Exception):
    """Raised when the broadcast rotation engine is unavailable or fails."""


def _artist_gap_from_categories(categories):
    """Pick a sensible artist_gap (in number-of-tracks) from the studio config.

    playlistgen's artist_gap is a track count, while the studio's minArtistGap
    is in minutes.  We translate the smallest non-zero minute gap into a
    conservative track count (roughly gap_minutes / 4 minutes per track).
    """
    gaps = [c.get("minArtistGap", 0) for c in categories if c.get("minArtistGap")]
    if not gaps:
        return 2
    minutes = min(gaps)
    return max(1, int(minutes / 4))


def build_broadcast_rotation(categories):
    """Translate studio categories + daypart weights into broadcast rotation.json.

    Returns a dict matching the format described in broadcast/formats.md:
    ``{"categories": {code: {sph, description}}, "rules": {...},
    "dayparts": {name: {start, end, weights}}}``.
    """
    broadcast_cats = {}
    code_for = {}
    for cat in categories:
        code = CATEGORY_CODE_MAP.get(cat["id"], cat["id"].upper()[:1])
        code_for[cat["id"]] = code
        broadcast_cats[code] = {
            "sph": int(cat.get("spinsPerHour", 0)),
            "description": cat.get("name", cat["id"]),
        }

    artist_gap = _artist_gap_from_categories(categories)

    dayparts = {}
    for dp in DAYPARTS:
        name = DAYPART_NAME_MAP.get(dp["id"], dp["name"])
        weights = {}
        for cat in categories:
            code = code_for.get(cat["id"])
            if code:
                w = (cat.get("weights") or {}).get(dp["id"], 1.0)
                weights[code] = float(w)
        dayparts[name] = {
            "start": f"{dp['start']:02d}:00",
            "end": f"{dp['end']:02d}:00" if dp["end"] <= 24 else "24:00",
            "weights": weights,
        }

    return {
        "categories": broadcast_cats,
        "rules": {
            "artist_gap": artist_gap,
            "title_gap": 1,
            "category_gap": 1,
        },
        "dayparts": dayparts,
    }


def build_broadcast_library(library, categories):
    """Translate the studio library into broadcast library.json format.

    Each track becomes {path, artist, title, album, category, duration,
    replaygain_track_gain}.  The studio category id is mapped to a broadcast
    code; uncategorised tracks get category None.
    """
    code_for = {}
    for cat in categories:
        code = CATEGORY_CODE_MAP.get(cat["id"], cat["id"].upper()[:1])
        code_for[cat["id"]] = code

    out = []
    for track in library:
        cat_id = track.get("category") or ""
        code = code_for.get(cat_id) if cat_id else None
        out.append({
            "path": track.get("path", ""),
            "artist": track.get("artist"),
            "title": track.get("title"),
            "album": track.get("album") or None,
            "category": code,
            "duration": float(track.get("duration") or 0),
            "replaygain_track_gain": None,
        })
    return out


def find_playlistgen(venv_dir=None):
    """Return the path to the playlistgen executable, or None if unavailable.

    Looks for ../.venv/bin/playlistgen relative to the studio directory, then
    falls back to a PATH lookup.  Works on both POSIX venvs (bin/) and Windows
    venvs (Scripts/playlistgen.exe).
    """
    candidates = []

    def _add_venv(dir_path):
        if not dir_path:
            return
        base = Path(dir_path)
        candidates.extend([
            base / "bin" / "playlistgen",
            base / "Scripts" / "playlistgen.exe",
            base / "Scripts" / "playlistgen",
        ])

    if venv_dir:
        _add_venv(venv_dir)
    # ../.venv relative to the studio directory (core.py lives in studio/).
    studio_dir = Path(__file__).resolve().parent
    _add_venv(studio_dir.parent / ".venv")

    for path in candidates:
        if path.is_file() and os.access(str(path), os.X_OK):
            return str(path)
    # Last resort: rely on PATH.
    from shutil import which
    return which("playlistgen")


def generate_rotation(library, categories, hour=None, daypart=None,
                      slot="30min", seed=None, venv_dir=None,
                      runner=None):
    """Drive the broadcast playlistgen tool to produce an ordered queue.

    Writes a temporary broadcast-format rotation.json and library.json, invokes
    ``playlistgen --library <tmp> --rotation <tmp> --hour H --daypart D
    --slot S --seed N -o <tmp.m3u>``, parses the JSON sidecar, and maps each
    track back to a studio track id by matching the absolute file path.

    Returns a dict: ``{"trackIds": [...], "daypart": str, "seed": int,
    "count": int, "engine": "playlistgen"}``.

    Raises :class:`RotationError` with a clear message (including install
    instructions) when playlistgen is missing or fails.

    ``runner`` is an optional callable ``(cmd_list) -> (returncode, stdout,
    stderr)`` injected by tests to fake the subprocess.
    """
    if not library:
        return {"trackIds": [], "daypart": daypart, "seed": seed or 0,
                "count": 0, "engine": "playlistgen"}

    executable = find_playlistgen(venv_dir=venv_dir)
    if executable is None and runner is None:
        raise RotationError(
            "playlistgen is not installed. Install the broadcast toolkit with: "
            "pip install -e ../radio-tools  (from the studio directory) or "
            "pip install -e .  (from the repo root)."
        )

    # Use the injected runner's executable path or the real one.
    exe = executable or "playlistgen"

    now = time.localtime()
    if hour is None:
        hour = now.tm_hour
    if daypart is None:
        daypart = DAYPART_NAME_MAP.get(daypart_for_hour(hour), None)
    if seed is None:
        seed = random.randint(0, 999999)

    rotation = build_broadcast_rotation(categories)
    broadcast_lib = build_broadcast_library(library, categories)

    # Path -> studio track id lookup for mapping the result back.
    path_to_id = {}
    for track in library:
        abs_path = str(Path(track.get("path", "")).resolve())
        path_to_id[abs_path] = track["id"]

    tmp_dir = Path(tempfile.mkdtemp(prefix="studio-rotation-"))
    lib_path = tmp_dir / "library.json"
    rot_path = tmp_dir / "rotation.json"
    out_path = tmp_dir / "playlist.m3u"

    try:
        with open(lib_path, "w", encoding="utf-8") as f:
            json.dump(broadcast_lib, f, indent=2, ensure_ascii=False)
        with open(rot_path, "w", encoding="utf-8") as f:
            json.dump(rotation, f, indent=2, ensure_ascii=False)

        cmd = [exe, "--library", str(lib_path), "--rotation", str(rot_path),
               "--hour", str(hour), "--slot", slot, "--seed", str(seed)]
        if daypart:
            cmd += ["--daypart", daypart]
        cmd += ["-o", str(out_path)]

        if runner is not None:
            returncode, stdout, stderr = runner(cmd)
        else:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
            returncode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr

        if returncode != 0:
            msg = stderr.strip() or stdout.strip() or "unknown error"
            raise RotationError(f"playlistgen failed: {msg}")

        sidecar_path = out_path.with_suffix(".json")
        if not sidecar_path.exists():
            raise RotationError("playlistgen did not produce a JSON sidecar.")

        with open(sidecar_path, "r", encoding="utf-8") as f:
            sidecar = json.load(f)

        track_ids = []
        unmatched = 0
        for entry in sidecar.get("tracks", []):
            abs_path = str(Path(entry.get("path", "")).resolve())
            tid = path_to_id.get(abs_path)
            if tid:
                track_ids.append(tid)
            else:
                unmatched += 1

        result = {
            "trackIds": track_ids,
            "daypart": sidecar.get("daypart", daypart),
            "seed": sidecar.get("seed", seed),
            "count": len(track_ids),
            "engine": "playlistgen",
        }
        if unmatched:
            result["unmatched"] = unmatched
        return result
    finally:
        # Clean up the temp directory.
        import shutil
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store:
    """All persistent state: flat JSON files in one directory."""

    def __init__(self, base_dir, data_dir=None, music_dir=None):
        self.base_dir = str(Path(base_dir).resolve())
        self.data_dir = str(Path(data_dir or Path(base_dir) / "data").resolve())
        self._default_music = str(Path(
            music_dir or Path(base_dir) / "music").resolve())
        self._lock = threading.RLock()
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self._default_music).mkdir(parents=True, exist_ok=True)

    def path(self, name):
        return str(Path(self.data_dir) / (name + ".json"))

    def read(self, name, default):
        with self._lock:
            target = self.path(name)
            target_path = Path(target)
            if not target_path.exists():
                return json.loads(json.dumps(default))
            try:
                with open(target, "r", encoding="utf-8") as handle:
                    return json.load(handle)
            except (ValueError, OSError):
                # Keep a corrupt file around rather than silently losing it.
                try:
                    os.replace(target, target + ".corrupt")
                except OSError:
                    pass
                return json.loads(json.dumps(default))

    def write(self, name, payload):
        with self._lock:
            target = self.path(name)
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            os.replace(tmp, target)
        return payload

    # -- config ------------------------------------------------------------

    def config(self):
        stored = self.read("config", {})
        merged = dict(DEFAULT_CONFIG)
        merged.update(stored if isinstance(stored, dict) else {})
        return merged

    def save_config(self, updates):
        with self._lock:
            config = self.config()
            for key, value in (updates or {}).items():
                if key in DEFAULT_CONFIG:
                    config[key] = value
            config["crossfade"] = max(0.0, min(12.0, float(config["crossfade"])))
            config["fadeSeconds"] = max(0.5, min(10.0, float(config["fadeSeconds"])))
            config["volume"] = max(0.0, min(1.0, float(config["volume"])))
            config["duckLevel"] = max(0.0, min(1.0, float(config["duckLevel"])))
            config["autoDjMinQueue"] = max(1, min(20, int(config["autoDjMinQueue"])))
            config["autoDj"] = bool(config["autoDj"])
            self.write("config", config)
        return config

    def music_dir(self):
        configured = (self.config().get("musicDir") or "").strip()
        return str(Path(configured).expanduser().resolve()) if configured \
            else self._default_music

    # -- library -----------------------------------------------------------

    def library(self):
        library = self.read("library", [])
        return library if isinstance(library, list) else []

    def save_library(self, library):
        return self.write("library", library)

    def track(self, track_id):
        for track in self.library():
            if track.get("id") == track_id:
                return track
        return None

    def scan(self, prune=True):
        """Walk the music folder, adopt new files, refresh known ones.

        User-owned fields (category, playCount and any hand-edited tags) are
        preserved; only stream properties are refreshed unless the file's
        mtime changed.
        """
        root = Path(self.music_dir())
        with self._lock:
            library = self.library()
            by_id = {t["id"]: t for t in library}
            seen = set()
            added, updated = 0, 0

            if root.is_dir():
                for file_path in sorted(root.rglob("*")):
                    if not file_path.is_file():
                        continue
                    # Skip files inside hidden directories.
                    rel = file_path.relative_to(root)
                    if any(part.startswith(".") for part in rel.parts[:-1]):
                        continue
                    if file_path.suffix.lower() not in AUDIO_EXTENSIONS:
                        continue
                    full = str(file_path.resolve())
                    track_id = track_id_for_path(full)
                    seen.add(track_id)
                    try:
                        stat = file_path.stat()
                    except OSError:
                        continue
                    existing = by_id.get(track_id)
                    if existing and existing.get("mtime") == stat.st_mtime:
                        continue
                    tags = read_tags(full)
                    if existing:
                        # A rescan after an on-disk edit should win, but a
                        # field the user cleared in the app stays cleared.
                        existing.update(tags)
                        existing["size"] = stat.st_size
                        existing["mtime"] = stat.st_mtime
                        updated += 1
                    else:
                        entry = {
                            "id": track_id,
                            "path": full,
                            "filename": file_path.name,
                            "format": file_path.suffix.lstrip(".").upper(),
                            "size": stat.st_size,
                            "mtime": stat.st_mtime,
                            "added": time.time(),
                            "category": "",
                            "playCount": 0,
                            "lastPlayed": 0,
                            **tags,
                        }
                        library.append(entry)
                        by_id[track_id] = entry
                        added += 1

            removed = 0
            if prune:
                kept = [t for t in library if t["id"] in seen]
                removed = len(library) - len(kept)
                library = kept
            self.save_library(library)

        return {"added": added, "updated": updated, "removed": removed,
                "total": len(library), "musicDir": str(root)}

    def update_track(self, track_id, fields):
        """Edit metadata in the index and, where possible, in the file itself."""
        editable = {"title", "artist", "album", "genre", "year", "bpm", "category"}
        with self._lock:
            library = self.library()
            track = next((t for t in library if t["id"] == track_id), None)
            if not track:
                return None, False
            clean = {k: v for k, v in (fields or {}).items() if k in editable}
            if "bpm" in clean:
                clean["bpm"] = _to_int(clean["bpm"])
            track.update(clean)

            tag_fields = {k: v for k, v in clean.items()
                          if k in ("title", "artist", "album", "genre", "bpm")}
            if "year" in clean:
                tag_fields["date"] = clean["year"]
            written = False
            track_path = Path(track.get("path", ""))
            if tag_fields and track_path.exists():
                written = write_tags(str(track_path), tag_fields)
                if written:
                    try:
                        track["mtime"] = track_path.stat().st_mtime
                    except OSError:
                        pass
            self.save_library(library)
        return track, written

    def remove_track(self, track_id, delete_file=False):
        with self._lock:
            library = self.library()
            track = next((t for t in library if t["id"] == track_id), None)
            if not track:
                return False
            self.save_library([t for t in library if t["id"] != track_id])
            if delete_file:
                try:
                    Path(track["path"]).unlink(missing_ok=True)
                except OSError:
                    pass
            self.save_queue([i for i in self.queue() if i.get("trackId") != track_id])
            playlists = self.playlists()
            for playlist in playlists:
                playlist["trackIds"] = [i for i in playlist.get("trackIds", [])
                                        if i != track_id]
            self.write("playlists", playlists)
        return True

    # -- queue -------------------------------------------------------------

    def queue(self):
        stored = self.read("queue", [])
        items = stored if isinstance(stored, list) else []
        clean = []
        for item in items:
            if isinstance(item, str):
                item = {"trackId": item}
            if isinstance(item, dict) and item.get("trackId"):
                entry = {"uid": item.get("uid") or uuid.uuid4().hex[:10],
                         "trackId": item["trackId"],
                         "auto": bool(item.get("auto"))}
                source = item.get("source")
                if source:
                    entry["source"] = str(source)
                clean.append(entry)
        return clean

    def save_queue(self, items):
        return self.write("queue", self.__class__._clean_queue(items))

    @staticmethod
    def _clean_queue(items):
        clean = []
        for item in items or []:
            if isinstance(item, str):
                item = {"trackId": item}
            if isinstance(item, dict) and item.get("trackId"):
                entry = {"uid": item.get("uid") or uuid.uuid4().hex[:10],
                          "trackId": item["trackId"],
                          "auto": bool(item.get("auto"))}
                source = item.get("source")
                if source:
                    entry["source"] = str(source)
                clean.append(entry)
        return clean

    # -- playlists ---------------------------------------------------------

    def playlists(self):
        stored = self.read("playlists", [])
        return stored if isinstance(stored, list) else []

    def save_playlist(self, name, track_ids, playlist_id=None):
        name = str(name or "").strip() or "Untitled playlist"
        track_ids = [str(t) for t in (track_ids or [])]
        with self._lock:
            playlists = self.playlists()
            existing = next((p for p in playlists if p["id"] == playlist_id), None)
            if existing is None:
                existing = next((p for p in playlists
                                 if p["name"].lower() == name.lower()), None)
            if existing:
                existing.update({"name": name, "trackIds": track_ids,
                                 "updated": time.time()})
                result = existing
            else:
                result = {"id": uuid.uuid4().hex[:10], "name": name,
                          "trackIds": track_ids, "created": time.time(),
                          "updated": time.time()}
                playlists.append(result)
            self.write("playlists", playlists)
        return result

    def playlist(self, playlist_id):
        return next((p for p in self.playlists() if p["id"] == playlist_id), None)

    def delete_playlist(self, playlist_id):
        with self._lock:
            playlists = self.playlists()
            kept = [p for p in playlists if p["id"] != playlist_id]
            self.write("playlists", kept)
        return len(kept) != len(playlists)

    # -- rotation ----------------------------------------------------------

    def categories(self):
        stored = self.read("rotation", None)
        if not isinstance(stored, list) or not stored:
            return json.loads(json.dumps(DEFAULT_CATEGORIES))
        clean = []
        for cat in stored:
            if not isinstance(cat, dict) or not cat.get("id"):
                continue
            weights = dict(DEFAULT_WEIGHTS)
            weights.update({k: float(v) for k, v in (cat.get("weights") or {}).items()
                            if k in DEFAULT_WEIGHTS})
            clean.append({
                "id": str(cat["id"]),
                "name": str(cat.get("name") or cat["id"]),
                "color": str(cat.get("color") or "#22d3ee"),
                "spinsPerHour": max(0, min(60, int(cat.get("spinsPerHour") or 0))),
                "minArtistGap": max(0, min(600, int(cat.get("minArtistGap") or 0))),
                "weights": weights,
            })
        return clean

    def save_categories(self, categories):
        with self._lock:
            self.write("rotation", categories)
            valid = {c["id"] for c in self.categories()}
            library = self.library()
            dirty = False
            for track in library:
                if track.get("category") and track["category"] not in valid:
                    track["category"] = ""
                    dirty = True
            if dirty:
                self.save_library(library)
        return self.categories()

    # -- history -----------------------------------------------------------

    def history(self):
        stored = self.read("history", [])
        return stored if isinstance(stored, list) else []

    def record_play(self, track_id, at=None):
        """Log a play: drives the rotation rules and the library's counters."""
        with self._lock:
            library = self.library()
            track = next((t for t in library if t["id"] == track_id), None)
            if not track:
                return None
            stamp = time.time() if at is None else float(at)
            track["playCount"] = int(track.get("playCount") or 0) + 1
            track["lastPlayed"] = stamp
            self.save_library(library)

            history = self.history()
            history.insert(0, {
                "trackId": track_id,
                "artist": track.get("artist", ""),
                "title": track.get("title", ""),
                "category": track.get("category") or None,
                "at": stamp,
            })
            self.write("history", history[:HISTORY_LIMIT])
        return track

    # -- schedule ----------------------------------------------------------

    def schedule(self):
        stored = self.read("schedule", [])
        return stored if isinstance(stored, list) else []

    def save_schedule(self, entries):
        clean = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            at = str(entry.get("time") or "").strip()
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
                continue
            kind = entry.get("action") if entry.get("action") in ("playlist", "category") \
                else "playlist"
            clean.append({
                "id": entry.get("id") or uuid.uuid4().hex[:10],
                "time": at,
                "action": kind,
                "targetId": str(entry.get("targetId") or ""),
                "repeat": "daily" if entry.get("repeat") != "once" else "once",
                "mode": "replace" if entry.get("mode") != "append" else "append",
                "enabled": bool(entry.get("enabled", True)),
                "lastFired": float(entry.get("lastFired") or 0),
            })
        return self.write("schedule", clean)
