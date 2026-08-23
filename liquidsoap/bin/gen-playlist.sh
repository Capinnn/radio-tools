#!/usr/bin/env bash
# Superseded by the radio engine manager on other platforms.
# This script works on Linux only. Use `radio gen-playlist` (Python,
# cross-platform) for Linux + Windows.  See liquidsoap/engine/ and
# liquidsoap/README.md.
set -Eeuo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
REPO_DIR=$(cd -- "$ROOT_DIR/.." && pwd)

usage() {
  cat <<'EOF'
Usage: gen-playlist.sh [OPTIONS]

Generate one hour of rotation with broadcast/playlistgen, validate its M3U and
JSON sidecar, publish them atomically, and touch the Liquidsoap reload trigger.

Input (choose one; defaults to studio/music):
  --source DIR       Scan an audio directory.
  --library FILE     Use a broadcast-format library JSON index.

Options:
  --rotation FILE    Rotation JSON (default: liquidsoap/config/rotation.json).
  --output FILE      Destination M3U (default: liquidsoap/data/playlist.m3u).
  --trigger FILE     Reload trigger (default: liquidsoap/data/playlist.trigger).
  --slot DURATION    playlistgen slot duration (default: 1h).
  --daypart NAME     Apply a named daypart's weights.
  --hour HOUR        Program-clock hour, 0-23 (default: current local hour).
  --seed INTEGER     Deterministic seed (default: current epoch hour).
  --loop             Repeat at the next local hour boundary (minute 0).
  --dry-run          Print the planned commands without writing or sleeping.
  -h, --help         Show this help.

Environment defaults: RADIO_MUSIC_DIR, RADIO_LIBRARY, RADIO_ROTATION,
RADIO_PLAYLIST, and RADIO_PLAYLIST_TRIGGER.
EOF
}

die() {
  printf 'gen-playlist: %s\n' "$*" >&2
  exit 1
}

SOURCE_DIR=${RADIO_MUSIC_DIR:-$REPO_DIR/studio/music}
LIBRARY_FILE=${RADIO_LIBRARY:-}
ROTATION_FILE=${RADIO_ROTATION:-$ROOT_DIR/config/rotation.json}
OUTPUT_FILE=${RADIO_PLAYLIST:-$ROOT_DIR/data/playlist.m3u}
TRIGGER_FILE=${RADIO_PLAYLIST_TRIGGER:-$ROOT_DIR/data/playlist.trigger}
SLOT=1h
DAYPART=
HOUR=
SEED=
LOOP=false
DRY_RUN=false
INPUT_MODE=${LIBRARY_FILE:+library}

while (($#)); do
  case "$1" in
    --source)
      (($# >= 2)) || die "--source requires a directory"
      SOURCE_DIR=$2
      LIBRARY_FILE=
      INPUT_MODE=source
      shift 2
      ;;
    --library)
      (($# >= 2)) || die "--library requires a file"
      LIBRARY_FILE=$2
      INPUT_MODE=library
      shift 2
      ;;
    --rotation)
      (($# >= 2)) || die "--rotation requires a file"
      ROTATION_FILE=$2
      shift 2
      ;;
    --output)
      (($# >= 2)) || die "--output requires a file"
      OUTPUT_FILE=$2
      shift 2
      ;;
    --trigger)
      (($# >= 2)) || die "--trigger requires a file"
      TRIGGER_FILE=$2
      shift 2
      ;;
    --slot)
      (($# >= 2)) || die "--slot requires a duration"
      SLOT=$2
      shift 2
      ;;
    --daypart)
      (($# >= 2)) || die "--daypart requires a name"
      DAYPART=$2
      shift 2
      ;;
    --hour)
      (($# >= 2)) || die "--hour requires a value"
      HOUR=$2
      shift 2
      ;;
    --seed)
      (($# >= 2)) || die "--seed requires an integer"
      SEED=$2
      shift 2
      ;;
    --loop)
      LOOP=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1 (try --help)"
      ;;
  esac
done

[[ -f "$ROTATION_FILE" ]] || die "rotation file not found: $ROTATION_FILE"
[[ "$OUTPUT_FILE" == *.m3u ]] || die "--output must end in .m3u"
if [[ "$INPUT_MODE" == library ]]; then
  [[ -f "$LIBRARY_FILE" ]] || die "library file not found: $LIBRARY_FILE"
else
  [[ -d "$SOURCE_DIR" ]] || die "music directory not found: $SOURCE_DIR"
fi
if [[ -n "$HOUR" ]]; then
  [[ "$HOUR" =~ ^([0-9]|1[0-9]|2[0-3])$ ]] || die "--hour must be between 0 and 23"
fi
if [[ -n "$SEED" ]]; then
  [[ "$SEED" =~ ^-?[0-9]+$ ]] || die "--seed must be an integer"
fi

VENV_ACTIVATE=$REPO_DIR/.venv/bin/activate
[[ -f "$VENV_ACTIVATE" ]] || die "repository virtualenv not found: $VENV_ACTIVATE"
# shellcheck disable=SC1090
source "$VENV_ACTIVATE"
command -v playlistgen >/dev/null 2>&1 || die "playlistgen is not installed in $REPO_DIR/.venv"

OUTPUT_DIR=$(dirname -- "$OUTPUT_FILE")
SIDECAR_FILE=${OUTPUT_FILE%.m3u}.json
VALIDATOR=$ROOT_DIR/lib/playlist_loader.py
CURRENT_TEMP=

cleanup() {
  if [[ -n "$CURRENT_TEMP" && -d "$CURRENT_TEMP" ]]; then
    rm -rf -- "$CURRENT_TEMP"
  fi
}
trap cleanup EXIT INT TERM

print_command() {
  printf 'Dry run:'
  printf ' %q' "$@"
  printf '\n'
}

generate_once() {
  local run_hour run_seed temp_m3u temp_json
  local -a command input_args
  run_hour=${HOUR:-$(date +%H)}
  run_seed=${SEED:-$(( $(date +%s) / 3600 ))}

  if [[ "$INPUT_MODE" == library ]]; then
    input_args=(--library "$LIBRARY_FILE")
  else
    input_args=("$SOURCE_DIR")
  fi

  if $DRY_RUN; then
    command=(playlistgen "${input_args[@]}" --rotation "$ROTATION_FILE"
      --hour "$run_hour" --slot "$SLOT" --seed "$run_seed"
      --output "$OUTPUT_FILE")
    if [[ -n "$DAYPART" ]]; then
      command+=(--daypart "$DAYPART")
    fi
    print_command "${command[@]}"
    printf 'Would validate %q against %q and touch %q.\n' \
      "$SIDECAR_FILE" "$OUTPUT_FILE" "$TRIGGER_FILE"
    return
  fi

  mkdir -p -- "$OUTPUT_DIR" "$(dirname -- "$TRIGGER_FILE")"
  exec 9>"$OUTPUT_DIR/.playlistgen.lock"
  flock -n 9 || die "another playlist generation is already running"

  CURRENT_TEMP=$(mktemp -d "$OUTPUT_DIR/.playlistgen.XXXXXX")
  temp_m3u=$CURRENT_TEMP/playlist.m3u
  temp_json=$CURRENT_TEMP/playlist.json
  command=(playlistgen "${input_args[@]}" --rotation "$ROTATION_FILE"
    --hour "$run_hour" --slot "$SLOT" --seed "$run_seed"
    --output "$temp_m3u")
  if [[ -n "$DAYPART" ]]; then
    command+=(--daypart "$DAYPART")
  fi

  "${command[@]}"
  python "$VALIDATOR" --sidecar "$temp_json" --m3u "$temp_m3u" --require-files

  mv -f -- "$temp_json" "$SIDECAR_FILE"
  mv -f -- "$temp_m3u" "$OUTPUT_FILE"
  touch -- "$TRIGGER_FILE"
  rmdir -- "$CURRENT_TEMP"
  CURRENT_TEMP=
  printf 'Published %s and signaled %s.\n' "$OUTPUT_FILE" "$TRIGGER_FILE"
}

while true; do
  generate_once
  if ! $LOOP || $DRY_RUN; then
    break
  fi
  minute=$((10#$(date +%M)))
  second=$((10#$(date +%S)))
  wait_seconds=$((3600 - minute * 60 - second))
  printf 'Next generation in %ss at the next hour boundary.\n' "$wait_seconds"
  sleep "$wait_seconds"
done

