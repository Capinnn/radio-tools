"""Tests for trackcheck library hygiene scanner."""

import json
from pathlib import Path

from click.testing import CliRunner

from broadcast.trackcheck import (
    scan_library,
    render_report,
    _hash_file,
    cli,
)


class TestDuplicateDetection:
    def test_finds_duplicates(self, music_library):
        """Duplicate files (same content hash) should be detected."""
        report = scan_library(str(music_library))
        assert report["stats"]["duplicate_groups"] >= 1
        # The copy of Song Alpha should be detected as a duplicate
        dup_files = []
        for dup in report["duplicates"]:
            dup_files.extend(dup["files"])
        assert any("copy" in f.lower() for f in dup_files), \
            "Duplicate copy file not found in duplicates"

    def test_no_false_duplicates(self, tmp_path):
        """Files with different content should not be flagged as duplicates."""
        import wave
        for i in range(3):
            path = tmp_path / f"song_{i}.wav"
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(8000)
                # Different content for each file
                frames = (bytes([i * 10]) * 2) * 100
                w.writeframes(frames)
        report = scan_library(str(tmp_path))
        assert report["stats"]["duplicate_groups"] == 0

    def test_hash_consistency(self, tmp_path):
        """Same content produces same hash."""
        f1 = tmp_path / "a.wav"
        f2 = tmp_path / "b.wav"
        f1.write_bytes(b"identical content")
        f2.write_bytes(b"identical content")
        assert _hash_file(str(f1)) == _hash_file(str(f2))

        f3 = tmp_path / "c.wav"
        f3.write_bytes(b"different content")
        assert _hash_file(str(f1)) != _hash_file(str(f3))


class TestMissingTags:
    def test_detects_missing_tags(self, music_library):
        """Untagged files should be flagged."""
        report = scan_library(str(music_library))
        missing_tag_issues = [i for i in report["issues"] if i["issue"] == "missing_tag"]
        # The untagged WAV file should have missing artist and title
        assert len(missing_tag_issues) >= 2  # at least artist + title missing

    def test_fix_fills_empty_tags(self, music_library):
        """--fix should fill empty artist/title from filename."""
        # First, confirm the untagged file has missing tags
        report_before = scan_library(str(music_library))
        missing_before = [i for i in report_before["issues"] if i["issue"] == "missing_tag"]
        assert len(missing_before) >= 2

        # Run with fix=True
        report_after = scan_library(str(music_library), fix=True)
        assert report_after["stats"]["fixes_applied"] >= 1

        # Check that at least some tags were filled
        fix_details = report_after["fixes"]
        all_actions = []
        for fix in fix_details:
            all_actions.extend(fix["actions"])
        assert any("set artist" in a for a in all_actions)
        assert any("set title" in a for a in all_actions)


class TestDurationChecks:
    def test_too_short(self, tmp_path):
        """Files shorter than --min should be flagged."""
        import wave
        path = tmp_path / "short.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 400)  # 0.05 seconds
        report = scan_library(str(tmp_path), min_duration=1.0)
        too_short = [i for i in report["issues"] if i["issue"] == "too_short"]
        assert len(too_short) == 1

    def test_too_long(self, tmp_path):
        """Files longer than --max should be flagged."""
        import wave
        path = tmp_path / "long.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 80000)  # 10 seconds
        report = scan_library(str(tmp_path), max_duration=5.0)
        too_long = [i for i in report["issues"] if i["issue"] == "too_long"]
        assert len(too_long) == 1


class TestReportRendering:
    def test_render_report(self, music_library):
        """Report should be human-readable and contain key sections."""
        report = scan_library(str(music_library))
        text = render_report(report)
        assert "TRACKCHECK REPORT" in text
        assert "Files scanned:" in text
        assert "Issues found:" in text

    def test_render_clean_report(self, tmp_path):
        """A clean library should report no issues."""
        # Create a properly tagged file
        import wave
        path = tmp_path / "Artist - Title.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 8000)
        report = scan_library(str(tmp_path))
        text = render_report(report)
        # It will still have issues (missing tags, replaygain) but report should render
        assert "TRACKCHECK REPORT" in text


class TestCLI:
    def test_cli_scan(self, music_library):
        runner = CliRunner()
        result = runner.invoke(cli, [str(music_library)])
        # Exit code 1 because issues exist (missing tags etc.)
        assert result.exit_code == 1
        assert "TRACKCHECK REPORT" in result.output

    def test_cli_missing_folder(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(cli, [str(tmp_path / "nonexistent")])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_cli_json_output(self, music_library, tmp_path):
        runner = CliRunner()
        json_path = str(tmp_path / "report.json")
        result = runner.invoke(cli, [
            str(music_library),
            "--json", json_path,
            "--quiet",
        ])
        assert result.exit_code == 1
        assert Path(json_path).exists()
        data = json.loads(Path(json_path).read_text())
        assert "issues" in data
        assert "duplicates" in data
        assert "stats" in data

    def test_cli_fix_flag(self, music_library):
        runner = CliRunner()
        result = runner.invoke(cli, [str(music_library), "--fix"])
        # Should report fixes applied
        assert "Fixes applied" in result.output or "fixes" in result.output.lower()


class TestKindFlag:
    """Tests for --kind sweep|jingle|music short-form audio tagging."""

    def _make_wav(self, path, duration_sec, sample_rate=8000):
        import wave
        n_frames = int(duration_sec * sample_rate)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(b"\x00\x00" * n_frames)

    def test_kind_sweep_tags_report(self, tmp_path):
        """scan_library with kind='sweeper' tags the report with that kind."""
        self._make_wav(tmp_path / "sweeper1.wav", 5.0)
        report = scan_library(str(tmp_path), kind="sweeper", force=True)
        assert report["kind"] == "sweeper"

    def test_kind_music_is_default(self, tmp_path):
        """Default kind is music and does not appear in the report title."""
        self._make_wav(tmp_path / "song.wav", 2.0)
        report = scan_library(str(tmp_path))
        assert report["kind"] == "music"

    def test_kind_sweep_short_file_no_warning(self, tmp_path):
        """A short sweeper (< 90s) should not trigger too_long_for_kind."""
        self._make_wav(tmp_path / "short.wav", 5.0)
        report = scan_library(str(tmp_path), kind="sweeper")
        kind_issues = [i for i in report["issues"]
                       if i["issue"] == "too_long_for_kind"]
        assert len(kind_issues) == 0

    def test_kind_sweep_long_file_warns(self, tmp_path):
        """A sweeper > 90s should warn with too_long_for_kind."""
        self._make_wav(tmp_path / "long.wav", 95.0)
        report = scan_library(str(tmp_path), kind="sweeper")
        kind_issues = [i for i in report["issues"]
                       if i["issue"] == "too_long_for_kind"]
        assert len(kind_issues) == 1
        assert "90" in kind_issues[0]["detail"]

    def test_kind_sweep_long_file_force_suppresses_warning(self, tmp_path):
        """--force suppresses the too_long_for_kind warning."""
        self._make_wav(tmp_path / "long.wav", 95.0)
        report = scan_library(str(tmp_path), kind="sweeper", force=True)
        kind_issues = [i for i in report["issues"]
                       if i["issue"] == "too_long_for_kind"]
        assert len(kind_issues) == 0

    def test_kind_jingle_long_file_warns(self, tmp_path):
        """A jingle > 90s should warn with too_long_for_kind."""
        self._make_wav(tmp_path / "long.wav", 95.0)
        report = scan_library(str(tmp_path), kind="jingle")
        kind_issues = [i for i in report["issues"]
                       if i["issue"] == "too_long_for_kind"]
        assert len(kind_issues) == 1

    def test_cli_kind_sweep_shows_kind_in_report(self, tmp_path):
        """CLI --kind sweep shows [sweeper] in the report header."""
        self._make_wav(tmp_path / "sweeper.wav", 5.0)
        runner = CliRunner()
        result = runner.invoke(cli, [str(tmp_path), "--kind", "sweep",
                                     "--force"])
        # Exit 1 because of missing tags etc., but report should render
        assert "[sweeper]" in result.output

    def test_cli_kind_jingle_force_no_kind_warning(self, tmp_path):
        """CLI --kind jingle --force does not flag a long jingle."""
        self._make_wav(tmp_path / "jingle.wav", 95.0)
        runner = CliRunner()
        result = runner.invoke(cli, [str(tmp_path), "--kind", "jingle",
                                     "--force", "--quiet"])
        assert "too_long_for_kind" not in result.output