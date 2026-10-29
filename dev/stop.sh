#!/usr/bin/env bash
# Stop the dev HA instance started by dev/run.sh. Storage (dev/ha-config/.storage)
# must never be rewritten (dev/inject-displays.py, dev/restore.sh) while this
# process is still up, so both of those call this before touching any file.
set -euo pipefail

DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DEV_DIR/ha-config/.harness.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "stop: no PID file at $PID_FILE -- nothing to stop (or it was started some other way)."
  exit 0
fi

pid="$(cat "$PID_FILE")"
if ! kill -0 "$pid" 2>/dev/null; then
  echo "stop: PID $pid (from $PID_FILE) is not running -- removing stale PID file."
  rm -f "$PID_FILE"
  exit 0
fi

echo "stop: sending SIGTERM to pid $pid..."
kill "$pid"

deadline=$((SECONDS + 20))
while kill -0 "$pid" 2>/dev/null; do
  if ((SECONDS > deadline)); then
    echo "stop: pid $pid did not exit within 20s -- sending SIGKILL." >&2
    kill -9 "$pid" 2>/dev/null || true
    break
  fi
  sleep 1
done

rm -f "$PID_FILE"
echo "stop: done."
