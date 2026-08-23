#!/usr/bin/env python3
"""RADIO STUDIO — Flask backend.

Serves the single-page console, streams audio with Range support (the browser
needs it to seek), and exposes the JSON API over `core.Store`.
"""

import io
import mimetypes
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, render_template, request,
                   send_file)
from werkzeug.utils import secure_filename

import core

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
MAX_UPLOAD_MB = 512

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
store = core.Store(BASE_DIR,
                   data_dir=os.environ.get("STUDIO_DATA_DIR"),
                   music_dir=os.environ.get("STUDIO_MUSIC_DIR"))


def _int_arg(name):
    value = request.args.get(name)
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Cross-platform server restart helper
# ---------------------------------------------------------------------------

def _port_in_use(port, host="127.0.0.1"):
    """Return True if something is listening on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def restart_server(host, port, timeout=5, argv=None):
    """Restart the studio server: terminate any listener on port, then
    relaunch ``app.py`` in a new process and exit the current process.

    Uses psutil when available; otherwise falls back to platform-aware
    process lookup/termination (taskkill on Windows, lsof + SIGTERM on
    POSIX).
    """
    argv = argv or sys.argv
    own_pid = os.getpid()

    try:
        import psutil
    except Exception:  # pragma: no cover - optional dependency
        psutil = None

    target_pids = set()

    if psutil is not None:
        try:
            for conn in psutil.net_connections(kind="inet"):
                if getattr(conn.laddr, "port", None) == port:
                    pid = getattr(conn, "pid", None)
                    if pid and pid != own_pid:
                        target_pids.add(pid)
        except Exception:
            pass
    else:
        # Platform-aware fallback.
        if sys.platform == "win32":
            try:
                proc = subprocess.run(
                    ["netstat", "-ano"], capture_output=True, text=True, timeout=10
                )
                for line in proc.stdout.splitlines():
                    if f":{port}" not in line:
                        continue
                    parts = line.strip().split()
                    if parts and parts[-1].isdigit():
                        pid = int(parts[-1])
                        if pid != own_pid:
                            target_pids.add(pid)
            except Exception:
                pass
            for pid in target_pids:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                )
        else:
            try:
                proc = subprocess.run(
                    ["lsof", "-ti", f":{port}"],
                    capture_output=True, text=True, timeout=10
                )
                for pid_str in proc.stdout.split():
                    try:
                        pid = int(pid_str)
                        if pid != own_pid:
                            target_pids.add(pid)
                    except ValueError:
                        pass
            except Exception:
                pass

    # Terminate surviving PIDs (skipped on Windows where taskkill already ran).
    for pid in target_pids:
        if pid == own_pid:
            continue
        try:
            if psutil is not None:
                psutil.Process(pid).terminate()
            elif sys.platform != "win32":
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    # Wait until the port is free (or the timeout expires).
    deadline = time.time() + timeout
    while _port_in_use(port, host) and time.time() < deadline:
        time.sleep(0.2)

    # Relaunch without --restart to avoid an infinite loop.
    script_path = Path(__file__).resolve()
    new_argv = [sys.executable, str(script_path)]
    skip_next = False
    for arg in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg == "--restart":
            continue
        if arg.startswith("--restart") and "=" in arg:
            continue
        new_argv.append(arg)

    if sys.platform == "win32":
        subprocess.Popen(
            new_argv,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    else:
        subprocess.Popen(new_argv, start_new_session=True, close_fds=True)

    sys.exit(0)


@app.get("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

@app.get("/api/state")
def api_state():
    """Everything the console needs for a cold start, in one round trip."""
    return jsonify({
        "config": store.config(),
        "musicDir": store.music_dir(),
        "library": core.search_tracks(store.library()),
        "queue": store.queue(),
        "playlists": store.playlists(),
        "categories": store.categories(),
        "dayparts": core.DAYPARTS,
        "schedule": store.schedule(),
        "history": store.history()[:40],
    })


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

@app.get("/api/library")
def api_library():
    return jsonify(core.search_tracks(store.library()))


@app.get("/api/tracks")
def api_tracks():
    tracks = core.search_tracks(
        store.library(),
        query=request.args.get("q", ""),
        genre=request.args.get("genre", ""),
        artist=request.args.get("artist", ""),
        category=request.args.get("category", ""),
        bpm_min=_int_arg("bpmMin"),
        bpm_max=_int_arg("bpmMax"),
        sort=request.args.get("sort", "artist"),
    )
    return jsonify({"tracks": tracks, "count": len(tracks)})


@app.post("/api/library/scan")
def api_scan():
    payload = request.get_json(silent=True) or {}
    if "musicDir" in payload:
        folder = str(payload["musicDir"] or "").strip()
        if folder:
            expanded = os.path.abspath(os.path.expanduser(folder))
            if not os.path.isdir(expanded):
                return jsonify({"error": f"not a folder: {expanded}"}), 400
        store.save_config({"musicDir": folder})
    return jsonify(store.scan())


@app.patch("/api/tracks/<track_id>")
def api_update_track(track_id):
    track, tags_written = store.update_track(track_id, request.get_json(silent=True) or {})
    if not track:
        return jsonify({"error": "track not found"}), 404
    return jsonify({"track": track, "tagsWritten": tags_written})


@app.delete("/api/tracks/<track_id>")
def api_delete_track(track_id):
    delete_file = request.args.get("deleteFile") == "1"
    if not store.remove_track(track_id, delete_file=delete_file):
        return jsonify({"error": "track not found"}), 404
    return jsonify({"ok": True, "deletedFile": delete_file})


@app.post("/api/library/import")
def api_import():
    """Accept files dragged onto the window; land them in the music folder."""
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "no files supplied"}), 400
    target_dir = store.music_dir()
    os.makedirs(target_dir, exist_ok=True)

    saved, skipped = [], []
    for storage in files:
        if not storage or not storage.filename:
            continue
        extension = os.path.splitext(storage.filename)[1].lower()
        if extension not in core.AUDIO_EXTENSIONS:
            skipped.append({"name": storage.filename, "reason": "unsupported format"})
            continue
        name = secure_filename(storage.filename) or "track" + extension
        stem, extension = os.path.splitext(name)
        candidate, counter = name, 1
        while os.path.exists(os.path.join(target_dir, candidate)):
            candidate = f"{stem}-{counter}{extension}"
            counter += 1
        try:
            storage.save(os.path.join(target_dir, candidate))
            saved.append(candidate)
        except OSError as exc:
            skipped.append({"name": storage.filename, "reason": str(exc)})

    result = store.scan()
    return jsonify({"saved": saved, "skipped": skipped, **result})


# ---------------------------------------------------------------------------
# Audio + art
# ---------------------------------------------------------------------------

@app.get("/api/audio/<track_id>")
def api_audio(track_id):
    track = store.track(track_id)
    if not track or not os.path.exists(track.get("path", "")):
        abort(404)
    mime = mimetypes.guess_type(track["path"])[0] or "application/octet-stream"
    response = send_file(track["path"], mimetype=mime, conditional=True)
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/art/<track_id>")
def api_art(track_id):
    track = store.track(track_id)
    if not track or not os.path.exists(track.get("path", "")):
        abort(404)
    art = core.read_art(track["path"])
    if not art:
        abort(404)
    data, mime = art
    return send_file(io.BytesIO(data), mimetype=mime, max_age=3600)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@app.get("/api/config")
def api_get_config():
    config = store.config()
    config["resolvedMusicDir"] = store.music_dir()
    return jsonify(config)


@app.put("/api/config")
def api_put_config():
    payload = request.get_json(silent=True) or {}
    if "musicDir" in payload:
        folder = str(payload["musicDir"] or "").strip()
        if folder and not os.path.isdir(os.path.abspath(os.path.expanduser(folder))):
            return jsonify({"error": "that folder does not exist"}), 400
    try:
        config = store.save_config(payload)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": f"invalid setting: {exc}"}), 400
    config["resolvedMusicDir"] = store.music_dir()
    return jsonify(config)


# ---------------------------------------------------------------------------
# Queue + playlists
# ---------------------------------------------------------------------------

@app.get("/api/queue")
def api_get_queue():
    return jsonify(store.queue())


@app.put("/api/queue")
def api_put_queue():
    payload = request.get_json(silent=True)
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return jsonify({"error": "expected an array of queue items"}), 400
    return jsonify(store.save_queue(items))


@app.get("/api/playlists")
def api_get_playlists():
    return jsonify(store.playlists())


@app.post("/api/playlists")
def api_post_playlist():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("trackIds"), list):
        return jsonify({"error": "trackIds must be an array"}), 400
    if not str(payload.get("name") or "").strip():
        return jsonify({"error": "a playlist name is required"}), 400
    playlist = store.save_playlist(payload["name"], payload["trackIds"],
                                   payload.get("id"))
    return jsonify(playlist)


@app.get("/api/playlists/<playlist_id>")
def api_get_playlist(playlist_id):
    playlist = store.playlist(playlist_id)
    if not playlist:
        return jsonify({"error": "playlist not found"}), 404
    known = {t["id"] for t in store.library()}
    return jsonify({**playlist,
                    "trackIds": [i for i in playlist["trackIds"] if i in known],
                    "missing": [i for i in playlist["trackIds"] if i not in known]})


@app.delete("/api/playlists/<playlist_id>")
def api_delete_playlist(playlist_id):
    if not store.delete_playlist(playlist_id):
        return jsonify({"error": "playlist not found"}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Rotation + history
# ---------------------------------------------------------------------------

@app.get("/api/rotation")
def api_get_rotation():
    return jsonify({"categories": store.categories(), "dayparts": core.DAYPARTS,
                    "daypart": core.daypart_for_hour(time.localtime().tm_hour)})


@app.put("/api/rotation")
def api_put_rotation():
    payload = request.get_json(silent=True)
    categories = payload.get("categories") if isinstance(payload, dict) else payload
    if not isinstance(categories, list):
        return jsonify({"error": "expected an array of categories"}), 400
    return jsonify({"categories": store.save_categories(categories)})


@app.post("/api/rotation/next")
def api_rotation_next():
    """Ask the rotation engine for the next N tracks (Auto-DJ)."""
    payload = request.get_json(silent=True) or {}
    try:
        count = max(1, min(50, int(payload.get("count", 1))))
    except (TypeError, ValueError):
        count = 1
    exclude = payload.get("excludeIds")
    exclude = exclude if isinstance(exclude, list) else []

    categories = store.categories()
    category_id = str(payload.get("categoryId") or "")
    if category_id:
        # A scheduled "start category X" restricts the pool to that one
        # category; give it a target so an exhausted hourly quota (or a
        # spins/hour of 0) cannot score it out of contention.
        categories = [dict(c, spinsPerHour=max(1, c["spinsPerHour"]))
                      for c in categories if c["id"] == category_id]
        if not categories:
            return jsonify({"error": "unknown category"}), 404
        if not any(t.get("category") == category_id for t in store.library()):
            # Do not let the engine's "play something" fallback quietly widen a
            # scheduled category start to the whole library.
            return jsonify({"picks": [], "reason": "no tracks in that category"})

    picks = core.plan_next(store.library(), categories, store.history(),
                           count=count, exclude_ids=exclude)
    return jsonify({
        "picks": [{"track": p["track"], "category": p["category"],
                   "relaxed": p["relaxed"]} for p in picks],
        "daypart": core.daypart_for_hour(time.localtime().tm_hour),
    })


@app.post("/api/rotation/generate")
def api_rotation_generate():
    """Drive the broadcast playlistgen engine to generate a 30-min block.

    Accepts optional JSON: {hour, daypart, slot, seed, excludeIds}.
    Returns {trackIds, daypart, seed, count, engine} on success.
    Falls back to the studio's built-in rotation picker (plan_next) with a
    ``fallback`` flag when playlistgen is unavailable.
    """
    payload = request.get_json(silent=True) or {}
    hour = payload.get("hour")
    daypart = payload.get("daypart")
    slot = payload.get("slot", "30min")
    seed = payload.get("seed")
    exclude = payload.get("excludeIds")
    exclude = exclude if isinstance(exclude, list) else []

    library = store.library()
    categories = store.categories()

    try:
        result = core.generate_rotation(
            library, categories, hour=hour, daypart=daypart,
            slot=slot, seed=seed,
        )
    except core.RotationError as exc:
        # Graceful degradation: fall back to the built-in rotation picker.
        picks = core.plan_next(library, categories, store.history(),
                               count=max(1, int(payload.get("count", 3))),
                               exclude_ids=exclude)
        return jsonify({
            "trackIds": [p["track"]["id"] for p in picks],
            "daypart": core.daypart_for_hour(time.localtime().tm_hour),
            "count": len(picks),
            "engine": "fallback",
            "fallback": True,
            "warning": str(exc),
        })

    # Honour excludeIds by filtering them from the generated list.
    if exclude:
        excl = set(exclude)
        result["trackIds"] = [t for t in result["trackIds"] if t not in excl]
        result["count"] = len(result["trackIds"])
    return jsonify(result)


@app.get("/api/history")
def api_history():
    return jsonify(store.history()[:100])


@app.post("/api/plays")
def api_record_play():
    payload = request.get_json(silent=True) or {}
    track = store.record_play(str(payload.get("trackId") or ""))
    if not track:
        return jsonify({"error": "track not found"}), 404
    return jsonify({"track": track})


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

@app.get("/api/schedule")
def api_get_schedule():
    return jsonify(store.schedule())


@app.put("/api/schedule")
def api_put_schedule():
    payload = request.get_json(silent=True)
    entries = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return jsonify({"error": "expected an array of schedule entries"}), 400
    return jsonify(store.save_schedule(entries))


@app.errorhandler(413)
def too_large(_exc):
    return jsonify({"error": f"upload exceeds {MAX_UPLOAD_MB} MB"}), 413


@app.errorhandler(404)
def not_found(_exc):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return Response("Not found", status=404, mimetype="text/plain")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="RADIO STUDIO")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("STUDIO_PORT", 5110)))
    parser.add_argument("--host", default=os.environ.get("STUDIO_HOST", "127.0.0.1"))
    parser.add_argument("--scan", action="store_true",
                        help="scan the music folder before starting")
    parser.add_argument("--restart", action="store_true",
                        help="restart any existing instance on this port first")
    args = parser.parse_args()

    if args.restart:
        restart_server(args.host, args.port)

    if args.scan:
        print("  scanning {} ...".format(store.music_dir()))
        print("  {added} added, {updated} updated, {removed} removed, "
              "{total} in library".format(**store.scan()))

    print(f"  RADIO STUDIO  ->  http://{args.host}:{args.port}")
    print(f"  music folder  ->  {store.music_dir()}")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
