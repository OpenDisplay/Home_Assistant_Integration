#!/usr/bin/env bash
# Exercises snapshot.sh / restore.sh's jq filter+merge logic against the
# synthetic fixtures in dev/seed/fixtures/ — no Docker, no HA, no hardware.
# This is the regression test for the storage-manipulation logic: it does NOT
# prove the scripts work against a REAL opendisplay device's .storage output
# (that needs real hardware; see dev/README.md "Verified vs UNVERIFIED").
#
# Runs entirely in a throwaway copy of dev/ under mktemp -d; never touches
# dev/ha-config or dev/seed in the real checkout.
set -euo pipefail

DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

cp -R "$DEV_DIR/." "$SANDBOX/"
STORAGE="$SANDBOX/ha-config/.storage"
mkdir -p "$STORAGE"

# --- snapshot.sh: filters a fixture "post-bootstrap" instance down to just
# the opendisplay-domain records, leaving the unrelated "met" fixture entry
# out. ------------------------------------------------------------------
cp "$SANDBOX/seed/fixtures/config_entries.json" "$STORAGE/core.config_entries"
cp "$SANDBOX/seed/fixtures/device_registry.json" "$STORAGE/core.device_registry"
cp "$SANDBOX/seed/fixtures/entity_registry.json" "$STORAGE/core.entity_registry"

"$SANDBOX/snapshot.sh"

jq -e '.data.entries | length == 1 and .[0].domain == "opendisplay"' \
  "$SANDBOX/seed/config_entries.json" >/dev/null ||
  fail "snapshot: config_entries.json should contain exactly the opendisplay entry"
jq -e '.data.devices | length == 1 and .[0].id == "od_fake_device_1"' \
  "$SANDBOX/seed/device_registry.json" >/dev/null ||
  fail "snapshot: device_registry.json should contain exactly the opendisplay device"
jq -e '.data.entities | length == 1 and .[0].entity_id == "image.opendisplay_aabbccddeeff_image"' \
  "$SANDBOX/seed/entity_registry.json" >/dev/null ||
  fail "snapshot: entity_registry.json should contain exactly the opendisplay entity"
jq -e '.mode == "filtered"' "$SANDBOX/seed/MANIFEST.json" >/dev/null ||
  fail "snapshot: MANIFEST.json should be valid JSON with mode=filtered"
echo "OK: snapshot.sh filters to the opendisplay domain only"

# --- restore.sh: merging the snapshot into a "fresh" instance that only has
# the unrelated met.no entry must add the opendisplay records back AND leave
# met.no's alone; running it twice must not duplicate anything. -----------
jq '.data.entries |= map(select(.domain=="met"))' \
  "$SANDBOX/seed/fixtures/config_entries.json" >"$STORAGE/core.config_entries"
jq '.data.devices |= map(select(.config_entries | index("met_fake_entry_1")))' \
  "$SANDBOX/seed/fixtures/device_registry.json" >"$STORAGE/core.device_registry"
jq '.data.entities |= map(select(.config_entry_id=="met_fake_entry_1"))' \
  "$SANDBOX/seed/fixtures/entity_registry.json" >"$STORAGE/core.entity_registry"

"$SANDBOX/restore.sh" >/dev/null
"$SANDBOX/restore.sh" >/dev/null # idempotency: must not duplicate

jq -e '[.data.entries[].entry_id] | sort == ["met_fake_entry_1", "od_fake_entry_1"]' \
  "$STORAGE/core.config_entries" >/dev/null ||
  fail "restore: config_entries should have exactly met + opendisplay, once each"
jq -e '[.data.devices[].id] | sort == ["met_fake_device_1", "od_fake_device_1"]' \
  "$STORAGE/core.device_registry" >/dev/null ||
  fail "restore: device_registry should have exactly met + opendisplay, once each"
jq -e '[.data.entities[].entity_id] | sort == ["image.opendisplay_aabbccddeeff_image", "weather.forecast_home"]' \
  "$STORAGE/core.entity_registry" >/dev/null ||
  fail "restore: entity_registry should have exactly met + opendisplay, once each"
jq -e '.version == 1 and .minor_version == 4 and .key == "core.config_entries"' \
  "$STORAGE/core.config_entries" >/dev/null ||
  fail "restore: config_entries envelope (version/minor_version/key) must be preserved"
echo "OK: restore.sh merges by id, preserves unrelated entries, idempotent"

echo "verify-scripts: all checks passed."
