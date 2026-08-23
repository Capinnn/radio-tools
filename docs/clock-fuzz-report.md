# Clock module fuzz report

**Date:** 2026-08-23
**Module:** `broadcast/clock.py` (commit `8238669`, "feat: clock template hour structure (station clock)")
**Scope:** property fuzz of `build_hour` / `render_hour` / `RotationEngine.generate(strict_gaps=True)`
**Seed count:** 200 randomized cases + 6 targeted edge cases
**Repro harness:** `/tmp/fuzz_clock.py` (throwaway; not committed)
**Interpreter:** `./studio/.venv/bin/python`

## Summary

No real defects found. All invariants held across 200 randomized seeds and
the targeted edge probes. **No code was modified.** Commit is a report only
(`qa: clock module fuzz report`).

## What was randomized

Per seed (deterministic master RNG, seeds derived from `20260823`):

- Template: default `DEFAULT_HOUR_TEMPLATE` ~half the time; otherwise a
  synthetic valid template (3-8 slots, music/`legal_id`/`sweeper`/`promo`,
  minute-scale offsets `< 60m`, random `weight_hint`, random
  `allow_gap_from_previous`).
- Library: 5-40 tracks, artists from a small 6-artist pool (forces collisions),
  categories drawn from template codes `A/B/C/GOLD` plus a disallowed pool
  `D/NEW/X/""` (0-3 "rejected" tracks per library), durations varied.
- Rotation: `sph` 1-10 per category, `artist_gap` 0-4, `title_gap` 0-2,
  `category_gap` 0-2; ~50% with a `Morning` daypart weight table.
- `hour_of_day` 0-23, `daypart` `None`/`Morning`.

## Invariant verdicts

| Invariant | Verdict | Detail |
|---|---|---|
| No crash on any input | **PASS** (205/205) | Includes empty library, single-track, zero-duration tracks, hour bounds |
| Output length matches template / valid shortened fallback | **PASS** (202/202) | Every event present exactly once; a music block drops only when its source category is empty in the library OR the block engine legitimately stalls under `strict_gaps` (no candidate satisfies the gap rules given cross-block history) |
| `:00` is a `legal_id` marker on the default template | **PASS** (102/102) | First output item is always the `ID` marker (a `ClockMarker`) |
| Markers appear exactly at template positions | **PASS** (200/200) | Event order, `_clock_position_seconds`, `_clock_position_label`, `_clock_kind` all match slot metadata |
| Music blocks only from allowed categories | **PASS** (201/201) | Every music item's `category` == its `_clock_source_category` ∈ template source codes |
| Artist gap honored across music-block boundaries | **PASS** (165/165) | Within a gap-preserving run no same artist is consecutive; a block declaring `allow_gap_from_previous=True` intentionally resets history (documented behaviour) |
| Determinism (same seed twice → identical) | **PASS** (200/200) | Fresh engine + same seed gives byte-identical output |
| No same artist across a boundary is only enforced where the module says | **PASS** | `allow_gap_from_previous` and `strict_guts` give the expected escapes |

## Targeted edges probed

1. Fully empty library with the default template → returns exactly
   `["ID", "SWEEPER:station", "PROMO", "SWEEPER:station"]`, no crash.
2. Single track in one category → blocks for other categories drop cleanly;
   the surviving block yields only that category.
3. All tracks have `duration=0` → no hang / runaway (engine's 0-duration guard
   terminates; all 4 event markers still present).
4. `hour_of_day=24` → raises `ValueError`.
5. `hour_of_day=True` → raises `TypeError` (bool guard).
6. Daypart weights present and absent across many randomized cases.

## Notes / non-issues investigated

The first fuzz passes produced two false alarms that were traced to the
harness, not the module, and are recorded here for completeness:

- **"Music block dropped with its category present":** the block in question
  had a 1-track category whose only track was already played by an earlier
  block with `artist_gap` ≥ 2. Under `strict_gaps=True` the block engine
  correctly produced zero tracks (gap-stall). This is the documented
  "valid shortened fallback", not a bug.
- **"Consecutive same artist across a boundary":** the boundary block
  declared `allow_gap_from_previous=True`, which by design resets gap history
  (see `ClockSlot` docstring). Not a bug.

No reproducible defect in `build_hour` / `render_hour` / `RotationEngine`
clock block path was found.

## Regression suites (pre/post)

Ran before fuzzing; all green:

- `tests/` → **72 passed**
- `liquidsoap/tests/` → **110 passed**
- `studio/tests/` → **61 passed**

## Commit

`qa: clock module fuzz report` — report file `docs/clock-fuzz-report.md` only.
No source changes. Not pushed.
