# Imaging Library Spec

How the imaging library is laid out on disk, named, and tagged so the clock
engine and the human both know what is what. This is the organization layer
that sits on top of the `kind=sweeper|jingle|liner` support already in the
stack.

Scope: folder layout, naming rules, the element menu, the minimum viable set,
and the trackcheck workflow. No code changes are described here beyond the one
scanner note in section 1 — this is the contract the files follow.

---

## 1. Folder layout

Imaging lives under the music root in a single `_imaging/` umbrella. The
underscore is deliberate: it sorts to the top of the folder listing and reads
as "not music" at a glance, and the studio scanner does not skip it (it only
skips dot-prefixed hidden folders).

```
music/
  _imaging/
    sweepers/     kind=sweeper   -> :14 / :45 sweeper slots
    jingles/      kind=jingle    -> :30 promo slot (promos ARE jingles, see 2)
    liners/       kind=liner     -> :05 / :40 liner slots
    ids/          (not scanned yet) -> :00 legal ID
    stingers/     (not scanned yet) -> no clock slot
  ... normal music files ...
```

### Naming convention

`KIND_SLOT_NN.ext` — two-letter kind, a slot/descriptor token, a zero-padded
two-digit number, underscore-separated, no spaces.

| Kind code | Meaning | Example |
|-----------|---------|---------|
| `ID` | station / legal ID | `ID_TOH_01.mp3` (top of hour) |
| `SW` | sweeper | `SW_STATION_01.mp3` |
| `LN` | liner | `LN_OPENER_01.mp3` |
| `PR` | promo | `PR_SHOW_01.mp3` |
| `ST` | stinger | `ST_HIT_01.mp3` |
| `JG` | jingle (musical bump) | `JG_BED_01.mp3` |

The slot token is free-form but should be short and stable: `TOH`, `STATION`,
`OPENER`, `CLOSER`, `SHOW`, `BED`, `HIT`. The number is a per-kind sequence
starting at `01`. When you add a new sweeper you bump the number, you do not
rename the old ones.

### The `[CATEGORY]` bracket pattern

The stack already parses a `[CODE]` prefix out of a filename stem — but it is
`broadcast/playlistgen.py::_detect_category()` that does it, not
`studio/core.py`. It takes everything between the first `[` and the first `]`,
strips and uppercases it, and uses it as the rotation **category** code
(`A`, `B`, `C`, `NEW`, `GOLD`). The studio sample files (`[Hot] Midnight
Drive.mp3`) carry that prefix, but their `category` field in `library.json` is
empty — the studio's category chip is driven by the `category` field set in the
studio's own category editor, not by the filename bracket.

So there are two different tags and they do two different jobs:

- **Bracket `[CODE]`** -> `category` (music rotation tier). Parsed by
  `playlistgen._detect_category`.
- **Folder name** -> `kind` (sweeper / jingle / liner). Inferred by
  `studio.core.Store.scan`.

The clock engine's short-form substitution keys off **`kind`**, not
`category`. That is why imaging files are organized by folder, not by bracket.
You can still put a bracket on an imaging file if you want a category chip to
render, but it is optional and it does not affect which clock slot the file
feeds.

### Scanner note (the one thing to know before you adopt `_imaging/`)

`Store.scan` infers `kind` from `rel.parts[0]` — the **top-level** folder name
only. It recognizes exactly `sweepers`, `jingles`, `liners`. Two consequences:

1. Flat top-level folders (`music/sweepers/`, `music/jingles/`,
   `music/liners/`) work today with zero code change.
2. The `_imaging/` umbrella does **not** work today: a file at
   `music/_imaging/sweepers/SW_STATION_01.mp3` has `rel.parts[0] == "_imaging"`,
   so no `kind` is inferred and the file scans in as ordinary music.

If you adopt the umbrella (recommended), make the scanner look one level
deeper. In `Store.scan`, when `rel.parts[0] == "_imaging"`, read the kind from
`rel.parts[1]` instead. That is a three-line change and it is the only code
this spec implies. Until that lands, keep the flat folders.

---

## 2. The element menu

| Element | Folder | Duration | Clock slot | Substituted today? |
|---------|--------|----------|------------|--------------------|
| Top-of-hour ID | `ids/` | 5–15s | `:00` | **No** — `legal_id` has no track-kind mapping; always renders the `ID` marker |
| Sweeper | `sweepers/` | 5–15s | `:14`, `:45` | **Yes** — `kind=sweeper` |
| Promo | `jingles/` | 15–30s | `:30` | **Yes** — `kind=jingle` (the promo slot substitutes jingles) |
| Liner | `liners/` | 5–20s | `:05`, `:40` | **Yes** — `kind=liner` |
| Stinger | `stingers/` | 2–5s | none | **No** — no stinger kind exists |

Two things worth calling out because they will bite you:

- **Promos are jingles.** The clock's substitution map is
  `{"sweeper": "sweeper", "promo": "jingle", "liner": "liner"}`. A `:30` promo
  slot pulls from `kind=jingle` files. So promos live in `jingles/` and get
  tagged `kind=jingle`. There is no `promos/` folder and no `kind=promo`.
- **IDs and stingers are not wired up.** The `:00` legal ID always renders the
  text marker `ID`; nothing substitutes it. Stingers have no clock slot at all.
  Both folders are placeholders for future work, not something the engine reads
  today.

Duration ranges above are the *recommended* targets. The hard limits trackcheck
enforces are looser: `sweeper` and `jingle` cap at 90s, `liner` at 30s (see
`KIND_DURATION_LIMITS` in `broadcast/trackcheck.py`). Stay inside the
recommended range and you will never trip the hard limit.

---

## 3. Naming rules

1. Two-letter kind code, then a slot token, then a two-digit number, all
   joined by underscores: `SW_STATION_01.mp3`.
2. The number is zero-padded to two digits (`01`–`99`). `SW_STATION_1.mp3` is
   wrong; it sorts after `SW_STATION_10` and breaks the sequence.
3. No spaces. Ever. Spaces are legal in a filename but they are a liability in
   M3U paths and every shell command that touches the file. Use underscores.
4. ASCII only. No emoji, no accented characters, no unicode. The M3U writer and
   the CLI both assume plain paths; a `SW_STATION_01 🎙️.mp3` will survive the
   scan and then break the first time it is quoted, globbed, or written into a
   playlist.

What actually breaks when you ignore these:

- **Spaces** — an M3U line is whitespace-delimited in several consumers; a
  space in a path means the loader splits it wrong or you have to quote every
  command.
- **Emoji / unicode** — `playlistgen` and the studio write paths verbatim into
  JSON and M3U. A non-ASCII filename round-trips through `ensure_ascii=False`
  JSON fine, but the moment a tool normalizes or re-encodes it, the path stops
  matching the on-disk file and the track silently drops out of the library.
- **Missing zero-pad** — breaks lexical sort, so the deterministic picker (see
  section 4) sees a different order than you do and "random" selection stops
  looking random.

---

## 4. Minimum viable imaging set

For a 24/7 station, the floor is:

| Element | Count | Plays/day | Each airs |
|---------|-------|-----------|-----------|
| IDs | 6 | 24 (1/hour) | ~4x/day |
| Sweepers | 10 | 48 (2/hour) | ~5x/day |
| Liners | 8 | 48 (2/hour) | ~6x/day |
| Promos | 4 | 24 (1/hour) | ~6x/day |

Why these numbers: the short-form picker (`_pick_short_form_track` in
`broadcast/clock.py`) is deterministic and has **no repeat guard**. It seeds an
RNG from `(seed, hour_of_day, slot_index)` and picks a uniform random index
into a path-sorted candidate list. That means:

- For a fixed seed and hour, the same file always wins the same slot — that is
  the determinism, and it is fine.
- But there is no "don't play this again for N hours" rule for short-form
  audio. Variety comes *only* from the size of the pool.

With 2 or 3 sweepers, the same sweeper fires at both `:14` and `:45` in the
same hour and again the next hour, and a listener hears a loop inside a single
commute. Ten sweepers is the point where random selection stops sounding like a
loop within a 4-hour listening window. IDs get the same treatment at 6 because
the top-of-hour ID is the most-heard branding element on the station — enough
variety to avoid the robot effect, few enough to build recognition. Promos stay
at 4 because they are usually show-specific and rotate slower.

The researcher's finding — that the deterministic picker repeats — is exactly
this: the picker is correct, the *library* is too small. The fix is more files,
not a smarter picker.

---

## 5. trackcheck workflow

Scan and tag a fresh download. `--kind` accepts `sweep` (alias for sweeper),
`jingle`, and `liner`, case-insensitive.

```
# Sweepers (short station IDs)
trackcheck /music/_imaging/sweepers --kind sweep

# Jingles and promos (promos are jingles to the engine)
trackcheck /music/_imaging/jingles --kind jingle

# Liners (voice-tracked "you're listening to...")
trackcheck /music/_imaging/liners --kind liner
```

Add `--fix` to fill empty artist/title tags from the filename (non-destructive,
never overwrites existing tags):

```
trackcheck /music/_imaging/sweepers --kind sweep --fix
```

If a file is legitimately longer than the short-form limit and you want to keep
it, `--force` suppresses the `too_long_for_kind` flag:

```
trackcheck /music/_imaging/jingles --kind jingle --force
```

Write the report to JSON for scripting:

```
trackcheck /music/_imaging/liners --kind liner --json /tmp/liners-report.json
```

### The `.m3u` variant for the `[CATEGORY]` bracket convention

The bracket prefix lives in the filename, and `_detect_category` reads it from
the stem. When a bracket-prefixed file is written into an M3U, the bracket
stays in the path line and the `#EXTINF` label carries the human-readable
artist/title — the category is not repeated in the label:

```
#EXTM3U
#EXTINF:12,Station - Sweeper 01
/music/_imaging/sweepers/[Sweeper] SW_STATION_01.mp3
#EXTINF:18,Station - Promo 01
/music/_imaging/jingles/[Jingle] PR_SHOW_01.mp3
```

The `[Sweeper]` / `[Jingle]` prefix is optional and cosmetic for imaging — the
clock keys off `kind`, which comes from the folder, not the bracket. If you do
use brackets, keep the code inside them to a known rotation code (`A`, `B`,
`C`, `NEW`, `GOLD`) or a short label, because `_detect_category` uppercases
whatever it finds and treats it as a category code.

---

## Summary

- One `_imaging/` umbrella under the music root, five subfolders, three of
  which the scanner understands today (`sweepers/`, `jingles/`, `liners/`).
- `KIND_SLOT_NN.ext` naming, zero-padded, no spaces, ASCII only.
- The clock substitutes sweepers, jingles (as promos), and liners today; IDs
  and stingers are placeholders.
- Minimum viable set: 6 IDs, 10 sweepers, 8 liners, 4 promos — sized to beat
  the deterministic picker's repeat behavior, not to fix the picker.
- `trackcheck --kind sweep|jingle|liner` is the tagging workflow; brackets are
  optional and set `category`, not `kind`.
