#!/usr/bin/env bash
# Superseded by the radio engine manager on other platforms.
# This script works on Linux only. Use `radio stop` (Python, cross-platform)
# for Linux + Windows.  See liquidsoap/engine/ and liquidsoap/README.md.
set -Eeuo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
LOG_DIR=$ROOT_DIR/logs
ICECAST_PID_FILE=$LOG_DIR/icecast.pid
LIQUIDSOAP_PID_FILE=$LOG_DIR/liquidsoap.pid
RUNTIME_CONFIG=$LOG_DIR/icecast.runtime.xml
FORCE_ROOT=false

usage() {
  cat <<'EOF'
Usage: stop.sh [--force-root]

Stop Liquidsoap first, then Icecast. Running as root is refused unless
--force-root is explicit. Logs are retained; the rendered secret config and PID
files are removed after the processes stop.
EOF
}

die() {
  printf 'stop: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
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

stop_process() {
  local label=$1 pid_file=$2 expected=$3 pid cmdline
  if [[ ! -f "$pid_file" ]]; then
    printf '%s is not running (no PID file).\n' "$label"
    return 0
  fi
  read -r pid < "$pid_file" || pid=
  if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
    printf '%s has an invalid PID file; leaving it for inspection: %s\n' \
      "$label" "$pid_file" >&2
    return 1
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    printf '%s was already stopped; removing stale PID file.\n' "$label"
    rm -f -- "$pid_file"
    return 0
  fi

  cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
  if [[ "$cmdline" != *"$expected"* ]]; then
    printf 'Refusing to stop PID %s: it does not look like %s (%s).\n' \
      "$pid" "$label" "$cmdline" >&2
    return 1
  fi

  kill "$pid"
  for _ in {1..50}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f -- "$pid_file"
      printf 'Stopped %s (PID %s).\n' "$label" "$pid"
      return 0
    fi
    sleep 0.2
  done

  printf '%s did not stop after 10 seconds; sending SIGKILL.\n' "$label" >&2
  kill -KILL "$pid" 2>/dev/null || true
  rm -f -- "$pid_file"
}

status=0
stop_process Liquidsoap "$LIQUIDSOAP_PID_FILE" "$ROOT_DIR/scripts/" || status=1
stop_process Icecast "$ICECAST_PID_FILE" "$RUNTIME_CONFIG" || status=1

if ((status == 0)); then
  rm -f -- "$RUNTIME_CONFIG"
  printf 'Removed generated runtime config; logs were retained in %s.\n' "$LOG_DIR"
fi
exit "$status"

