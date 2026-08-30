#!/usr/bin/env bash
# One command: bring up a local Home Assistant with this branch's OpenDisplay
# integration mounted, reachable at http://localhost:8123. See dev/README.md
# for the full workflow.
set -euo pipefail

DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$DEV_DIR/docker-compose.yml"
HA_PORT="${HA_PORT:-8123}"
HA_URL="http://localhost:${HA_PORT}"

command -v docker >/dev/null 2>&1 || {
  echo "ERROR: docker not found on PATH. Install Docker Desktop (or Rancher Desktop) first." >&2
  exit 1
}
docker compose version >/dev/null 2>&1 || {
  echo "ERROR: 'docker compose' (the plugin, not standalone docker-compose) is required." >&2
  exit 1
}
docker info >/dev/null 2>&1 || {
  echo "ERROR: Docker daemon not reachable. Start Docker Desktop / Rancher Desktop and retry." >&2
  exit 1
}

already_running=false
if docker compose -f "$COMPOSE_FILE" ps --status running --services 2>/dev/null | grep -qx homeassistant; then
  already_running=true
fi

if [[ "$already_running" == false ]]; then
  # Port-free check: only fail if something OTHER than our own (stopped) stack
  # holds it — a bare TCP probe can't tell whose process that is, so this is
  # best-effort; `docker compose up` below is the authoritative check.
  if (exec 3<>"/dev/tcp/127.0.0.1/$HA_PORT") 2>/dev/null; then
    exec 3>&- 3<&-
    echo "ERROR: port $HA_PORT is already in use by something else." \
      "Free it, or set HA_PORT=<other-port> and rerun." >&2
    exit 1
  fi
fi

echo "run: starting the dev HA stack (docker compose up -d)..."
docker compose -f "$COMPOSE_FILE" up -d

echo "run: waiting for HA to answer at $HA_URL ..."
deadline=$((SECONDS + 120))
until curl -sf -o /dev/null "$HA_URL/manifest.json"; do
  if ((SECONDS > deadline)); then
    echo "ERROR: HA did not become reachable at $HA_URL within 120s." \
      "Check logs: docker compose -f dev/docker-compose.yml logs" >&2
    exit 1
  fi
  sleep 2
done
echo "run: HA is up."

echo "run: attempting scripted onboarding (idempotent, non-fatal if it fails)..."
if HA_URL="$HA_URL" "$DEV_DIR/internal/onboarding.sh"; then
  onboarded=true
else
  onboarded=false
  echo
  echo "WARNING: scripted onboarding failed (see error above)." \
    "HA is still up — onboard by hand in the browser (dev/README.md, ~30s)." >&2
  echo
fi

cat <<EOF

============================================================
 OpenDisplay dev HA:  $HA_URL
============================================================
EOF
if [[ "$onboarded" == true ]]; then
  cat <<EOF
Login: username '${HA_DEV_USERNAME:-dev}', password '${HA_DEV_PASSWORD:-opendisplay-dev-harness}'
  (override next time with HA_DEV_USERNAME / HA_DEV_PASSWORD env vars)
EOF
else
  echo "Onboarding wizard should be waiting for you in the browser."
fi
cat <<EOF

Next steps — no OpenDisplay hardware needed:
  1. Open $HA_URL and log in.
  2. Stop the stack (fabricated entries are written straight into HA's
     storage, which must not be rewritten under a live process):
       docker compose -f dev/docker-compose.yml down
  3. dev/inject-displays.py     — writes N fabricated OpenDisplay config
     entries (small mono / medium BWR / large BWRY, by default) into
     dev/ha-config/.storage/core.config_entries.
  4. dev/run.sh                 — bring HA back up; each fabricated entry
     sets up from its own cache with no BLE connection (sleepy-device
     fallback in __init__.py) and gets its device + entities created.

With real OpenDisplay hardware instead (BLE, not reachable from this macOS
Docker container — see dev/README.md):
  1. Add the OpenDisplay integration via the UI and pair the device on a
     machine with real BLE reach.
  2. dev/snapshot.sh   — capture that device's config/device/entity registry
     entries into dev/seed/ so you don't need hardware again.
  3. dev/restore.sh    — inject a dev/seed/ snapshot into a fresh instance.

Stop the stack:  docker compose -f dev/docker-compose.yml down
Logs:            docker compose -f dev/docker-compose.yml logs -f
EOF
