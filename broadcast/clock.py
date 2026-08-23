"""Station-clock templates layered on top of :mod:`broadcast.playlistgen`.

An hour template describes fixed events and category-constrained music blocks.
The rotation engine still chooses the music, so daypart weighting and the
existing artist/title separation rules remain the source of truth.
"""

from __future__ import annotations

import copy
import random
import re
from dataclasses import dataclass

from .playlistgen import RotationEngine


_POSITION_LABEL_RE = re.compile(r"^:(\d{2})(?::(\d{2}))?$")
_MUSIC_KIND = "music"
_MUSIC_KINDS = {_MUSIC_KIND, "category"}


class ClockMarker(str):
    """String marker that output writers can distinguish from a track path."""

    _clock_marker = True


def _seconds_from_label(label: str) -> int:
    """Convert a station-clock label such as ``:14`` to seconds."""
    match = _POSITION_LABEL_RE.fullmatch(label.strip())
    if match is None:
        raise ValueError(
            f"invalid clock position {label!r}; use :MM or :MM:SS"
        )
    minutes = int(match.group(1))
    seconds = int(match.group(2) or 0)
    if minutes > 59 or seconds > 59:
        raise ValueError(f"clock position outside the hour: {label!r}")
    return minutes * 60 + seconds


def _label_from_seconds(seconds: int) -> str:
    minutes, remainder = divmod(seconds, 60)
    if remainder:
        return f":{minutes:02d}:{remainder:02d}"
    return f":{minutes:02d}"


@dataclass(frozen=True)
class ClockSlot:
    """One fixed position in an hour template.

    Exactly one of ``position_seconds`` and ``position_label`` is required.
    Music slots use ``kind="music"`` and name their rotation category in
    ``source_category``. Other kinds become event markers; the built-in event
    kinds are ``legal_id``, ``sweeper``, and ``promo``.

    ``weight_hint`` is applied to the source category's spins-per-hour value
    in the block-local engine. ``allow_gap_from_previous`` deliberately resets
    gap history at the start of that block; by default artist/title separation
    carries across clock events and music-block boundaries.
    """

    position_seconds: int | None = None
    position_label: str | None = None
    kind: str = _MUSIC_KIND
    source_category: str | None = None
    weight_hint: float = 1.0
    allow_gap_from_previous: bool = False
    name: str | None = None

    def __post_init__(self) -> None:
        if (self.position_seconds is None) == (self.position_label is None):
            raise ValueError(
                "ClockSlot requires exactly one of position_seconds or "
                "position_label"
            )
        if self.position_seconds is not None:
            if isinstance(self.position_seconds, bool) or not isinstance(
                self.position_seconds, int
            ):
                raise TypeError("position_seconds must be an integer")
            if not 0 <= self.position_seconds < 3600:
                raise ValueError("position_seconds must be within one hour")
        else:
            assert self.position_label is not None
            _seconds_from_label(self.position_label)

        kind = self.kind.strip().lower()
        if not kind:
            raise ValueError("clock slot kind cannot be empty")
        if kind in _MUSIC_KINDS and not (self.source_category or "").strip():
            raise ValueError("music clock slots require source_category")
        if kind == "event" and not (self.name or "").strip():
            raise ValueError("generic event clock slots require a name")
        if isinstance(self.weight_hint, bool) or not isinstance(
            self.weight_hint, (int, float)
        ):
            raise TypeError("weight_hint must be a number")
        if self.weight_hint < 0:
            raise ValueError("weight_hint cannot be negative")

    @property
    def offset_seconds(self) -> int:
        """Position within the hour as an integer number of seconds."""
        if self.position_seconds is not None:
            return self.position_seconds
        assert self.position_label is not None
        return _seconds_from_label(self.position_label)

    @property
    def label(self) -> str:
        """Canonical station-clock label for this slot."""
        return _label_from_seconds(self.offset_seconds)

    @property
    def is_music(self) -> bool:
        return self.kind.strip().lower() in _MUSIC_KINDS

    @property
    def event_kind(self) -> str | None:
        """Normalised event kind, or ``None`` for a music block."""
        return None if self.is_music else self.kind.strip().lower()

    @property
    def marker(self) -> str:
        """Render this event slot as its public playlist marker."""
        kind = self.kind.strip().lower()
        if kind in _MUSIC_KINDS:
            raise ValueError("music slots do not have event markers")
        if kind in {"id", "legal_id"}:
            return "ID"
        if kind in {"promo", "station_promo"}:
            return "PROMO"
        if kind == "sweeper":
            return f"SWEEPER:{(self.name or 'station').strip()}"
        if kind == "liner":
            return f"LINER:{(self.name or 'station').strip()}"
        if kind == "event":
            assert self.name is not None
            return self.name.strip().upper()
        marker = kind.upper()
        if self.name:
            marker += f":{self.name.strip()}"
        return marker


@dataclass(frozen=True)
class HourTemplate:
    """A named, ordered list of clock positions within one hour."""

    name: str
    slots: list[ClockSlot]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("hour template name cannot be empty")
        previous = -1
        for slot in self.slots:
            if not isinstance(slot, ClockSlot):
                raise TypeError("hour template slots must be ClockSlot instances")
            if slot.offset_seconds <= previous:
                raise ValueError("clock slots must be in strictly increasing order")
            previous = slot.offset_seconds


# Broadcast's established category codes are A (Power), B (Hot), C
# (Recurrent/light), and GOLD. The template intentionally uses those codes
# rather than introducing a parallel category vocabulary.
DEFAULT_HOUR_TEMPLATE = HourTemplate(
    name="Default station hour",
    slots=[
        ClockSlot(position_label=":00", kind="legal_id"),
        ClockSlot(position_label=":01", kind="music", source_category="A"),
        ClockSlot(
            position_label=":14", kind="sweeper", name="station"
        ),
        ClockSlot(position_label=":15", kind="music", source_category="B"),
        ClockSlot(position_label=":30", kind="promo"),
        ClockSlot(position_label=":31", kind="music", source_category="C"),
        ClockSlot(
            position_label=":45", kind="sweeper", name="station"
        ),
        ClockSlot(
            position_label=":46", kind="music", source_category="GOLD"
        ),
    ],
)


def _seed_for_block(seed: int, hour_of_day: int, slot_index: int) -> int:
    """Derive stable, independent RNG seeds for the music blocks."""
    rng = random.Random(f"clock:{int(seed)}:{hour_of_day}")
    block_seed = 0
    for _ in range(slot_index + 1):
        block_seed = rng.getrandbits(64)
    return block_seed


# ── voice-tracked liners ────────────────────────────────────────────────
#
# A liner is a short pre-recorded voice file ("You're listening to…") that
# plays between songs at scheduled clock positions.  Like sweeper slots, a
# liner slot tries to substitute a kind=liner file from the library; if
# none is available, the text marker (LINER:station) is kept as fallback.

# Default liner positions within the hour, expressed as clock labels.
# These are the positions where a liner fires when --liners is enabled.
CLOCK_LINER_SLOTS: tuple[str, ...] = (":05", ":40")


@dataclass(frozen=True)
class LinerTemplate(HourTemplate):
    """An hour template with voice-tracked liner slots injected.

    Wraps a base :class:`HourTemplate` and inserts ``kind="liner"`` clock
    slots at configurable positions (default ``:05`` and ``:40``).  When a
    liner slot fires and a ``kind=liner`` file exists in the library, the
    file plays instead of the marker — mirroring the sweeper substitution.
    """

    liner_positions: tuple[str, ...] = CLOCK_LINER_SLOTS

    def __post_init__(self) -> None:
        # HourTemplate.__post_init__ already validates name/slots ordering.
        super().__post_init__()
        # Validate liner position labels.
        for label in self.liner_positions:
            _seconds_from_label(label)


def with_liners(
    template: HourTemplate,
    positions: tuple[str, ...] | list[str] | None = None,
    name: str | None = None,
    liner_name: str = "station",
) -> LinerTemplate:
    """Return a copy of *template* with liner slots inserted at *positions*.

    Liner slots are inserted in time order alongside the existing slots.
    If a liner position coincides with an existing slot, the liner is
    dropped for that position (the existing event takes precedence).

    The resulting template is a :class:`LinerTemplate` so downstream code
    can detect that liners are enabled.
    """
    pos_labels = tuple(positions) if positions is not None else CLOCK_LINER_SLOTS

    # Build the combined slot list, inserting liner slots at the right
    # chronological positions.
    existing_offsets = {slot.offset_seconds: slot for slot in template.slots}
    new_slots: list[ClockSlot] = []

    for label in pos_labels:
        offset = _seconds_from_label(label)
        if offset in existing_offsets:
            # Existing slot at this position takes precedence.
            continue
        liner_slot = ClockSlot(
            position_label=label,
            kind="liner",
            name=liner_name,
        )
        new_slots.append(liner_slot)

    all_slots = list(template.slots) + new_slots
    # Sort by offset; ties (shouldn't happen due to the dedup above) keep
    # existing slots first for stability.
    all_slots.sort(
        key=lambda s: (s.offset_seconds, 0 if s in template.slots else 1)
    )

    return LinerTemplate(
        name=name or template.name,
        slots=all_slots,
        liner_positions=pos_labels,
    )


# Map clock event kinds to the library track ``kind`` tag they prefer.
# When a slot is sweeper or promo, build_hour looks for a library track
# tagged with the corresponding kind and uses its path instead of the
# plain text marker. If none is available, the marker is kept.
#
# ``legal_id`` maps to ``kind="id"`` so a real top-of-hour legal-ID file
# (from the ``_imaging/ids/`` folder) can substitute the ``:00`` text marker
# the same way sweepers and jingles substitute their slots.
_EVENT_KIND_TO_TRACK_KIND = {
    "legal_id": "id",
    "sweeper": "sweeper",
    "promo": "jingle",
    "liner": "liner",
}


def _pick_short_form_track(
    engine: RotationEngine, slot: ClockSlot, seed: int, hour_of_day: int,
    slot_index: int,
) -> dict | None:
    """Return a tagged short-form track for an event slot, or None.

    Selection is deterministic for a given (seed, hour, slot_index): the
    RNG is seeded from those values so the same inputs always pick the
    same file.
    """
    event_kind = slot.event_kind
    if event_kind is None:
        return None
    track_kind = _EVENT_KIND_TO_TRACK_KIND.get(event_kind)
    if track_kind is None:
        return None
    candidates = [
        track for track in engine.tracks
        if str(track.get("kind") or "").lower() == track_kind
    ]
    if not candidates:
        return None
    rng = random.Random(f"clock-shortform:{int(seed)}:{hour_of_day}:{slot_index}")
    return sorted(candidates, key=lambda t: t.get("path") or t.get("id") or "")[
        rng.randrange(len(candidates))
    ]


def _block_rotation(engine: RotationEngine, slot: ClockSlot) -> dict:
    """Return a block-local rotation without mutating the caller's engine."""
    rotation = copy.deepcopy(engine.rotation)
    rules = rotation.setdefault("rules", {})

    # A clock block is already constrained to one category. Applying the flat
    # rotation category-gap rule here would reject every track after the first.
    rules["category_gap"] = 0

    source = (slot.source_category or "").upper()
    categories = rotation.setdefault("categories", {})
    matching_key = next(
        (key for key in categories if str(key).upper() == source), None
    )
    if matching_key is None:
        categories[source] = {
            "sph": float(slot.weight_hint) or 0.5,
            "description": f"Clock source {source}",
        }
    else:
        config = categories[matching_key]
        config["sph"] = float(config.get("sph", 0)) * float(slot.weight_hint)
    return rotation


def render_hour(
    template: HourTemplate,
    rotation_engine: RotationEngine,
    hour_of_day: int,
    seed: int,
) -> list[dict]:
    """Render a template into interleaved track and event dictionaries.

    Event dictionaries contain ``marker`` plus their scheduled clock position.
    Track dictionaries retain the complete library metadata and gain private
    ``_clock_*`` annotations used by the output writers. Call :func:`build_hour`
    when only the public path/marker sequence is needed.
    """
    if isinstance(hour_of_day, bool) or not isinstance(hour_of_day, int):
        raise TypeError("hour_of_day must be an integer")
    if not 0 <= hour_of_day <= 23:
        raise ValueError("hour_of_day must be between 0 and 23")

    rendered: list[dict] = []
    music_history: list[dict] = []

    for index, slot in enumerate(template.slots):
        if not slot.is_music:
            # Try to substitute a tagged short-form audio file (sweeper or
            # jingle) from the library for this event slot. If none is
            # available, fall back to the text marker.
            short_form = _pick_short_form_track(
                rotation_engine, slot, seed, hour_of_day, index,
            )
            if short_form is not None:
                item = dict(short_form)
                item["_clock_position_seconds"] = slot.offset_seconds
                item["_clock_position_label"] = slot.label
                item["_clock_kind"] = slot.event_kind
                item["_clock_marker"] = slot.marker
                rendered.append(item)
            else:
                rendered.append(
                    {
                        "marker": slot.marker,
                        "_clock_position_seconds": slot.offset_seconds,
                        "_clock_position_label": slot.label,
                        "_clock_kind": slot.event_kind,
                    }
                )
            continue

        block_end = (
            template.slots[index + 1].offset_seconds
            if index + 1 < len(template.slots)
            else 3600
        )
        block_duration = float(block_end - slot.offset_seconds)
        if block_duration <= 0:
            continue

        source = (slot.source_category or "").upper()
        category_tracks = [
            track
            for track in rotation_engine.tracks
            if str(track.get("category") or "").upper() == source
        ]
        if not category_tracks:
            continue

        block_engine = RotationEngine(
            category_tracks,
            _block_rotation(rotation_engine, slot),
            seed=_seed_for_block(seed, hour_of_day, index),
            daypart=rotation_engine.daypart,
        )

        history_limit = max(block_engine.artist_gap, block_engine.title_gap)
        history = []
        if not slot.allow_gap_from_previous and history_limit > 0:
            history = music_history[-history_limit:]

        block = block_engine.generate(
            target_duration=block_duration,
            history=history,
            strict_gaps=True,
        )
        for track in block:
            item = dict(track)
            item["_clock_position_seconds"] = slot.offset_seconds
            item["_clock_position_label"] = slot.label
            item["_clock_source_category"] = source
            item["_clock_weight_hint"] = float(slot.weight_hint)
            rendered.append(item)
            music_history.append(item)

    return rendered


def build_hour(
    template: HourTemplate,
    rotation_engine: RotationEngine,
    hour_of_day: int,
    seed: int,
) -> list[str]:
    """Build one hour as an interleaved list of paths and event markers.

    Music entries use their filesystem path (falling back to ``id`` for a
    path-less synthetic library entry). Fixed events use markers such as
    ``ID``, ``SWEEPER:station``, and ``PROMO``.
    """
    result: list[str] = []
    for item in render_hour(template, rotation_engine, hour_of_day, seed):
        if "marker" in item:
            marker = ClockMarker(item["marker"])
            marker._clock_position_seconds = item.get(
                "_clock_position_seconds"
            )
            marker._clock_position_label = item.get("_clock_position_label")
            result.append(marker)
            continue
        identifier = item.get("path") or item.get("id")
        if identifier is not None:
            result.append(str(identifier))
    return result


__all__ = [
    "ClockSlot",
    "ClockMarker",
    "HourTemplate",
    "LinerTemplate",
    "DEFAULT_HOUR_TEMPLATE",
    "CLOCK_LINER_SLOTS",
    "with_liners",
    "build_hour",
    "render_hour",
]
