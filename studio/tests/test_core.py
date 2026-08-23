"""Unit tests for scanning, search, playlists and the rotation engine."""

import os
import random
import time

import core


# --------------------------------------------------------------------- scan

def test_scan_finds_audio_and_ignores_other_files(store, music_dir):
    result = store.scan()
    assert result["added"] == 5
    assert result["total"] == 5
    library = store.library()
    assert {t["filename"] for t in library} == {
        "alpha.wav", "bravo.wav", "charlie.wav", "delta.wav", "echo.wav"}
    assert all(t["path"].startswith(str(music_dir)) for t in library)


def test_scan_reads_stream_properties(scanned):
    alpha = next(t for t in scanned.library() if t["filename"] == "alpha.wav")
    assert round(alpha["duration"]) == 1
    assert alpha["sampleRate"] == 44100
    assert alpha["channels"] == 2
    assert alpha["bitrate"] > 0
    assert alpha["format"] == "WAV"
    # No tags in a bare WAV, so the filename becomes the title.
    assert alpha["title"] == "alpha"
    assert alpha["artist"] == "Unknown Artist"


def test_scan_is_idempotent_and_ids_are_stable(scanned):
    before = {t["id"]: t["path"] for t in scanned.library()}
    result = scanned.scan()
    assert result == {**result, "added": 0, "updated": 0, "removed": 0, "total": 5}
    assert {t["id"]: t["path"] for t in scanned.library()} == before


def test_scan_prunes_deleted_files_and_keeps_user_fields(scanned, music_dir):
    library = scanned.library()
    library[0]["category"] = "power"
    library[0]["playCount"] = 7
    scanned.save_library(library)

    os.remove(os.path.join(str(music_dir), "echo.wav"))
    result = scanned.scan()

    assert result["removed"] == 1
    assert result["total"] == 4
    kept = next(t for t in scanned.library() if t["id"] == library[0]["id"])
    assert kept["category"] == "power"
    assert kept["playCount"] == 7


def test_scan_picks_up_subfolders(store, music_dir):
    from make_test_tracks import write_tone
    nested = music_dir / "b-sides"
    nested.mkdir()
    write_tone(str(nested / "hidden.wav"), 300.0, 1.0)
    assert store.scan()["total"] == 6


def test_scan_tags_sweepers_subfolder(store, music_dir):
    """Files under sweepers/ get kind='sweeper' and category='sweepers'."""
    from make_test_tracks import write_tone
    sweepers = music_dir / "sweepers"
    sweepers.mkdir()
    write_tone(str(sweepers / "station-id.wav"), 440.0, 1.0)
    store.scan()
    library = store.library()
    sweeper = next(t for t in library if t["filename"] == "station-id.wav")
    assert sweeper["kind"] == "sweeper"
    assert sweeper["category"] == "sweepers"


def test_scan_tags_jingles_subfolder(store, music_dir):
    """Files under jingles/ get kind='jingle' and category='jingles'."""
    from make_test_tracks import write_tone
    jingles = music_dir / "jingles"
    jingles.mkdir()
    write_tone(str(jingles / "intro.wav"), 440.0, 1.0)
    store.scan()
    library = store.library()
    jingle = next(t for t in library if t["filename"] == "intro.wav")
    assert jingle["kind"] == "jingle"
    assert jingle["category"] == "jingles"


def test_scan_tags_liners_subfolder(store, music_dir):
    """Files under liners/ get kind='liner' and category='liners'."""
    from make_test_tracks import write_tone
    liners = music_dir / "liners"
    liners.mkdir()
    write_tone(str(liners / "youre-listening-to.wav"), 440.0, 1.0)
    store.scan()
    library = store.library()
    liner = next(t for t in library if t["filename"] == "youre-listening-to.wav")
    assert liner["kind"] == "liner"
    assert liner["category"] == "liners"


def test_scan_music_files_have_no_kind(store):
    """Regular music files should not have a kind tag (or it is empty)."""
    store.scan()
    for track in store.library():
        assert track.get("kind", "") == ""


def test_edit_writes_tags_back_into_the_file(scanned):
    track = scanned.library()[0]
    updated, written = scanned.update_track(track["id"], {
        "title": "Night Drive", "artist": "The Meters", "genre": "Funk",
        "bpm": "112", "category": "power",
    })
    assert updated["title"] == "Night Drive"
    assert updated["bpm"] == 112
    assert updated["category"] == "power"

    # WAV carries raw ID3, so the edit must land on disk, not just in the index.
    assert written is True
    from_disk = core.read_tags(updated["path"])
    assert from_disk["title"] == "Night Drive"
    assert from_disk["artist"] == "The Meters"
    assert from_disk["genre"] == "Funk"
    assert from_disk["bpm"] == 112


def test_edit_survives_a_rescan(scanned):
    """A rescan re-reads tags from disk, so an edit must not be reverted."""
    track = scanned.library()[0]
    scanned.update_track(track["id"], {"title": "Kept", "artist": "Kept Artist"})
    scanned.scan()
    refreshed = scanned.track(track["id"])
    assert refreshed["title"] == "Kept"
    assert refreshed["artist"] == "Kept Artist"


def test_year_is_written_as_a_date_tag(scanned):
    track = scanned.library()[0]
    updated, written = scanned.update_track(track["id"], {"year": "1997"})
    assert written is True
    assert updated["year"] == "1997"
    assert core.read_tags(updated["path"])["year"] == "1997"


def test_edit_rejects_unknown_track(scanned):
    track, written = scanned.update_track("nope", {"title": "x"})
    assert track is None and written is False


# ------------------------------------------------------------------- search

def library_fixture():
    return [
        {"id": "1", "title": "Sunrise", "artist": "Ana Vega", "album": "Dawn",
         "genre": "Pop", "bpm": 128, "duration": 210, "category": "power",
         "lastPlayed": 0, "added": 3},
        {"id": "2", "title": "Midnight Drive", "artist": "Ana Vega", "album": "Dusk",
         "genre": "Synth", "bpm": 96, "duration": 185, "category": "hot",
         "lastPlayed": 0, "added": 2},
        {"id": "3", "title": "Slow Tide", "artist": "Bo Lin", "album": "Ocean",
         "genre": "Ambient", "bpm": 70, "duration": 320, "category": "slow",
         "lastPlayed": 0, "added": 1},
    ]


def test_search_matches_across_fields_case_insensitively():
    library = library_fixture()
    assert [t["id"] for t in core.search_tracks(library, "sunrise")] == ["1"]
    # Same artist, so the default sort falls through to title.
    assert [t["id"] for t in core.search_tracks(library, "ANA")] == ["2", "1"]
    assert [t["id"] for t in core.search_tracks(library, "ambient")] == ["3"]


def test_search_requires_every_term():
    library = library_fixture()
    assert [t["id"] for t in core.search_tracks(library, "ana midnight")] == ["2"]
    assert core.search_tracks(library, "ana ocean") == []


def test_search_filters_combine():
    library = library_fixture()
    assert [t["id"] for t in core.search_tracks(library, genre="Pop")] == ["1"]
    assert [t["id"] for t in core.search_tracks(library, artist="Bo Lin")] == ["3"]
    assert [t["id"] for t in core.search_tracks(library, category="hot")] == ["2"]
    assert [t["id"] for t in core.search_tracks(library, bpm_min=90, bpm_max=130)] \
        == ["2", "1"]


def test_search_sorting():
    library = library_fixture()
    assert [t["id"] for t in core.search_tracks(library, sort="artist")] == ["2", "1", "3"]
    assert [t["id"] for t in core.search_tracks(library, sort="duration")] == ["2", "1", "3"]
    assert [t["id"] for t in core.search_tracks(library, sort="bpm")] == ["3", "2", "1"]
    assert [t["id"] for t in core.search_tracks(library, sort="added")] == ["1", "2", "3"]


def test_search_with_no_query_returns_everything():
    assert len(core.search_tracks(library_fixture())) == 3


# ---------------------------------------------------------------- playlists

def test_playlist_save_and_load_round_trip(scanned):
    ids = [t["id"] for t in scanned.library()][:3]
    saved = scanned.save_playlist("Drive Time", ids)
    assert saved["name"] == "Drive Time"

    loaded = scanned.playlist(saved["id"])
    assert loaded["trackIds"] == ids

    # A fresh Store reading the same folder sees it too.
    reopened = core.Store(scanned.base_dir, data_dir=scanned.data_dir)
    assert [p["name"] for p in reopened.playlists()] == ["Drive Time"]
    assert reopened.playlist(saved["id"])["trackIds"] == ids


def test_playlist_save_updates_instead_of_duplicating(scanned):
    ids = [t["id"] for t in scanned.library()]
    first = scanned.save_playlist("Overnight", ids[:2])
    second = scanned.save_playlist("Overnight", ids[2:4])
    assert first["id"] == second["id"]
    assert len(scanned.playlists()) == 1
    assert scanned.playlist(first["id"])["trackIds"] == ids[2:4]


def test_playlist_delete(scanned):
    playlist = scanned.save_playlist("Temp", [])
    assert scanned.delete_playlist(playlist["id"]) is True
    assert scanned.playlists() == []
    assert scanned.delete_playlist(playlist["id"]) is False


def test_removing_a_track_prunes_it_from_playlists_and_queue(scanned):
    ids = [t["id"] for t in scanned.library()]
    playlist = scanned.save_playlist("Mix", ids)
    scanned.save_queue([{"trackId": i} for i in ids])

    assert scanned.remove_track(ids[0]) is True
    assert ids[0] not in scanned.playlist(playlist["id"])["trackIds"]
    assert ids[0] not in [i["trackId"] for i in scanned.queue()]


# ----------------------------------------------------------------- rotation

def rotation_categories(**overrides):
    base = {
        "id": "power", "name": "Power", "color": "#f00", "spinsPerHour": 4,
        "minArtistGap": 30, "weights": dict(core.DEFAULT_WEIGHTS),
    }
    base.update(overrides)
    return base


def make_track(track_id, artist, category, last_played=0):
    return {"id": track_id, "title": f"Song {track_id}", "artist": artist,
            "genre": "", "bpm": 0, "duration": 200, "category": category,
            "lastPlayed": last_played, "album": ""}


def test_daypart_boundaries():
    assert core.daypart_for_hour(0) == "overnight"
    assert core.daypart_for_hour(5) == "overnight"
    assert core.daypart_for_hour(8) == "morning"
    assert core.daypart_for_hour(12) == "midday"
    assert core.daypart_for_hour(17) == "afternoon"
    assert core.daypart_for_hour(23) == "evening"


def test_daypart_weights_scale_the_hourly_target():
    now = time.time()
    part = core.daypart_for_hour(time.localtime(now).tm_hour)
    heavy = rotation_categories(id="power", spinsPerHour=4,
                                weights={**core.DEFAULT_WEIGHTS, part: 2.0})
    light = rotation_categories(id="slow", spinsPerHour=4,
                                weights={**core.DEFAULT_WEIGHTS, part: 0.25})
    scores = core.category_scores([heavy, light], [], now)
    assert scores["power"] == 8.0
    assert scores["slow"] == 1.0
    assert scores["power"] > scores["slow"] * 4


def test_spins_already_played_this_hour_reduce_the_score():
    now = time.time()
    category = rotation_categories(spinsPerHour=4)
    fresh = core.category_scores([category], [], now)["power"]

    history = [{"trackId": str(i), "artist": "A", "category": "power", "at": now - 60 * i}
               for i in range(1, 3)]
    after_two = core.category_scores([category], history, now)["power"]
    assert after_two < fresh

    # Plays older than an hour do not count against the current hour.
    stale = [{"trackId": "x", "artist": "A", "category": "power", "at": now - 4000}]
    assert core.category_scores([category], stale, now)["power"] == fresh


def test_minimum_artist_gap_is_enforced():
    now = time.time()
    category = rotation_categories(minArtistGap=30)
    library = [make_track("a1", "Repeat Artist", "power"),
               make_track("a2", "Repeat Artist", "power"),
               make_track("b1", "Other Artist", "power")]
    history = [{"trackId": "a1", "artist": "Repeat Artist", "category": "power",
                "at": now - 300}]   # 5 minutes ago, gap is 30

    result = core.pick_next(library, [category], history, now=now,
                            rng=random.Random(7))
    assert result["track"]["artist"] == "Other Artist"
    assert result["relaxed"] is None


def test_artist_gap_expires():
    now = time.time()
    category = rotation_categories(minArtistGap=30)
    library = [make_track("a2", "Repeat Artist", "power")]
    old = [{"trackId": "a1", "artist": "Repeat Artist", "category": "power",
            "at": now - 3600}]   # an hour ago, well past the 30 minute gap

    result = core.pick_next(library, [category], old, now=now, rng=random.Random(1))
    assert result["track"]["id"] == "a2"
    assert result["relaxed"] is None


def test_artist_gap_is_relaxed_rather_than_stalling():
    now = time.time()
    category = rotation_categories(minArtistGap=120)
    library = [make_track("a1", "Only Artist", "power")]
    history = [{"trackId": "a1", "artist": "Only Artist", "category": "power",
                "at": now - 60}]

    result = core.pick_next(library, [category], history, now=now, rng=random.Random(3))
    assert result is not None
    assert result["track"]["id"] == "a1"
    assert result["relaxed"] in ("repeat-window", "artist-gap")


def test_categories_with_no_tracks_are_skipped():
    now = time.time()
    categories = [rotation_categories(id="power", spinsPerHour=10),
                  rotation_categories(id="slow", spinsPerHour=1)]
    library = [make_track("s1", "Someone", "slow")]

    for seed in range(6):
        result = core.pick_next(library, categories, [], now=now, rng=random.Random(seed))
        assert result["category"] == "slow"


def test_rotation_does_not_poach_uncategorised_tracks():
    """A categorised library must never quietly play off-rotation material."""
    now = time.time()
    category = rotation_categories(minArtistGap=0)
    library = [make_track("in1", "In Rotation", "power"),
               make_track("out1", "Not In Rotation", ""),
               make_track("out2", "Also Out", "")]
    for seed in range(10):
        result = core.pick_next(library, [category], [], now=now, rng=random.Random(seed))
        assert result["track"]["id"] == "in1"


def test_rotation_stalls_when_every_category_is_switched_off():
    library = [make_track("t1", "Artist", "power")]
    silent = rotation_categories(spinsPerHour=0)
    assert core.pick_next(library, [silent], []) is None


def test_uncategorised_library_still_plays_and_says_so():
    library = [make_track("u1", "Nobody", "")]
    result = core.pick_next(library, [rotation_categories()], [], rng=random.Random(2))
    assert result["track"]["id"] == "u1"
    assert result["relaxed"] == "uncategorised"


def test_pick_prefers_the_least_recently_played():
    now = time.time()
    category = rotation_categories(minArtistGap=0)
    library = [make_track(f"t{i}", f"Artist {i}", "power", last_played=now - i * 1000)
               for i in range(1, 11)]
    # t10 is coldest, t1 the most recent; the pool window is the coldest 40%.
    chosen = {core.pick_next(library, [category], [], now=now,
                             rng=random.Random(seed))["track"]["id"]
              for seed in range(30)}
    assert chosen <= {"t7", "t8", "t9", "t10"}


def test_plan_next_returns_distinct_tracks():
    now = time.time()
    category = rotation_categories(minArtistGap=0)
    library = [make_track(f"t{i}", f"Artist {i}", "power") for i in range(12)]
    picks = core.plan_next(library, [category], [], count=5, now=now,
                           rng=random.Random(11))
    ids = [p["track"]["id"] for p in picks]
    assert len(ids) == 5
    assert len(set(ids)) == 5


def test_plan_next_respects_artist_gap_within_its_own_plan():
    now = time.time()
    category = rotation_categories(minArtistGap=60)
    library = ([make_track(f"x{i}", "Same Artist", "power") for i in range(4)]
               + [make_track(f"y{i}", f"Other {i}", "power") for i in range(4)])
    picks = core.plan_next(library, [category], [], count=4, now=now,
                           rng=random.Random(5))
    artists = [p["track"]["artist"] for p in picks]
    assert artists.count("Same Artist") <= 1


def test_plan_next_honours_exclusions():
    now = time.time()
    category = rotation_categories(minArtistGap=0)
    library = [make_track(f"t{i}", f"Artist {i}", "power") for i in range(6)]
    picks = core.plan_next(library, [category], [], count=3, now=now,
                           exclude_ids=["t0", "t1", "t2"], rng=random.Random(9))
    assert {p["track"]["id"] for p in picks}.isdisjoint({"t0", "t1", "t2"})


def test_pick_next_on_empty_library_returns_none():
    assert core.pick_next([], [rotation_categories()], []) is None


def test_recording_a_play_updates_counters_and_history(scanned):
    track = scanned.library()[0]
    scanned.record_play(track["id"])
    scanned.record_play(track["id"])

    refreshed = scanned.track(track["id"])
    assert refreshed["playCount"] == 2
    assert refreshed["lastPlayed"] > 0

    history = scanned.history()
    assert len(history) == 2
    assert history[0]["trackId"] == track["id"]
    assert history[0]["at"] >= history[1]["at"]


def test_saving_categories_clears_stale_assignments(scanned):
    library = scanned.library()
    library[0]["category"] = "power"
    scanned.save_library(library)

    scanned.save_categories([rotation_categories(id="hot", name="Hot")])
    assert scanned.track(library[0]["id"])["category"] == ""


# -------------------------------------------------------------------- store

def test_config_is_clamped(store):
    config = store.save_config({"crossfade": 99, "volume": -3, "autoDjMinQueue": 0})
    assert config["crossfade"] == 12.0
    assert config["volume"] == 0.0
    assert config["autoDjMinQueue"] == 1


def test_corrupt_json_is_quarantined_not_fatal(store):
    with open(store.path("playlists"), "w", encoding="utf-8") as handle:
        handle.write("{not json")
    assert store.playlists() == []
    assert os.path.exists(store.path("playlists") + ".corrupt")


def test_schedule_entries_are_validated(store):
    saved = store.save_schedule([
        {"time": "07:30", "action": "playlist", "targetId": "abc"},
        {"time": "25:00", "action": "playlist", "targetId": "bad"},   # dropped
        {"time": "18:00", "action": "nonsense", "targetId": "x", "repeat": "once"},
    ])
    assert [e["time"] for e in saved] == ["07:30", "18:00"]
    assert saved[0]["repeat"] == "daily"
    assert saved[1]["action"] == "playlist"       # unknown action falls back
    assert saved[1]["repeat"] == "once"
    assert all(e["id"] for e in saved)
