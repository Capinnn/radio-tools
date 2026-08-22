"""Tests for logbook logging and stats."""

import json
from datetime import datetime, date
from pathlib import Path

from click.testing import CliRunner

from broadcast.logbook import (
    _load_log,
    _save_log,
    compute_hourly_stats,
    render_heatmap,
    export_csv,
    cli,
)


class TestLogbook:
    def test_load_empty_log(self, tmp_path):
        """Loading a nonexistent log returns []."""
        assert _load_log(str(tmp_path / "nonexistent.json")) == []

    def test_save_and_load(self, tmp_path):
        """Save and reload a log."""
        path = str(tmp_path / "log.json")
        entries = [
            {"timestamp": "2026-08-21T14:00:00", "artist": "A", "title": "B",
             "category": "A", "duration": 200, "source": "manual", "path": "/x.mp3"},
        ]
        _save_log(path, entries)
        loaded = _load_log(path)
        assert len(loaded) == 1
        assert loaded[0]["artist"] == "A"


class TestHourlyStats:
    def test_compute_stats_basic(self):
        """Basic hourly stats computation."""
        today = date.today().isoformat()
        entries = [
            {"timestamp": f"{today}T14:05:00", "category": "A", "source": "manual"},
            {"timestamp": f"{today}T14:15:00", "category": "A", "source": "manual"},
            {"timestamp": f"{today}T14:45:00", "category": "B", "source": "playlistgen"},
            {"timestamp": f"{today}T15:05:00", "category": "B", "source": "playlistgen"},
        ]
        stats = compute_hourly_stats(entries, today)
        assert stats["total"] == 4
        assert stats["hours"][14] == 3
        assert stats["hours"][15] == 1
        assert stats["by_category"]["A"] == 2
        assert stats["by_category"]["B"] == 2
        assert stats["by_source"]["manual"] == 2
        assert stats["by_source"]["playlistgen"] == 2

    def test_compute_stats_filters_by_date(self):
        """Stats should only count entries for the specified date."""
        entries = [
            {"timestamp": "2026-08-20T14:00:00", "category": "A", "source": "manual"},
            {"timestamp": "2026-08-21T14:00:00", "category": "B", "source": "manual"},
            {"timestamp": "2026-08-21T15:00:00", "category": "B", "source": "manual"},
        ]
        stats = compute_hourly_stats(entries, "2026-08-21")
        assert stats["total"] == 2
        assert stats["hours"][14] == 1
        assert stats["hours"][15] == 1

    def test_compute_stats_empty(self):
        """Empty entries produce zero stats."""
        stats = compute_hourly_stats([], "2026-08-21")
        assert stats["total"] == 0
        assert all(v == 0 for v in stats["hours"].values())

    def test_compute_stats_all_24_hours_present(self):
        """The hours dict should have all 24 hours."""
        stats = compute_hourly_stats([], "2026-08-21")
        for h in range(24):
            assert h in stats["hours"]

    def test_heatmap_render(self):
        """Heat map should render without errors and contain key labels."""
        today = date.today().isoformat()
        entries = [
            {"timestamp": f"{today}T09:00:00", "category": "A", "source": "manual"},
            {"timestamp": f"{today}T09:30:00", "category": "B", "source": "studio"},
            {"timestamp": f"{today}T10:00:00", "category": "A", "source": "manual"},
        ]
        stats = compute_hourly_stats(entries, today)
        text = render_heatmap(stats)
        assert "LOGBOOK STATS" in text
        assert "Hourly heat map" in text
        assert "By category" in text
        assert "By source" in text
        assert "09:00" in text

    def test_heatmap_empty(self):
        """Empty stats should still render."""
        stats = compute_hourly_stats([], "2026-08-21")
        text = render_heatmap(stats)
        assert "LOGBOOK STATS" in text
        assert "Total tracks played: 0" in text


class TestCSVExport:
    def test_export_all(self, tmp_path):
        """Export all entries to CSV."""
        entries = [
            {"timestamp": "2026-08-21T14:00:00", "artist": "A", "title": "B",
             "category": "A", "duration": 200, "source": "manual", "path": "/x.mp3"},
            {"timestamp": "2026-08-21T15:00:00", "artist": "C", "title": "D",
             "category": "B", "duration": 180, "source": "studio", "path": "/y.mp3"},
        ]
        csv_path = str(tmp_path / "export.csv")
        count = export_csv(entries, csv_path)
        assert count == 2
        content = Path(csv_path).read_text()
        assert "timestamp" in content  # header
        assert "A" in content  # artist
        assert "/x.mp3" in content

    def test_export_by_date(self, tmp_path):
        """Export filtered by date."""
        entries = [
            {"timestamp": "2026-08-20T14:00:00", "artist": "Old", "title": "X",
             "category": "A", "duration": 200, "source": "manual", "path": "/old.mp3"},
            {"timestamp": "2026-08-21T14:00:00", "artist": "New", "title": "Y",
             "category": "B", "duration": 180, "source": "studio", "path": "/new.mp3"},
        ]
        csv_path = str(tmp_path / "export.csv")
        count = export_csv(entries, csv_path, target_date="2026-08-21")
        assert count == 1
        content = Path(csv_path).read_text()
        assert "New" in content
        assert "Old" not in content


class TestCLI:
    def test_cli_record(self, music_library, tmp_path):
        """Record a track and verify it's saved."""
        runner = CliRunner()
        log_path = str(tmp_path / "log.json")
        mp3 = list(music_library.glob("A/*.wav"))[0]
        result = runner.invoke(cli, [
            "--file", log_path,
            "record", str(mp3),
            "--category", "A",
            "--source", "playlistgen",
        ])
        assert result.exit_code == 0, result.output
        assert "Recorded" in result.output
        # Verify the log file
        entries = _load_log(log_path)
        assert len(entries) == 1
        assert entries[0]["category"] == "A"

    def test_cli_today_empty(self, tmp_path):
        runner = CliRunner()
        log_path = str(tmp_path / "log.json")
        result = runner.invoke(cli, ["--file", log_path, "today"])
        assert result.exit_code == 0
        assert "empty" in result.output.lower()

    def test_cli_today_with_entries(self, tmp_path):
        """Today command should show entries for today's date."""
        log_path = str(tmp_path / "log.json")
        today = date.today().isoformat()
        entries = [
            {"timestamp": f"{today}T09:00:00", "artist": "Artist A", "title": "Song X",
             "category": "A", "duration": 200, "source": "manual", "path": "/x.mp3"},
            {"timestamp": f"{today}T10:30:00", "artist": "Artist B", "title": "Song Y",
             "category": "B", "duration": 180, "source": "studio", "path": "/y.mp3"},
        ]
        _save_log(log_path, entries)

        runner = CliRunner()
        result = runner.invoke(cli, ["--file", log_path, "today"])
        assert result.exit_code == 0, result.output
        assert "Artist A" in result.output
        assert "Song Y" in result.output

    def test_cli_stats(self, tmp_path):
        """Stats command should show the heat map."""
        log_path = str(tmp_path / "log.json")
        today = date.today().isoformat()
        entries = [
            {"timestamp": f"{today}T09:00:00", "artist": "A", "title": "B",
             "category": "A", "duration": 200, "source": "manual", "path": "/x.mp3"},
            {"timestamp": f"{today}T09:30:00", "artist": "C", "title": "D",
             "category": "B", "duration": 180, "source": "studio", "path": "/y.mp3"},
        ]
        _save_log(log_path, entries)

        runner = CliRunner()
        result = runner.invoke(cli, ["--file", log_path, "stats", "--hour"])
        assert result.exit_code == 0, result.output
        assert "LOGBOOK STATS" in result.output
        assert "heat map" in result.output.lower()

    def test_cli_export_csv(self, tmp_path):
        """Export command should write CSV."""
        log_path = str(tmp_path / "log.json")
        today = date.today().isoformat()
        entries = [
            {"timestamp": f"{today}T09:00:00", "artist": "A", "title": "B",
             "category": "A", "duration": 200, "source": "manual", "path": "/x.mp3"},
        ]
        _save_log(log_path, entries)

        csv_path = str(tmp_path / "out.csv")
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--file", log_path,
            "export", "--csv", csv_path,
        ])
        assert result.exit_code == 0, result.output
        assert Path(csv_path).exists()
        content = Path(csv_path).read_text()
        assert "timestamp" in content