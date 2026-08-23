# Changelog

The repo has no tags yet, so each section is dated. Work is grouped by area
rather than by commit order.

## 2026-08-23

The clock and imaging work landed in full. The station clock now builds a real
hour with music, sweepers, jingles, liners, and a legal ID at the top of the
hour, and the imaging library got a proper on disk layout with an `_imaging`
umbrella folder. Studio picked up a clock view panel and gzip compression for
its static output. The roadmap was written out to 30 days.

### Clock & imaging

- `8238669` clock template hour structure, the station clock itself
- `c3d7f80` clock module fuzz report
- `9064daa` sweeper and jingle tagging across trackcheck, library, and clock
- `edc8173` clock review fixes, marker labels and sidecar seconds now carry through
- `b8207ad` clock marker format documented in formats.md
- `76bfeae` liner support, voice tracked slots in clock and library
- `ea95a17` sweeper and liner review fixes
- `3b0781d` liner review fix
- `990f9c5` imaging library spec
- `7f9e4b5` `_imaging` umbrella scan plus legal ID file support

### Studio

- `0630255` studio clock view panel
- `2f1ef3f` gzip compression for studio and showcast static output

### Docs

- `09e8145` 30 day radio roadmap

Tests: 274 passing (89 broadcast, 110 liquidsoap, 75 studio).

## 2026-08-22

The stack went cross platform. The engine got a pure stdlib `radio` CLI that
manages the Liquidsoap and Icecast chain on both Linux and Windows, and the
studio launcher and paths were fixed up to match. Windows packaging and install
scripts landed, and the install now runs the engine validation as part of the
flow. Studio got a UI polish pass.

### Engine

- `69b980b` cross platform radio engine manager, the `radio` CLI
- `e32e37a` Windows path detection, dry run, and validate script
- `7faca68` install runs engine validation

### Studio

- `0fed9e6` cross platform fixes for restart, paths, launcher, and docs
- `6a9213a` studio UI polish pass, snappy, dark, no glyphs

### Tooling

- `283a440` Windows packaging and install scripts
- `2c0e2db` package dir mapping fix, broadcast lives at the repo root
- `c0b5997` untrack egg-info build artifacts

### Docs

- `783b702` refresh quick start, platforms, and troubleshooting

Tests: 232 passing (63 broadcast, 108 engine, 61 studio).

## Caveats

The Windows side has not been run on real hardware yet. The packaging, install,
and validation scripts are written and wired up, but they have only been
exercised on Linux. Live assist is Linux only for now.
