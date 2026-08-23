"""HTTP-level tests: the endpoints the console actually calls."""

import io


def test_index_page_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "RADIO STUDIO" in body
    assert "/static/js/app.js" in body
    assert "/static/css/style.css" in body


def test_state_bootstrap_has_everything_the_client_needs(client):
    client.post("/api/library/scan")
    payload = client.get("/api/state").get_json()
    assert set(payload) >= {"config", "library", "queue", "playlists",
                            "categories", "dayparts", "schedule", "musicDir"}
    assert len(payload["library"]) == 5
    assert len(payload["dayparts"]) == 5
    assert [c["id"] for c in payload["categories"]] == \
        ["power", "hot", "medium", "slow", "specialty"]


def test_scan_then_search(client):
    assert client.post("/api/library/scan").get_json()["added"] == 5

    everything = client.get("/api/tracks").get_json()
    assert everything["count"] == 5

    hit = client.get("/api/tracks?q=alpha").get_json()
    assert hit["count"] == 1
    assert hit["tracks"][0]["title"] == "alpha"

    assert client.get("/api/tracks?q=nothinghere").get_json()["count"] == 0


def test_track_edit_and_category_filter(client):
    client.post("/api/library/scan")
    track = client.get("/api/library").get_json()[0]

    response = client.patch(f"/api/tracks/{track['id']}",
                            json={"artist": "Vega", "genre": "Synth",
                                  "bpm": 118, "category": "power"})
    assert response.status_code == 200
    updated = response.get_json()["track"]
    assert updated["artist"] == "Vega"
    assert updated["bpm"] == 118

    filtered = client.get("/api/tracks?category=power").get_json()
    assert [t["id"] for t in filtered["tracks"]] == [track["id"]]
    assert client.get("/api/tracks?genre=Synth").get_json()["count"] == 1
    assert client.get("/api/tracks?bpmMin=100&bpmMax=130").get_json()["count"] == 1


def test_patching_a_missing_track_is_404(client):
    assert client.patch("/api/tracks/nope", json={"title": "x"}).status_code == 404


def test_audio_streams_with_range_support(client):
    client.post("/api/library/scan")
    track_id = client.get("/api/library").get_json()[0]["id"]

    full = client.get(f"/api/audio/{track_id}")
    assert full.status_code == 200
    assert full.headers["Accept-Ranges"] == "bytes"
    assert len(full.get_data()) > 1000

    partial = client.get(f"/api/audio/{track_id}", headers={"Range": "bytes=0-99"})
    assert partial.status_code == 206
    assert len(partial.get_data()) == 100

    assert client.get("/api/audio/missing").status_code == 404


def test_queue_round_trip(client):
    client.post("/api/library/scan")
    ids = [t["id"] for t in client.get("/api/library").get_json()][:3]

    saved = client.put("/api/queue", json={"items": [{"trackId": i} for i in ids]}).get_json()
    assert [i["trackId"] for i in saved] == ids
    assert all(i["uid"] for i in saved)
    assert [i["trackId"] for i in client.get("/api/queue").get_json()] == ids

    assert client.put("/api/queue", json={"items": "nope"}).status_code == 400


def test_playlist_endpoints(client):
    client.post("/api/library/scan")
    ids = [t["id"] for t in client.get("/api/library").get_json()][:2]

    created = client.post("/api/playlists",
                          json={"name": "Drive", "trackIds": ids}).get_json()
    assert created["name"] == "Drive"

    loaded = client.get(f"/api/playlists/{created['id']}").get_json()
    assert loaded["trackIds"] == ids
    assert loaded["missing"] == []

    assert client.post("/api/playlists", json={"name": "", "trackIds": []}).status_code == 400
    assert client.post("/api/playlists", json={"name": "x"}).status_code == 400
    assert client.get("/api/playlists/ghost").status_code == 404

    assert client.delete(f"/api/playlists/{created['id']}").status_code == 200
    assert client.get("/api/playlists").get_json() == []


def test_playlist_reports_tracks_that_left_the_library(client):
    client.post("/api/library/scan")
    ids = [t["id"] for t in client.get("/api/library").get_json()][:2]
    created = client.post("/api/playlists",
                          json={"name": "Mix", "trackIds": ids + ["ghost-id"]}).get_json()
    loaded = client.get(f"/api/playlists/{created['id']}").get_json()
    assert loaded["trackIds"] == ids
    assert loaded["missing"] == ["ghost-id"]


def test_rotation_config_round_trip(client):
    categories = client.get("/api/rotation").get_json()["categories"]
    categories[0]["spinsPerHour"] = 9
    categories[0]["weights"]["morning"] = 2.5

    saved = client.put("/api/rotation", json={"categories": categories}).get_json()
    assert saved["categories"][0]["spinsPerHour"] == 9
    assert saved["categories"][0]["weights"]["morning"] == 2.5
    assert client.get("/api/rotation").get_json()["categories"][0]["spinsPerHour"] == 9

    assert client.put("/api/rotation", json={"categories": "no"}).status_code == 400


def test_autodj_returns_categorised_tracks(client):
    client.post("/api/library/scan")
    library = client.get("/api/library").get_json()
    for track in library[:3]:
        client.patch(f"/api/tracks/{track['id']}", json={"category": "power"})

    picks = client.post("/api/rotation/next", json={"count": 2}).get_json()["picks"]
    assert len(picks) == 2
    assert all(p["category"] == "power" for p in picks)
    assert len({p["track"]["id"] for p in picks}) == 2


def test_autodj_can_be_restricted_to_one_category(client):
    client.post("/api/library/scan")
    library = client.get("/api/library").get_json()
    client.patch(f"/api/tracks/{library[0]['id']}", json={"category": "slow"})
    client.patch(f"/api/tracks/{library[1]['id']}", json={"category": "power"})

    picks = client.post("/api/rotation/next",
                        json={"count": 3, "categoryId": "slow"}).get_json()["picks"]
    assert [p["track"]["id"] for p in picks] == [library[0]["id"]]

    # An empty category must not silently widen to the whole library.
    empty = client.post("/api/rotation/next",
                        json={"count": 3, "categoryId": "specialty"}).get_json()
    assert empty["picks"] == []
    assert client.post("/api/rotation/next",
                       json={"categoryId": "bogus"}).status_code == 404


def test_recording_plays_feeds_history(client):
    client.post("/api/library/scan")
    track_id = client.get("/api/library").get_json()[0]["id"]

    assert client.post("/api/plays", json={"trackId": track_id}).status_code == 200
    assert client.post("/api/plays", json={"trackId": "ghost"}).status_code == 404

    history = client.get("/api/history").get_json()
    assert len(history) == 1 and history[0]["trackId"] == track_id
    assert client.get("/api/library").get_json()
    played = next(t for t in client.get("/api/library").get_json() if t["id"] == track_id)
    assert played["playCount"] == 1


def test_config_updates_are_persisted_and_clamped(client):
    updated = client.put("/api/config", json={"crossfade": 6.5, "autoDj": True}).get_json()
    assert updated["crossfade"] == 6.5
    assert updated["autoDj"] is True
    assert client.get("/api/config").get_json()["crossfade"] == 6.5

    assert client.put("/api/config", json={"crossfade": 100}).get_json()["crossfade"] == 12.0
    assert client.put("/api/config", json={"musicDir": "/definitely/not/here"}).status_code == 400


def test_schedule_round_trip(client):
    entries = client.put("/api/schedule", json={"entries": [
        {"time": "06:00", "action": "category", "targetId": "power", "repeat": "daily"},
    ]}).get_json()
    assert len(entries) == 1
    assert entries[0]["enabled"] is True
    assert client.get("/api/schedule").get_json()[0]["time"] == "06:00"
    assert client.put("/api/schedule", json={"entries": {}}).status_code == 400


def test_import_uploads_land_in_the_library(client):
    client.post("/api/library/scan")
    before = len(client.get("/api/library").get_json())

    with open(client.store.music_dir() + "/alpha.wav", "rb") as handle:
        blob = handle.read()

    response = client.post("/api/library/import", content_type="multipart/form-data",
                           data={"files": (io.BytesIO(blob), "imported.wav")})
    payload = response.get_json()
    assert payload["saved"] == ["imported.wav"]
    assert len(client.get("/api/library").get_json()) == before + 1


def test_import_rejects_non_audio(client):
    response = client.post("/api/library/import", content_type="multipart/form-data",
                           data={"files": (io.BytesIO(b"nope"), "notes.txt")})
    payload = response.get_json()
    assert payload["saved"] == []
    assert payload["skipped"][0]["reason"] == "unsupported format"


def test_unknown_api_route_returns_json_404(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.get_json()["error"] == "not found"


# --------------------------------------------------------------- compression


def test_static_js_is_gzipped_when_accepted(client):
    response = client.get("/static/js/app.js", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == "gzip"
    assert response.headers["Vary"] == "Accept-Encoding"
    assert len(response.get_data()) >= 500


def test_small_response_is_not_gzipped(client):
    # This endpoint returns a tiny JSON body, well under the 500-byte threshold.
    response = client.delete("/api/playlists/ghost", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 404
    assert "Content-Encoding" not in response.headers
    assert response.headers["Vary"] == "Accept-Encoding"


# --------------------------------------------------------------- rotation gen
#
# /api/rotation/generate drives the broadcast playlistgen engine.  The
# subprocess is faked with an injected runner so the tests do not depend on
# the broadcast package being installed.

def _fake_playlistgen_runner(library_paths):
    """Build a fake runner that writes a valid M3U + JSON sidecar.

    ``library_paths`` is the list of paths from the broadcast-format library
    (in the order playlistgen would see them).  The fake picks the first 3.
    """
    def runner(cmd):
        import json
        # Find the -o output path in the cmd list.
        out_path = cmd[cmd.index("-o") + 1]
        tracks = []
        for i, path in enumerate(library_paths[:3]):
            tracks.append({
                "position": i + 1,
                "path": path,
                "artist": f"Artist {i}",
                "title": f"Title {i}",
                "category": "A",
                "duration": 120.0,
            })
        sidecar = {
            "generated_at": "2026-08-21T14:00:00",
            "seed": 42,
            "daypart": "Morning",
            "target_duration": 1800,
            "actual_duration": 360.0,
            "tracks": tracks,
        }
        import os
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            f.write("#EXTM3U\n")
            for t in tracks:
                f.write(f"#EXTINF:{int(t['duration'])},{t['artist']} - {t['title']}\n")
                f.write(f"{t['path']}\n")
        sidecar_path = os.path.splitext(out_path)[0] + ".json"
        with open(sidecar_path, "w") as f:
            json.dump(sidecar, f)
        return 0, "Generated playlist: 3 tracks", ""
    return runner


def _fake_failing_runner(cmd):
    """A runner that simulates playlistgen crashing."""
    return 1, "", "playlistgen: error: something broke"


def test_rotation_generate_returns_ordered_track_ids(client, monkeypatch):
    client.post("/api/library/scan")
    library = client.get("/api/library").get_json()
    # Assign categories so the mapping is exercised.
    for track in library[:3]:
        client.patch(f"/api/tracks/{track['id']}", json={"category": "power"})
    for track in library[3:]:
        client.patch(f"/api/tracks/{track['id']}", json={"category": "slow"})

    # Build the list of paths the fake runner should "play", matching the
    # studio library order.
    studio_lib = client.store.library()
    lib_paths = [t["path"] for t in studio_lib]

    # Inject the fake runner into core.generate_rotation.
    import core
    original = core.generate_rotation
    fake = _fake_playlistgen_runner(lib_paths)

    def patched(library, categories, **kwargs):
        return original(library, categories, runner=fake, **kwargs)

    monkeypatch.setattr(core, "generate_rotation", patched)

    response = client.post("/api/rotation/generate", json={"seed": 42})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["engine"] == "playlistgen"
    assert payload["count"] == 3
    assert isinstance(payload["trackIds"], list)
    assert len(payload["trackIds"]) == 3
    # Every returned id must be a valid studio track id.
    studio_ids = {t["id"] for t in studio_lib}
    assert all(tid in studio_ids for tid in payload["trackIds"])
    # The order is deterministic (the fake picks the first 3).
    assert payload["trackIds"] == [t["id"] for t in studio_lib[:3]]


def test_rotation_generate_respects_category_mapping(client, monkeypatch):
    """The broadcast library written for playlistgen must use A/B/C/... codes."""
    import core, json, os, tempfile

    client.post("/api/library/scan")
    library = client.store.library()
    # Categorise all tracks to exercise the mapping.
    cats = client.get("/api/rotation").get_json()["categories"]
    for i, track in enumerate(library):
        cat_id = cats[i % len(cats)]["id"]
        client.patch(f"/api/tracks/{track['id']}", json={"category": cat_id})

    studio_lib = client.store.library()
    lib_paths = [t["path"] for t in studio_lib]
    fake = _fake_playlistgen_runner(lib_paths)
    original = core.generate_rotation

    def patched(library, categories, **kwargs):
        return original(library, categories, runner=fake, **kwargs)

    monkeypatch.setattr(core, "generate_rotation", patched)

    # Inspect the broadcast library that would be written.
    broadcast_lib = core.build_broadcast_library(studio_lib, cats)
    code_map = core.CATEGORY_CODE_MAP
    for i, track in enumerate(studio_lib):
        cat_id = track.get("category") or ""
        expected_code = code_map.get(cat_id)
        assert broadcast_lib[i]["category"] == expected_code
        assert broadcast_lib[i]["path"] == track["path"]

    # Also check the rotation file has the right codes.
    rotation = core.build_broadcast_rotation(cats)
    for cat in cats:
        code = code_map[cat["id"]]
        assert code in rotation["categories"]
        assert rotation["categories"][code]["sph"] == cat["spinsPerHour"]

    response = client.post("/api/rotation/generate", json={"seed": 1})
    assert response.status_code == 200


def test_rotation_generate_error_path_falls_back(client, monkeypatch):
    """When playlistgen fails, the endpoint returns fallback + warning."""
    import core

    original = core.generate_rotation

    def failing(library, categories, **kwargs):
        return original(library, categories, runner=_fake_failing_runner, **kwargs)

    monkeypatch.setattr(core, "generate_rotation", failing)

    client.post("/api/library/scan")
    # Assign at least one category so plan_next can pick something.
    lib = client.get("/api/library").get_json()
    client.patch(f"/api/tracks/{lib[0]['id']}", json={"category": "power"})

    response = client.post("/api/rotation/generate", json={"seed": 1})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["engine"] == "fallback"
    assert payload["fallback"] is True
    assert "warning" in payload
    assert isinstance(payload["trackIds"], list)


def test_rotation_generate_missing_playlistgen_falls_back(client, monkeypatch):
    """When playlistgen is not installed, the endpoint falls back gracefully."""
    import core

    def not_found(library, categories, **kwargs):
        raise core.RotationError("playlistgen is not installed.")

    monkeypatch.setattr(core, "generate_rotation", not_found)

    client.post("/api/library/scan")
    lib = client.get("/api/library").get_json()
    client.patch(f"/api/tracks/{lib[0]['id']}", json={"category": "power"})

    response = client.post("/api/rotation/generate", json={})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["engine"] == "fallback"
    assert "playlistgen" in payload["warning"].lower()


def test_rotation_generate_empty_library(client, monkeypatch):
    """An empty library returns an empty trackIds list without error."""
    import core

    response = client.post("/api/rotation/generate", json={"seed": 1})
    assert response.status_code == 200
    payload = response.get_json()
    # No tracks scanned yet, so the library is empty.
    assert payload["count"] == 0
    assert payload["trackIds"] == []


def test_console_page_contains_clock_view_panel(client):
    """The console exposes the read-only CLOCK hour-wheel panel."""
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'id="clockView"' in body
    assert "CLOCK" in body
    assert "hour structure, read only" in body
    assert 'id="clockViewTrack"' in body


def test_clock_view_slot_renderer_is_defined(client):
    """The static JS ships the function that paints the hour template."""
    response = client.get("/static/js/app.js")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    # Renderer name and the eight-slot template must both be in the bundle.
    assert "renderClockView" in body
    assert "CLOCK_VIEW_TEMPLATE" in body
    assert "id" in body and "sweeper" in body and "promo" in body
