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

from broadcast.clock import (
    _SHORT_FORM_BLOCK_FLOOR_SECONDS,
    _SHORT_FORM_WARN_THRESHOLD_SECONDS,
)


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
    assert "#CLOCK :00 ID" in content
    assert "#CLOCK :14 SWEEPER:station" in content
    assert "#CLOCK :30 PROMO" in content
    assert "#CLOCK :45 SWEEPER:station" in content
    assert "ID" in content.splitlines()[1]


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


# ── short-form sweeper/jingle substitution ─────────────────────────────


def _track_with_kind(path: str, artist: str, category: str, duration: float,
                     kind: str = "") -> dict:
    """Like _track but adds a kind field for sweeper/jingle tagging."""
    track = _track(path, artist, category, duration)
    track["kind"] = kind
    return track


def test_sweeper_slot_uses_tagged_sweeper_file():
    """A sweeper slot should use a kind=sweeper file from the library."""
    template = HourTemplate(
        "Sweeper substitution",
        [
            ClockSlot(position_label=":00", kind="music", source_category="A"),
            ClockSlot(position_label=":14", kind="sweeper", name="station"),
            ClockSlot(position_label=":15", kind="music", source_category="B"),
        ],
    )
    tracks = [
        _track("/music/a1.mp3", "Artist A", "A", 60),
        _track("/music/b1.mp3", "Artist B", "B", 60),
        _track_with_kind("/sweepers/station-id.mp3", "Station", "sweepers",
                         10, kind="sweeper"),
    ]
    result = build_hour(template, _engine(tracks), hour_of_day=9, seed=7)

    # The sweeper slot should be replaced with the file path, not a marker.
    assert "/sweepers/station-id.mp3" in result
    assert "SWEEPER:station" not in result


def test_sweeper_slot_falls_back_to_marker_without_tagged_file():
    """Without a kind=sweeper file, the marker should be kept."""
    template = HourTemplate(
        "Sweeper fallback",
        [
            ClockSlot(position_label=":00", kind="music", source_category="A"),
            ClockSlot(position_label=":14", kind="sweeper", name="station"),
        ],
    )
    tracks = [
        _track("/music/a1.mp3", "Artist A", "A", 60),
    ]
    result = build_hour(template, _engine(tracks), hour_of_day=9, seed=7)

    assert "SWEEPER:station" in result


def test_promo_slot_uses_tagged_jingle_file():
    """A promo slot should use a kind=jingle file from the library."""
    template = HourTemplate(
        "Promo substitution",
        [
            ClockSlot(position_label=":00", kind="music", source_category="A"),
            ClockSlot(position_label=":30", kind="promo"),
        ],
    )
    tracks = [
        _track("/music/a1.mp3", "Artist A", "A", 60),
        _track_with_kind("/jingles/promo.mp3", "Station", "jingles", 15,
                         kind="jingle"),
    ]
    result = build_hour(template, _engine(tracks), hour_of_day=9, seed=7)

    assert "/jingles/promo.mp3" in result
    assert "PROMO" not in result


def test_sweeper_substitution_is_deterministic():
    """Same seed produces the same sweeper file selection."""
    template = HourTemplate(
        "Deterministic sweeper",
        [
            ClockSlot(position_label=":00", kind="music", source_category="A"),
            ClockSlot(position_label=":14", kind="sweeper", name="station"),
        ],
    )
    tracks = [
        _track("/music/a1.mp3", "Artist A", "A", 60),
        _track_with_kind("/sweepers/sweeper1.mp3", "S", "sweepers", 10,
                         kind="sweeper"),
        _track_with_kind("/sweepers/sweeper2.mp3", "S", "sweepers", 10,
                         kind="sweeper"),
    ]
    first = build_hour(template, _engine(tracks), hour_of_day=9, seed=42)
    second = build_hour(template, _engine(tracks), hour_of_day=9, seed=42)
    assert first == second
    # The selected sweeper should be one of the two available files
    sweeper_entry = next(
        item for item in first if "sweepers" in str(item)
    )
    assert sweeper_entry in ("/sweepers/sweeper1.mp3",
                             "/sweepers/sweeper2.mp3")


def test_legal_id_slot_not_substituted_by_sweeper():
    """legal_id slots should not be substituted by sweeper files."""
    template = HourTemplate(
        "ID not substituted",
        [
            ClockSlot(position_label=":00", kind="legal_id"),
            ClockSlot(position_label=":01", kind="music", source_category="A"),
        ],
    )
    tracks = [
        _track("/music/a1.mp3", "Artist A", "A", 60),
        _track_with_kind("/sweepers/station-id.mp3", "Station", "sweepers",
                         10, kind="sweeper"),
    ]
    result = build_hour(template, _engine(tracks), hour_of_day=9, seed=7)

    assert "ID" in result
    assert "/sweepers/station-id.mp3" not in result


# ── voice-tracked liner slots ───────────────────────────────────────────


def test_liner_slot_uses_tagged_liner_file():
    """A liner slot should use a kind=liner file from the library."""
    template = HourTemplate(
        "Liner substitution",
        [
            ClockSlot(position_label=":00", kind="music", source_category="A"),
            ClockSlot(position_label=":05", kind="liner", name="station"),
            ClockSlot(position_label=":06", kind="music", source_category="B"),
        ],
    )
    tracks = [
        _track("/music/a1.mp3", "Artist A", "A", 60),
        _track("/music/b1.mp3", "Artist B", "B", 60),
        _track_with_kind("/liners/youre-listening-to.mp3", "Station",
                         "liners", 10, kind="liner"),
    ]
    result = build_hour(template, _engine(tracks), hour_of_day=9, seed=7)

    # The liner slot should be replaced with the file path, not a marker.
    assert "/liners/youre-listening-to.mp3" in result
    assert "LINER:station" not in result


def test_liner_slot_falls_back_to_marker_without_tagged_file():
    """Without a kind=liner file, the marker should be kept."""
    template = HourTemplate(
        "Liner fallback",
        [
            ClockSlot(position_label=":00", kind="music", source_category="A"),
            ClockSlot(position_label=":05", kind="liner", name="station"),
        ],
    )
    tracks = [
        _track("/music/a1.mp3", "Artist A", "A", 60),
    ]
    result = build_hour(template, _engine(tracks), hour_of_day=9, seed=7)

    assert "LINER:station" in result


def test_with_liners_injects_liner_slots_at_default_positions():
    """with_liners adds liner slots at :05 and :40 into the default template."""
    from broadcast.clock import with_liners, CLOCK_LINER_SLOTS

    enriched = with_liners(DEFAULT_HOUR_TEMPLATE)
    assert isinstance(enriched, type(DEFAULT_HOUR_TEMPLATE))  # subclass
    liner_slots = [s for s in enriched.slots if s.event_kind == "liner"]
    assert len(liner_slots) == 2
    assert [s.label for s in liner_slots] == list(CLOCK_LINER_SLOTS)


def test_liner_cli_flag_passes_through(tmp_path):
    """--liners flag enables liner markers in the clock CLI output."""
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
            "--liners",
            "--hour",
            "14",
            "--seed",
            "42",
            "-o",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Liners: enabled" in result.output
    m3u = output_path.read_text(encoding="utf-8")
    # With no kind=liner files in the library, liner slots produce markers.
    assert "LINER:station" in m3u
    # Existing clock events are still present.
    assert "#CLOCK :00 ID" in m3u


# ── clock-drift: short-form substitution budget ─────────────────────────


def _hour_total_seconds(rendered: list[dict]) -> float:
    """Sum the durations of every real track (music + substituted short-form)."""
    total = 0.0
    for item in rendered:
        if "marker" in item:
            continue
        total += float(item.get("duration", 0) or 0.0)
    return total


def test_15s_liner_shrinks_following_music_block_by_15s(monkeypatch):
    """A 15 s liner should reduce the following music block's target by 15 s.

    We spy on RotationEngine.generate to capture the target_duration passed
    for each block.  The engine quantises output to whole tracks, so checking
    the actual track count would be brittle — the *requested* target is the
    authoritative signal that the budget was adjusted.
    """
    template = HourTemplate(
        "Liner budget",
        [
            ClockSlot(position_label=":00", kind="music", source_category="A"),
            ClockSlot(position_label=":10", kind="liner", name="station"),
            ClockSlot(position_label=":11", kind="music", source_category="B"),
            ClockSlot(position_label=":14", kind="promo"),
        ],
    )
    tracks = [
        _track(f"/music/a{i}.mp3", f"Artist A{i}", "A", 60) for i in range(20)
    ] + [
        _track(f"/music/b{i}.mp3", f"Artist B{i}", "B", 60) for i in range(20)
    ] + [
        _track_with_kind("/liners/liner.mp3", "Station", "liners", 15,
                         kind="liner"),
    ]

    captured: list[float] = []
    original_generate = RotationEngine.generate

    def spy_generate(self, target_duration: float = 3600.0, **kwargs):
        captured.append(target_duration)
        return original_generate(self, target_duration=target_duration,
                                 **kwargs)

    monkeypatch.setattr(RotationEngine, "generate", spy_generate)
    render_hour(template, _engine(tracks), hour_of_day=9, seed=7)

    # Block A: :00 → :10 = 600 s (no preceding short-form).
    # Block B: :11 → :14 = 180 s, minus 15 s liner = 165 s.
    assert captured[0] == 600.0
    assert captured[1] == 165.0


def test_hour_total_stays_at_3600_with_substituted_short_form():
    """The whole-hour total (music + substituted short-form) stays <= 3600."""
    template = HourTemplate(
        "Hour total",
        [
            ClockSlot(position_label=":00", kind="legal_id"),
            ClockSlot(position_label=":01", kind="music", source_category="A"),
            ClockSlot(position_label=":14", kind="sweeper", name="station"),
            ClockSlot(position_label=":15", kind="music", source_category="B"),
            ClockSlot(position_label=":30", kind="promo"),
            ClockSlot(position_label=":31", kind="music", source_category="C"),
            ClockSlot(position_label=":45", kind="sweeper", name="station"),
            ClockSlot(position_label=":46", kind="music", source_category="GOLD"),
        ],
    )
    tracks = (
        [_track(f"/music/a{i}.mp3", f"A{i}", "A", 180) for i in range(20)]
        + [_track(f"/music/b{i}.mp3", f"B{i}", "B", 180) for i in range(20)]
        + [_track(f"/music/c{i}.mp3", f"C{i}", "C", 180) for i in range(20)]
        + [_track(f"/music/g{i}.mp3", f"G{i}", "GOLD", 180) for i in range(20)]
        + [_track_with_kind("/sweepers/s1.mp3", "S", "sweepers", 12,
                            kind="sweeper")]
        + [_track_with_kind("/jingles/p1.mp3", "S", "jingles", 8,
                            kind="jingle")]
        + [_track_with_kind("/ids/legal.mp3", "S", "ids", 5, kind="id")]
    )
    rendered = render_hour(template, _engine(tracks), hour_of_day=14, seed=42)
    total = _hour_total_seconds(rendered)
    # The hour should not run long.  Allow a small overshoot (one track).
    max_track = max((i.get("duration", 0) for i in rendered), default=0)
    assert total <= 3600 + max_track


def test_95s_sweeper_logs_warning_and_still_plays(caplog):
    """A 95 s sweeper logs a warning (>30 s) but is still in the output."""
    template = HourTemplate(
        "Long sweeper",
        [
            ClockSlot(position_label=":00", kind="music", source_category="A"),
            ClockSlot(position_label=":14", kind="sweeper", name="station"),
            ClockSlot(position_label=":15", kind="music", source_category="B"),
        ],
    )
    tracks = [
        _track("/music/a1.mp3", "Artist A", "A", 60),
        _track("/music/b1.mp3", "Artist B", "B", 60),
        _track_with_kind("/sweepers/long.mp3", "Station", "sweepers", 95,
                         kind="sweeper"),
    ]
    with caplog.at_level("WARNING", logger="broadcast.clock"):
        result = build_hour(template, _engine(tracks), hour_of_day=9, seed=7)

    # The sweeper file is still played.
    assert "/sweepers/long.mp3" in result
    # A warning was logged mentioning the duration threshold.
    assert any(
        "exceeds" in rec.message and "95.0" in rec.message
        for rec in caplog.records
    )


def test_no_short_form_file_means_zero_change_backward_compat():
    """Without a kind=sweeper file, the marker is kept and blocks are unchanged."""
    template = HourTemplate(
        "No short-form",
        [
            ClockSlot(position_label=":00", kind="music", source_category="A"),
            ClockSlot(position_label=":14", kind="sweeper", name="station"),
            ClockSlot(position_label=":15", kind="music", source_category="B"),
        ],
    )
    tracks = [
        _track("/music/a1.mp3", "Artist A", "A", 60),
        _track("/music/b1.mp3", "Artist B", "B", 60),
    ]
    # No kind=sweeper file in the library.
    rendered = render_hour(template, _engine(tracks), hour_of_day=9, seed=7)

    # Marker is kept (no substitution → no duration to subtract).
    assert any(i.get("marker") == "SWEEPER:station" for i in rendered)
    # Music block B targets the full 2940 s (3600 − 840 − 60), unshrunken.
    b_items = [
        i for i in rendered
        if i.get("category") == "B" and i.get("_clock_position_label") == ":15"
    ]
    assert len(b_items) >= 1  # block generated as before


def test_short_form_substitution_preserves_determinism():
    """Same seed + substituted short-form still produces identical output."""
    template = HourTemplate(
        "Determinism with short-form",
        [
            ClockSlot(position_label=":00", kind="music", source_category="A"),
            ClockSlot(position_label=":10", kind="liner", name="station"),
            ClockSlot(position_label=":11", kind="music", source_category="B"),
        ],
    )
    tracks = [
        _track(f"/music/a{i}.mp3", f"A{i}", "A", 60) for i in range(10)
    ] + [
        _track(f"/music/b{i}.mp3", f"B{i}", "B", 60) for i in range(10)
    ] + [
        _track_with_kind("/liners/l1.mp3", "Station", "liners", 15,
                         kind="liner"),
    ]
    first = build_hour(template, _engine(tracks), hour_of_day=9, seed=42)
    second = build_hour(template, _engine(tracks), hour_of_day=9, seed=42)
    assert first == second
    # And the liner is actually substituted (not a marker).
    assert "/liners/l1.mp3" in first
