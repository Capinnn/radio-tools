#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
LOG_DIR=$ROOT_DIR/logs
PORT=${ICECAST_PORT:-8000}
STREAM_URL="http://127.0.0.1:${PORT}/radio.mp3"
STATUS_URL="http://127.0.0.1:${PORT}/status-json.xsl"
DURATION=${SMOKE_DURATION:-20}

die() {
  printf 'smoke: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: smoke.sh [OPTIONS]

Run a full end-to-end smoke test: start the chain, capture 20 seconds of MP3
audio, verify Icecast reports a listener and audio/mpeg, then stop the chain.
Exits nonzero on any failure.

Options:
  --duration SECONDS   Capture duration (default: 20)
  --keep               Leave the chain running after the test (for debugging)
  -h, --help           Show this help
EOF
}

KEEP=false
while (($#)); do
  case "$1" in
    --duration)
      (($# >= 2)) || die "--duration requires a value"
      DURATION=$2
      shift 2
      ;;
    --keep)
      KEEP=true
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

[[ "$DURATION" =~ ^[0-9]+$ ]] || die "--duration must be a positive integer"
((DURATION >= 5)) || die "--duration must be at least 5 seconds"

printf '=== Radio smoke test (%ss capture) ===\n' "$DURATION"

# ---------------------------------------------------------------------------
# Start the chain
# ---------------------------------------------------------------------------
printf 'Starting chain...\n'
bash "$ROOT_DIR/bin/start.sh" || die "start.sh failed"

cleanup() {
  if ! $KEEP; then
    printf 'Stopping chain...\n'
    bash "$ROOT_DIR/bin/stop.sh" || printf 'smoke: stop.sh had errors\n' >&2
  fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Capture MP3 bytes for DURATION seconds
# ---------------------------------------------------------------------------
printf 'Capturing %ss of stream from %s ...\n' "$DURATION" "$STREAM_URL"
STREAM_FILE=$(mktemp -t radiosmoke.XXXXXX.bin)
trap 'rm -f -- "$STREAM_FILE"; cleanup' EXIT

# curl runs in the foreground for exactly DURATION+2 seconds (timeout) and
# writes raw bytes to the temp file. The open connection counts as a listener
# on the Icecast mount.
timeout $((DURATION + 2)) curl -s -m $((DURATION + 2)) "$STREAM_URL" > "$STREAM_FILE" 2>/dev/null || true

BYTES=$(wc -c < "$STREAM_FILE" | tr -d ' ')
printf 'Captured %s bytes of stream data.\n' "$BYTES"

# At 128 kbps, DURATION seconds should yield roughly DURATION * 16000 bytes.
# Require at least 50% of the theoretical minimum to allow for startup delay.
MIN_BYTES=$((DURATION * 8000))
if ((BYTES < MIN_BYTES)); then
  die "stream capture too short: $BYTES bytes (expected at least $MIN_BYTES for ${DURATION}s at 128kbps)"
fi

# Verify the captured data contains MP3 frame sync words (0xFF followed by a
# byte whose top 3 bits are set: 0xFB, 0xF3, 0xFA, 0xF2, 0xE? etc.).
if ! python3 "$ROOT_DIR/lib/check_mp3_sync.py" "$STREAM_FILE"; then
  die "no MP3 frame sync bytes found in captured stream data"
fi
printf 'MP3 frame sync bytes detected in stream data.\n'

# ---------------------------------------------------------------------------
# Query Icecast status JSON (with a fresh short-lived listener to ensure
# the listener count is at least 1)
# ---------------------------------------------------------------------------
# Re-connect briefly so the status snapshot sees a live listener.
timeout 5 curl -s -m 5 "$STREAM_URL" > /dev/null 2>&1 &
LISTENER_PID=$!
sleep 1

STATUS_FILE=$(mktemp -t radiosmoke.XXXXXX.json)
curl -s -m 5 "$STATUS_URL" > "$STATUS_FILE" 2>/dev/null || die "failed to fetch status-json.xsl"
trap 'rm -f -- "$STREAM_FILE" "$STATUS_FILE"; cleanup' EXIT

wait $LISTENER_PID 2>/dev/null || true

printf -- '--- Icecast status ---\n'
# Extract key fields from the JSON status via the parse_status helper.
python3 "$ROOT_DIR/lib/parse_status.py" "$STATUS_FILE" > /tmp/.radiosmoke_status_fields 2>/dev/null || true
LISTENERS=$(grep '^listeners=' /tmp/.radiosmoke_status_fields 2>/dev/null | cut -d= -f2)
SERVER_TYPE=$(grep '^server_type=' /tmp/.radiosmoke_status_fields 2>/dev/null | cut -d= -f2)
TITLE=$(grep '^title=' /tmp/.radiosmoke_status_fields 2>/dev/null | cut -d= -f2-)
BITRATE=$(grep '^audio_info=' /tmp/.radiosmoke_status_fields 2>/dev/null | cut -d= -f2-)
rm -f /tmp/.radiosmoke_status_fields

printf '  listeners:  %s\n' "$LISTENERS"
printf '  server_type: %s\n' "$SERVER_TYPE"
printf '  title:       %s\n' "$TITLE"
printf '  audio_info:  %s\n' "$BITRATE"

# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------
if [[ "$SERVER_TYPE" != "audio/mpeg" ]]; then
  die "server_type is '$SERVER_TYPE', expected 'audio/mpeg'"
fi

if [[ "$LISTENERS" == "?" || "$LISTENERS" -lt 1 ]]; then
  die "listener count is $LISTENERS, expected at least 1"
fi

printf '\n=== SMOKE TEST PASSED ===\n'
printf '  Stream:  %s bytes of MP3 audio captured over %ss\n' "$BYTES" "$DURATION"
printf '  Listener count: %s\n' "$LISTENERS"
printf '  Now-playing:    %s\n' "$TITLE"
printf '  Audio:          %s (%s)\n' "$SERVER_TYPE" "$BITRATE"