#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
LOG_DIR=$ROOT_DIR/logs
DATA_DIR=$ROOT_DIR/data
ICECAST_BIN=${ICECAST_BIN:-/usr/bin/icecast2}
LIQUIDSOAP_BIN=${LIQUIDSOAP_BIN:-/usr/bin/liquidsoap}
CONFIG_TEMPLATE=$ROOT_DIR/config/icecast.xml
SECRETS_FILE=$ROOT_DIR/config/secrets.env
RUNTIME_CONFIG=$LOG_DIR/icecast.runtime.xml
ICECAST_PID_FILE=$LOG_DIR/icecast.pid
LIQUIDSOAP_PID_FILE=$LOG_DIR/liquidsoap.pid
MODE=station
FORCE_ROOT=false

usage() {
  cat <<'EOF'
Usage: start.sh [--live] [--force-root]

Start Icecast and Liquidsoap. Rotation mode is the default; --live selects the
PulseAudio/JACK live-assist script. Credentials come from config/secrets.env or
the environment. Running as root is refused unless --force-root is explicit.
EOF
}

die() {
  printf 'start: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --live)
      MODE=live
      shift
      ;;
    --force-root)
      FORCE_ROOT=true
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

if ((EUID == 0)) && ! $FORCE_ROOT; then
  die "refusing to run as root; pass --force-root only if you accept the risk"
fi

[[ -x "$ICECAST_BIN" ]] || die "icecast2 executable not found: $ICECAST_BIN"
[[ -x "$LIQUIDSOAP_BIN" ]] || die "liquidsoap executable not found: $LIQUIDSOAP_BIN"
command -v envsubst >/dev/null 2>&1 || die "envsubst is required"
command -v curl >/dev/null 2>&1 || die "curl is required for startup checks"

EXTERNAL_SOURCE_PASSWORD=${ICECAST_SOURCE_PASSWORD:-}
EXTERNAL_ADMIN_PASSWORD=${ICECAST_ADMIN_PASSWORD:-}
EXTERNAL_RELAY_PASSWORD=${ICECAST_RELAY_PASSWORD:-}
EXTERNAL_HOSTNAME=${ICECAST_HOSTNAME:-}
EXTERNAL_PORT=${ICECAST_PORT:-}
if [[ -f "$SECRETS_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "$SECRETS_FILE"
  set +a
fi

# Explicit process environment values take precedence over the convenience
# secrets file.
if [[ -n "$EXTERNAL_SOURCE_PASSWORD" ]]; then
  ICECAST_SOURCE_PASSWORD=$EXTERNAL_SOURCE_PASSWORD
fi
if [[ -n "$EXTERNAL_ADMIN_PASSWORD" ]]; then
  ICECAST_ADMIN_PASSWORD=$EXTERNAL_ADMIN_PASSWORD
fi
if [[ -n "$EXTERNAL_RELAY_PASSWORD" ]]; then
  ICECAST_RELAY_PASSWORD=$EXTERNAL_RELAY_PASSWORD
fi
if [[ -n "$EXTERNAL_HOSTNAME" ]]; then
  ICECAST_HOSTNAME=$EXTERNAL_HOSTNAME
fi
if [[ -n "$EXTERNAL_PORT" ]]; then
  ICECAST_PORT=$EXTERNAL_PORT
fi

ICECAST_SOURCE_PASSWORD=${ICECAST_SOURCE_PASSWORD:-}
[[ -n "$ICECAST_SOURCE_PASSWORD" ]] || die \
  "set ICECAST_SOURCE_PASSWORD or create config/secrets.env"
ICECAST_ADMIN_PASSWORD=${ICECAST_ADMIN_PASSWORD:-$ICECAST_SOURCE_PASSWORD}
ICECAST_RELAY_PASSWORD=${ICECAST_RELAY_PASSWORD:-$ICECAST_SOURCE_PASSWORD}
ICECAST_HOSTNAME=${ICECAST_HOSTNAME:-radio.example.invalid}
ICECAST_PORT=${ICECAST_PORT:-8000}
ICECAST_HOST=${ICECAST_HOST:-127.0.0.1}

validate_secret() {
  local name=$1 value=$2
  ((${#value} >= 12)) || die "$name must be at least 12 characters"
  [[ "$value" =~ ^[-A-Za-z0-9._~!@%+=:,/]+$ ]] || die \
    "$name contains characters unsafe for XML environment substitution"
}
validate_secret ICECAST_SOURCE_PASSWORD "$ICECAST_SOURCE_PASSWORD"
validate_secret ICECAST_ADMIN_PASSWORD "$ICECAST_ADMIN_PASSWORD"
validate_secret ICECAST_RELAY_PASSWORD "$ICECAST_RELAY_PASSWORD"
[[ "$ICECAST_HOSTNAME" =~ ^[A-Za-z0-9.-]+$ ]] || die "ICECAST_HOSTNAME is invalid"
[[ "$ICECAST_PORT" =~ ^[0-9]+$ ]] || die "ICECAST_PORT must be numeric"
((ICECAST_PORT >= 1 && ICECAST_PORT <= 65535)) || die "ICECAST_PORT is out of range"

pid_running() {
  local pid_file=$1 pid
  [[ -f "$pid_file" ]] || return 1
  read -r pid < "$pid_file" || return 1
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

for pid_file in "$ICECAST_PID_FILE" "$LIQUIDSOAP_PID_FILE"; do
  if pid_running "$pid_file"; then
    die "already running (PID file: $pid_file)"
  fi
  rm -f -- "$pid_file"
done

mkdir -p -- "$LOG_DIR" "$DATA_DIR"
umask 077
export ICECAST_SOURCE_PASSWORD ICECAST_ADMIN_PASSWORD ICECAST_RELAY_PASSWORD
export ICECAST_HOSTNAME ICECAST_PORT ICECAST_HOST
export RADIO_LOG_DIR=$LOG_DIR
export RADIO_ROOT=$ROOT_DIR
export RADIO_PLAYLIST=${RADIO_PLAYLIST:-$DATA_DIR/playlist.m3u}
export RADIO_PLAYLIST_TRIGGER=${RADIO_PLAYLIST_TRIGGER:-$DATA_DIR/playlist.trigger}
touch -- "$RADIO_PLAYLIST_TRIGGER"

envsubst \
  '${ICECAST_SOURCE_PASSWORD} ${ICECAST_ADMIN_PASSWORD} ${ICECAST_RELAY_PASSWORD} ${ICECAST_HOSTNAME} ${ICECAST_PORT} ${RADIO_LOG_DIR}' \
  < "$CONFIG_TEMPLATE" > "$RUNTIME_CONFIG"
chmod 600 "$RUNTIME_CONFIG"

SCRIPT=$ROOT_DIR/scripts/$MODE.liq
if ! "$LIQUIDSOAP_BIN" --check "$SCRIPT" >> "$LOG_DIR/liquidsoap-check.log" 2>&1; then
  die "Liquidsoap validation failed; see $LOG_DIR/liquidsoap-check.log"
fi

terminate_pid() {
  local pid=${1:-}
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in {1..25}; do
      kill -0 "$pid" 2>/dev/null || return 0
      sleep 0.2
    done
    kill -KILL "$pid" 2>/dev/null || true
  fi
}

ICECAST_PID=
LIQUIDSOAP_PID=
abort_start() {
  local message=$1
  terminate_pid "$LIQUIDSOAP_PID"
  terminate_pid "$ICECAST_PID"
  rm -f -- "$LIQUIDSOAP_PID_FILE" "$ICECAST_PID_FILE" "$RUNTIME_CONFIG"
  die "$message"
}

nohup "$ICECAST_BIN" -c "$RUNTIME_CONFIG" </dev/null \
  >> "$LOG_DIR/icecast-console.log" 2>&1 &
ICECAST_PID=$!
printf '%s\n' "$ICECAST_PID" > "$ICECAST_PID_FILE"

icecast_ready=false
for _ in {1..40}; do
  if ! kill -0 "$ICECAST_PID" 2>/dev/null; then
    abort_start "Icecast exited during startup; see $LOG_DIR/icecast-console.log"
  fi
  if curl --silent --show-error --fail --max-time 1 \
    "http://127.0.0.1:$ICECAST_PORT/status-json.xsl" >/dev/null 2>&1; then
    icecast_ready=true
    break
  fi
  sleep 0.25
done
$icecast_ready || abort_start "Icecast did not become ready on port $ICECAST_PORT"

nohup "$LIQUIDSOAP_BIN" "$SCRIPT" </dev/null \
  >> "$LOG_DIR/liquidsoap.log" 2>&1 &
LIQUIDSOAP_PID=$!
printf '%s\n' "$LIQUIDSOAP_PID" > "$LIQUIDSOAP_PID_FILE"

source_ready=false
for _ in {1..60}; do
  if ! kill -0 "$LIQUIDSOAP_PID" 2>/dev/null; then
    abort_start "Liquidsoap exited during startup; see $LOG_DIR/liquidsoap.log"
  fi
  if curl --silent --show-error --fail --max-time 1 \
    "http://127.0.0.1:$ICECAST_PORT/status-json.xsl" 2>/dev/null \
      | grep -q '/radio.mp3'; then
    source_ready=true
    break
  fi
  sleep 0.25
done
$source_ready || abort_start "Liquidsoap did not connect /radio.mp3; see $LOG_DIR/liquidsoap.log"

printf 'Started Icecast (PID %s) and Liquidsoap %s mode (PID %s).\n' \
  "$ICECAST_PID" "$MODE" "$LIQUIDSOAP_PID"
printf 'Stream: http://127.0.0.1:%s/radio.mp3\n' "$ICECAST_PORT"
