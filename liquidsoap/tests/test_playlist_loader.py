import json
from pathlib import Path

import pytest

from lib.playlist_loader import (
    PlaylistValidationError,
    load_playlist_sidecar,
    validate_playlist_pair,
)


FIXTURES = Path(__file__).parent / "fixtures"


def test_playlistgen_sidecar_is_valid_loader_input():
    sidecar = load_playlist_sidecar(FIXTURES / "playlist.json")

    assert sidecar.generated_at.isoformat() == "2026-08-21T14:00:00"
    assert sidecar.seed == 42
    assert sidecar.daypart == "Morning"
    assert sidecar.target_duration == 1800
    assert [track.position for track in sidecar.tracks] == [1, 2]
    assert [track.path for track in sidecar.tracks] == [
        "/music/artist - title.mp3",
        "/music/other.mp3",
    ]


def test_playlistgen_m3u_and_sidecar_paths_match():
    sidecar = validate_playlist_pair(
        FIXTURES / "playlist.json", FIXTURES / "playlist.m3u"
    )
    assert len(sidecar.tracks) == 2


def test_loader_rejects_incorrect_duration(tmp_path):
    payload = json.loads((FIXTURES / "playlist.json").read_text(encoding="utf-8"))
    payload["actual_duration"] = 999
    bad_sidecar = tmp_path / "playlist.json"
    bad_sidecar.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PlaylistValidationError, match="actual_duration"):
        load_playlist_sidecar(bad_sidecar)


def test_loader_rejects_m3u_order_mismatch(tmp_path):
    m3u = tmp_path / "playlist.m3u"
    m3u.write_text("#EXTM3U\n/music/other.mp3\n", encoding="utf-8")

    with pytest.raises(PlaylistValidationError, match="do not match"):
        validate_playlist_pair(FIXTURES / "playlist.json", m3u)

