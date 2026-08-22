"""Tests for playlistgen rotation rules and playlist generation."""

import json
from pathlib import Path

from click.testing import CliRunner

from broadcast.playlistgen import (
    RotationEngine,
    scan_folder,
    load_rotation,
    write_m3u,
    _parse_slot,
    _infer_daypart,
    cli,
)


class TestRotationEngine:
    """Test the core rotation engine logic."""

    def test_artist_gap_respected(self, music_library, rotation_config):
        """Tracks by the same artist must be separated by at least artist_gap tracks."""
        tracks = scan_folder(str(music_library))
        rotation = load_rotation(str(rotation_config))
        engine = RotationEngine(tracks, rotation, seed=42)
        playlist = engine.generate(target_duration=600)

        # Check artist gap rule (gap=2 means at least 2 tracks between same artist)
        artist_positions: dict[str, list[int]] = {}
        for i, track in enumerate(playlist):
            artist = (track.get("artist") or "").lower()
            artist_positions.setdefault(artist, []).append(i)

        gap = engine.artist_gap
        for artist, positions in artist_positions.items():
            if len(positions) > 1:
                for i in range(1, len(positions)):
                    diff = positions[i] - positions[i - 1]
                    assert diff > gap, (
                        f"Artist '{artist}' appears at positions {positions[i-1]} "
                        f"and {positions[i]} (gap {diff} <= {gap})"
                    )

    def test_category_gap_respected(self, music_library, rotation_config):
        """No two consecutive tracks should be from the same category."""
        tracks = scan_folder(str(music_library))
        rotation = load_rotation(str(rotation_config))
        engine = RotationEngine(tracks, rotation, seed=42)
        playlist = engine.generate(target_duration=600)

        for i in range(1, len(playlist)):
            cat_prev = playlist[i - 1].get("category")
            cat_curr = playlist[i].get("category")
            if cat_prev and cat_curr:
                assert cat_prev != cat_curr, (
                    f"Consecutive tracks at positions {i-1} and {i} "
                    f"both in category '{cat_curr}'"
                )

    def test_deterministic_with_seed(self, music_library, rotation_config):
        """Same seed produces the same playlist."""
        tracks = scan_folder(str(music_library))
        rotation = load_rotation(str(rotation_config))

        engine1 = RotationEngine(tracks, rotation, seed=123)
        pl1 = engine1.generate(target_duration=300)

        engine2 = RotationEngine(tracks, rotation, seed=123)
        pl2 = engine2.generate(target_duration=300)

        assert len(pl1) == len(pl2)
        for i in range(len(pl1)):
            assert pl1[i]["path"] == pl2[i]["path"], (
                f"Track {i} differs: {pl1[i]['path']} vs {pl2[i]['path']}"
            )

    def test_different_seeds_different_playlists(self, music_library, rotation_config):
        """Different seeds should (almost always) produce different playlists."""
        tracks = scan_folder(str(music_library))
        rotation = load_rotation(str(rotation_config))

        engine1 = RotationEngine(tracks, rotation, seed=1)
        pl1 = engine1.generate(target_duration=300)

        engine2 = RotationEngine(tracks, rotation, seed=999)
        pl2 = engine2.generate(target_duration=300)

        # At least one track should differ
        paths1 = [t["path"] for t in pl1]
        paths2 = [t["path"] for t in pl2]
        assert paths1 != paths2, "Different seeds produced identical playlists"

    def test_title_gap_respected(self, music_library, rotation_config):
        """Same title should not appear in consecutive positions."""
        tracks = scan_folder(str(music_library))
        rotation = load_rotation(str(rotation_config))
        engine = RotationEngine(tracks, rotation, seed=42)
        playlist = engine.generate(target_duration=600)

        for i in range(1, len(playlist)):
            title_prev = (playlist[i - 1].get("title") or "").lower()
            title_curr = (playlist[i].get("title") or "").lower()
            if title_prev and title_curr:
                assert title_prev != title_curr, (
                    f"Same title '{title_curr}' at consecutive positions {i-1} and {i}"
                )

    def test_empty_library(self):
        """Engine with no tracks returns empty playlist."""
        engine = RotationEngine([], {"categories": {}, "rules": {}})
        assert engine.generate(target_duration=3600) == []

    def test_daypart_weights_applied(self, music_library, rotation_config):
        """Daypart weights affect the scoring of categories."""
        tracks = scan_folder(str(music_library))
        rotation = load_rotation(str(rotation_config))

        # Morning boosts A (weight 1.5) and reduces C (weight 0.8)
        engine_morning = RotationEngine(tracks, rotation, seed=42, daypart="Morning")
        # Evening reduces A (weight 0.8) and boosts C (weight 1.2)
        engine_evening = RotationEngine(tracks, rotation, seed=42, daypart="Evening")

        # Check that the weights are different
        assert engine_morning._sph("A") > engine_evening._sph("A")
        assert engine_morning._sph("C") < engine_evening._sph("C")


class TestSlotParsing:
    def test_parse_minutes(self):
        assert _parse_slot("30min") == 1800

    def test_parse_hours(self):
        assert _parse_slot("1h") == 3600
        assert _parse_slot("2h") == 7200

    def test_parse_seconds(self):
        assert _parse_slot("90s") == 90

    def test_parse_bare_number(self):
        assert _parse_slot("45") == 2700  # 45 minutes

    def test_parse_uppercase(self):
        assert _parse_slot("1H") == 3600
        assert _parse_slot("30MIN") == 1800


class TestInferDaypart:
    def test_morning(self):
        rotation = {
            "dayparts": {
                "Morning": {"start": "06:00", "end": "12:00", "weights": {}},
                "Evening": {"start": "12:00", "end": "18:00", "weights": {}},
                "Overnight": {"start": "18:00", "end": "06:00", "weights": {}},
            }
        }
        assert _infer_daypart(rotation, 8) == "Morning"
        assert _infer_daypart(rotation, 14) == "Evening"
        assert _infer_daypart(rotation, 22) == "Overnight"
        assert _infer_daypart(rotation, 2) == "Overnight"

    def test_no_dayparts(self):
        assert _infer_daypart({"dayparts": {}}, 12) is None


class TestM3UOutput:
    def test_write_m3u(self, tmp_path):
        tracks = [
            {"path": "/music/song1.mp3", "artist": "Artist A", "title": "Title 1", "duration": 200},
            {"path": "/music/song2.mp3", "artist": "Artist B", "title": "Title 2", "duration": 180},
        ]
        out = str(tmp_path / "test.m3u")
        write_m3u(tracks, out)

        content = Path(out).read_text()
        assert "#EXTM3U" in content
        assert "#EXTINF:200,Artist A - Title 1" in content
        assert "/music/song1.mp3" in content
        assert "#EXTINF:180,Artist B - Title 2" in content
        assert "/music/song2.mp3" in content


class TestCLI:
    def test_cli_generates_playlist(self, music_library, rotation_config, tmp_path):
        """End-to-end: CLI generates M3U + JSON sidecar."""
        runner = CliRunner()
        out_m3u = str(tmp_path / "out.m3u")
        result = runner.invoke(cli, [
            str(music_library),
            "--rotation", str(rotation_config),
            "--seed", "42",
            "--slot", "30min",
            "-o", out_m3u,
        ])
        assert result.exit_code == 0, result.output
        assert Path(out_m3u).exists()
        assert Path(out_m3u).with_suffix(".json").exists()

    def test_cli_missing_folder(self, tmp_path):
        """Missing folder should exit with code 1."""
        runner = CliRunner()
        result = runner.invoke(cli, [
            str(tmp_path / "nonexistent"),
            "--rotation", str(tmp_path / "rot.json"),
        ])
        assert result.exit_code == 1

    def test_cli_dump_library(self, music_library, tmp_path):
        """--dump-library writes a library.json and exits."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(cli, [
                str(music_library),
                "--dump-library",
            ])
            assert result.exit_code == 0, result.output
            assert Path("library.json").exists()
            data = json.loads(Path("library.json").read_text())
            assert len(data) > 0

def test_category_keys_case_insensitive(music_library, rotation_config):
    """Config keys like 'Power' must match detected 'POWER' categories."""
    import json as _json
    tracks = scan_folder(str(music_library))
    rotation = _json.loads(rotation_config.read_text())
    # Rewrite config with title-case keys (what a human/UI would write)
    rotation["categories"] = {k.title(): v for k, v in rotation["categories"].items()}
    dayparts = rotation.get("dayparts", {})
    for dp in dayparts.values():
        dp["weights"] = {k.title(): v for k, v in dp["weights"].items()}
    engine = RotationEngine(tracks, rotation, seed=42)
    # _sph must use real spins-per-hour, not the 0.5 uncategorised fallback
    detected = next(t for t in tracks if t.get("category"))
    sph = engine._sph(detected["category"])
    base = rotation["categories"][detected["category"].title()]["sph"]
    assert sph == base * 1.0 or sph > 0.5, (
        f"expected sph={base} for {detected['category']}, got {sph} (fallback 0.5 means mismatch)"
    )
