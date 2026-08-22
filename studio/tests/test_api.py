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
