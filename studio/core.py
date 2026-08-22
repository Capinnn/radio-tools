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
import threading
import time
import uuid

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
    return hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:12]


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
        tags = audio.tags or {}
        info["title"] = _first(tags.get("title"))
        info["artist"] = _first(tags.get("artist") or tags.get("albumartist"))
        info["album"] = _first(tags.get("album"))
        info["genre"] = _first(tags.get("genre"))
        info["year"] = _first(tags.get("date") or tags.get("year"))[:4]
        info["bpm"] = _to_int(_first(tags.get("bpm")))
        stream = getattr(audio, "info", None)
        if stream is not None:
            info["duration"] = round(float(getattr(stream, "length", 0) or 0), 3)
            info["bitrate"] = int(getattr(stream, "bitrate", 0) or 0)
            info["sampleRate"] = int(getattr(stream, "sample_rate", 0) or 0)
            info["channels"] = int(getattr(stream, "channels", 0) or 0)

    if not info["title"]:
        # "Artist - Title.mp3" is the common untagged case; split it when we can.
        stem = os.path.splitext(os.path.basename(path))[0]
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
    ext = os.path.splitext(path)[1].lower()
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

    WAV and a few odd files cannot hold the tags we want; the caller keeps the
    edit in the library index either way, so the UI can say so honestly.
    """
    try:
        audio = mutagen.File(path, easy=True)
        if audio is None:
            return False
        if audio.tags is None:
            audio.add_tags()
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

    # Last resort: an unconfigured library still has to play something.
    fallback = {"id": None, "name": "Unclassified", "minArtistGap": 0}
    order.append(fallback)

    for relax_step, (artist_gap, repeat) in enumerate(
            ((True, True), (True, False), (False, False))):
        for category in order:
            if category["id"] is None:
                pool = library
            else:
                pool = [t for t in library if t.get("category") == category["id"]]
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
    for step in range(max(0, int(count))):
        result = pick_next(library, categories, working, now=now,
                           exclude_ids=exclude, rng=rng)
        if not result:
            break
        track = result["track"]
        picks.append(result)
        exclude.add(track["id"])
        # Space the simulated plays out by a typical track length so the
        # artist-gap rule sees a realistic clock as the plan extends.
        working.insert(0, {
            "trackId": track["id"],
            "artist": track.get("artist", ""),
            "category": result["category"],
            "at": now + (step + 1) * max(60.0, float(track.get("duration") or 180)),
        })
    return picks


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store:
    """All persistent state: flat JSON files in one directory."""

    def __init__(self, base_dir, data_dir=None, music_dir=None):
        self.base_dir = os.path.abspath(base_dir)
        self.data_dir = os.path.abspath(data_dir or os.path.join(self.base_dir, "data"))
        self._default_music = os.path.abspath(
            music_dir or os.path.join(self.base_dir, "music"))
        self._lock = threading.RLock()
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self._default_music, exist_ok=True)

    def path(self, name):
        return os.path.join(self.data_dir, name + ".json")

    def read(self, name, default):
        with self._lock:
            target = self.path(name)
            if not os.path.exists(target):
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
        return os.path.abspath(os.path.expanduser(configured)) if configured \
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
        root = self.music_dir()
        with self._lock:
            library = self.library()
            by_id = {t["id"]: t for t in library}
            seen = set()
            added, updated = 0, 0

            if os.path.isdir(root):
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [d for d in sorted(dirnames) if not d.startswith(".")]
                    for filename in sorted(filenames):
                        if os.path.splitext(filename)[1].lower() not in AUDIO_EXTENSIONS:
                            continue
                        full = os.path.join(dirpath, filename)
                        track_id = track_id_for_path(full)
                        seen.add(track_id)
                        try:
                            stat = os.stat(full)
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
                                "filename": filename,
                                "format": os.path.splitext(filename)[1].lstrip(".").upper(),
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
                "total": len(library), "musicDir": root}

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
            if tag_fields and os.path.exists(track.get("path", "")):
                written = write_tags(track["path"], tag_fields)
                if written:
                    try:
                        track["mtime"] = os.stat(track["path"]).st_mtime
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
                    os.remove(track["path"])
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
                clean.append({"uid": item.get("uid") or uuid.uuid4().hex[:10],
                              "trackId": item["trackId"],
                              "auto": bool(item.get("auto"))})
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
                clean.append({"uid": item.get("uid") or uuid.uuid4().hex[:10],
                              "trackId": item["trackId"],
                              "auto": bool(item.get("auto"))})
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
