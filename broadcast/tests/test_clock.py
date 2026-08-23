"""Tests for minute-level station clock templates."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from broadcast.clock import (
    DEFAULT_HOUR_TEMPLATE,
    ClockSlot,
    HourTemplate,
    build_hour,
    render_hour,
)
from broadcast.playlistgen import RotationEngine, cli, write_m3u


def _track(path: str, artist: str, category: str, duration: float) -> dict:
    return {
        "path": path,
        "artist": artist,
        "title": Path(path).stem,
        "category": category,
        "duration": duration,
    }


def _engine(tracks: list[dict], artist_gap: int = 2) -> RotationEngine:
    categories = {
        track["category"]: {"sph": 1}
        for track in tracks
        if track.get("category")
    }
    rotation = {
        "categories": categories,
        "rules": {
            "artist_gap": artist_gap,
            "title_gap": 1,
            "category_gap": 1,
        },
    }
    return RotationEngine(tracks, rotation, seed=999)


def _default_tracks(per_category: int = 5) -> list[dict]:
    tracks = []
    for category in ("A", "B", "C", "GOLD"):
        for number in range(per_category):
            tracks.append(
                _track(
                    f"/{category}/track-{number}.mp3",
                    f"{category} Artist {number}",
                    category,
                    180,
                )
            )
    return tracks


def test_default_template_starts_with_top_of_hour_event():
    first = DEFAULT_HOUR_TEMPLATE.slots[0]

    assert first.label == ":00"
    assert first.event_kind == "legal_id"
    assert first.marker == "ID"


def test_rendered_hour_has_one_item_per_slot_when_blocks_fit_one_track():
    template = HourTemplate(
        "Short test hour",
        [
            ClockSlot(position_label=":00", kind="legal_id"),
            ClockSlot(position_label=":01", kind="music", source_category="A"),
            ClockSlot(position_label=":02", kind="sweeper", name="test"),
            ClockSlot(position_label=":03", kind="music", source_category="B"),
            ClockSlot(position_label=":04", kind="promo"),
        ],
    )
    tracks = [
        _track("/music/a.mp3", "Artist A", "A", 60),
        _track("/music/b.mp3", "Artist B", "B", 60),
    ]

    rendered = build_hour(template, _engine(tracks), hour_of_day=9, seed=7)

    assert len(rendered) == len(template.slots)


def test_music_blocks_only_use_their_allowed_categories():
    rendered = render_hour(
        DEFAULT_HOUR_TEMPLATE,
        _engine(_default_tracks()),
        hour_of_day=14,
        seed=42,
    )

    for item in rendered:
        if "marker" not in item:
            assert item["category"] == item["_clock_source_category"]


def test_same_seed_builds_the_same_hour():
    engine = _engine(_default_tracks())

    first = build_hour(DEFAULT_HOUR_TEMPLATE, engine, 14, 123)
    second = build_hour(DEFAULT_HOUR_TEMPLATE, engine, 14, 123)

    assert first == second


def test_artist_gap_crosses_music_block_boundaries():
    template = HourTemplate(
        "Boundary test",
        [
            ClockSlot(position_label=":00", kind="music", source_category="A"),
            ClockSlot(position_label=":01", kind="sweeper", name="test"),
            ClockSlot(position_label=":02", kind="music", source_category="B"),
            ClockSlot(position_label=":03", kind="promo"),
        ],
    )
    tracks = [
        _track("/music/a.mp3", "Shared Artist", "A", 60),
        _track("/music/b-shared.mp3", "Shared Artist", "B", 60),
        _track("/music/b-other.mp3", "Other Artist", "B", 60),
    ]

    rendered = render_hour(
        template, _engine(tracks, artist_gap=1), hour_of_day=10, seed=2
    )
    music = [item for item in rendered if "marker" not in item]

    assert [item["artist"] for item in music] == [
        "Shared Artist",
        "Other Artist",
    ]


def test_empty_library_keeps_events_and_does_not_crash():
    rendered = build_hour(
        DEFAULT_HOUR_TEMPLATE,
        _engine([]),
        hour_of_day=0,
        seed=1,
    )

    assert rendered == [
        "ID",
        "SWEEPER:station",
        "PROMO",
        "SWEEPER:station",
    ]


def test_build_hour_output_writes_markers_as_m3u_comments(tmp_path):
    rendered = build_hour(
        DEFAULT_HOUR_TEMPLATE,
        _engine(_default_tracks()),
        hour_of_day=0,
        seed=1,
    )
    output_path = tmp_path / "clock.m3u"

    write_m3u(rendered, str(output_path))

    content = output_path.read_text(encoding="utf-8")
    assert "#CLOCK ID" in content
    assert "#CLOCK SWEEPER:station" in content
    assert "\nID\n" not in content


def test_default_markers_are_at_expected_minutes():
    events = [
        (slot.label, slot.marker)
        for slot in DEFAULT_HOUR_TEMPLATE.slots
        if not slot.is_music
    ]

    assert events == [
        (":00", "ID"),
        (":14", "SWEEPER:station"),
        (":30", "PROMO"),
        (":45", "SWEEPER:station"),
    ]


def test_playlistgen_clock_cli_writes_markers_and_json(tmp_path):
    tracks = [
        _track("/music/power.mp3", "Power Artist", "A", 780),
        _track("/music/hot.mp3", "Hot Artist", "B", 900),
        _track("/music/recurrent.mp3", "Recurrent Artist", "C", 840),
        _track("/music/gold.mp3", "Gold Artist", "GOLD", 840),
    ]
    rotation = {
        "categories": {
            "A": {"sph": 4},
            "B": {"sph": 3},
            "C": {"sph": 2},
            "GOLD": {"sph": 1},
        },
        "rules": {"artist_gap": 2, "title_gap": 1, "category_gap": 1},
    }
    library_path = tmp_path / "library.json"
    rotation_path = tmp_path / "rotation.json"
    output_path = tmp_path / "clock.m3u"
    library_path.write_text(json.dumps(tracks), encoding="utf-8")
    rotation_path.write_text(json.dumps(rotation), encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "--library",
            str(library_path),
            "--rotation",
            str(rotation_path),
            "--clock",
            "--hour",
            "14",
            "--seed",
            "42",
            "-o",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.output
    m3u = output_path.read_text(encoding="utf-8")
    assert "#CLOCK :00 ID" in m3u
    assert "#CLOCK :14 SWEEPER:station" in m3u
    assert "#CLOCK :30 PROMO" in m3u
    assert "#CLOCK :45 SWEEPER:station" in m3u

    sidecar = json.loads(output_path.with_suffix(".json").read_text())
    assert [track["category"] for track in sidecar["tracks"]] == [
        "A",
        "B",
        "C",
        "GOLD",
    ]
    assert sidecar["clock"]["template"] == DEFAULT_HOUR_TEMPLATE.name
    assert len(sidecar["clock"]["items"]) == len(DEFAULT_HOUR_TEMPLATE.slots)
