"""Tests for schedule grid logic and ASCII rendering."""

import json
from datetime import datetime, timedelta
from pathlib import Path

from click.testing import CliRunner

from broadcast.schedule import (
    load_schedule,
    save_schedule,
    get_daypart_for,
    get_current_daypart,
    get_next_daypart,
    render_ascii_grid,
    cli,
    DEFAULT_GRID,
    DEFAULT_DAYPARTS,
    DAYS,
    _hour_to_block,
    _day_to_index,
)


class TestGridLogic:
    def test_hour_to_block(self):
        assert _hour_to_block(0) == 0
        assert _hour_to_block(2) == 0
        assert _hour_to_block(3) == 1
        assert _hour_to_block(6) == 2
        assert _hour_to_block(12) == 4
        assert _hour_to_block(23) == 7

    def test_day_to_index(self):
        assert _day_to_index("mon") == 0
        assert _day_to_index("MON") == 0
        assert _day_to_index("Friday") == 4
        assert _day_to_index("sun") == 6

    def test_get_daypart_for(self, schedule_config):
        sched = load_schedule(str(schedule_config))
        # Monday 08:00 → block 2 → "Morning"
        assert get_daypart_for(sched, "mon", 8) == "Morning"
        # Monday 14:00 → block 4 → "Evening"
        assert get_daypart_for(sched, "mon", 14) == "Evening"
        # Monday 22:00 → block 7 → "Overnight"
        assert get_daypart_for(sched, "mon", 22) == "Overnight"
        # Monday 02:00 → block 0 → "Overnight"
        assert get_daypart_for(sched, "mon", 2) == "Overnight"

    def test_get_daypart_for_all_days(self, schedule_config):
        """Every day in the test schedule has the same grid."""
        sched = load_schedule(str(schedule_config))
        for day in DAYS:
            assert get_daypart_for(sched, day, 8) == "Morning"
            assert get_daypart_for(sched, day, 14) == "Evening"

    def test_get_current_daypart(self, schedule_config):
        """Test current daypart lookup with a known time."""
        sched = load_schedule(str(schedule_config))
        # Monday at 10:00 → block 3 → "Morning"
        test_time = datetime(2026, 8, 24, 10, 0, 0)  # Monday
        name, config = get_current_daypart(sched, test_time)
        assert name == "Morning"
        assert config is not None
        assert config["start"] == "06:00"

    def test_get_current_daypart_overnight(self, schedule_config):
        """Overnight daypart at 02:00."""
        sched = load_schedule(str(schedule_config))
        test_time = datetime(2026, 8, 24, 2, 0, 0)  # Monday 2am
        name, config = get_current_daypart(sched, test_time)
        assert name == "Overnight"

    def test_get_next_daypart_transition(self, schedule_config):
        """Test that next daypart is found at a transition boundary."""
        sched = load_schedule(str(schedule_config))
        # Monday at 11:00 → Morning, next should be Evening at 12:00
        test_time = datetime(2026, 8, 24, 11, 0, 0)
        name, config = get_current_daypart(sched, test_time)
        assert name == "Morning"

        next_name, next_time, _ = get_next_daypart(sched, test_time)
        assert next_name == "Evening"
        assert next_time.hour == 12

    def test_get_next_daypart_from_evening(self, schedule_config):
        """Next daypart from Evening should be Overnight."""
        sched = load_schedule(str(schedule_config))
        test_time = datetime(2026, 8, 24, 16, 0, 0)
        next_name, next_time, _ = get_next_daypart(sched, test_time)
        assert next_name == "Overnight"
        assert next_time.hour == 18


class TestASCIIRendering:
    def test_render_grid_contains_all_days(self, schedule_config):
        sched = load_schedule(str(schedule_config))
        rendered = render_ascii_grid(sched)
        for label in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
            assert label in rendered

    def test_render_grid_contains_time_headers(self, schedule_config):
        sched = load_schedule(str(schedule_config))
        rendered = render_ascii_grid(sched)
        assert "00-03" in rendered
        assert "06-09" in rendered
        assert "18-21" in rendered

    def test_render_grid_contains_legend(self, schedule_config):
        sched = load_schedule(str(schedule_config))
        rendered = render_ascii_grid(sched)
        assert "Legend" in rendered
        assert "Morning" in rendered
        assert "rotation.json" in rendered


class TestCLI:
    def test_init_creates_file(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(cli, ["init"])
            assert result.exit_code == 0, result.output
            assert Path("schedule.json").exists()
            data = json.loads(Path("schedule.json").read_text())
            assert "dayparts" in data
            assert "grid" in data

    def test_init_no_overwrite(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            # Create first
            runner.invoke(cli, ["init"])
            # Try again without --force
            result = runner.invoke(cli, ["init"])
            assert result.exit_code == 1

    def test_show_outputs_grid(self, schedule_config, tmp_path):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--file", str(schedule_config),
            "show",
        ])
        assert result.exit_code == 0, result.output
        assert "Mon" in result.output
        assert "Morning" in result.output

    def test_show_missing_file(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--file", str(tmp_path / "nonexistent.json"),
            "show",
        ])
        assert result.exit_code == 1

    def test_next_command(self, schedule_config):
        runner = CliRunner()
        # Use a specific time: Monday 2026-08-24 10:00
        result = runner.invoke(cli, [
            "--file", str(schedule_config),
            "next",
            "--at", "2026-08-24 10:00",
        ])
        assert result.exit_code == 0, result.output
        assert "Morning" in result.output
        assert "NOW" in result.output
        assert "NEXT" in result.output