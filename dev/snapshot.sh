#!/usr/bin/env bash
# Capture the opendisplay-domain slice of .storage/{config_entries,device_registry,
# entity_registry} into dev/seed/, after the maintainer has done the ONE-TIME
# real-hardware device bootstrap in this dev HA (see dev/README.md). Idempotent:
# re-running overwrites dev/seed/*.json with the current state.
set -euo pipefail

DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STORAGE_DIR="$DEV_DIR/ha-config/.storage"
SEED_DIR="$DEV_DIR/seed"
# shellcheck source=internal/storage-files.sh
source "$DEV_DIR/internal/storage-files.sh"

command -v jq >/dev/null 2>&1 || {
  echo "ERROR: jq is required (brew install jq)." >&2
  exit 1
}

[[ -d "$STORAGE_DIR" ]] || {
  echo "ERROR: $STORAGE_DIR not found. Run dev/run.sh and onboard at least once first." >&2
  exit 1
}

ce_file="$STORAGE_DIR/$(storage_file_for config_entries)"
[[ -f "$ce_file" ]] || {
  echo "ERROR: $ce_file not found — has HA started at least once?" >&2
  exit 1
}
assert_storage_key config_entries "$ce_file"

mkdir -p "$SEED_DIR"

# --- config_entries: keep only domain == "opendisplay" ------------------
entry_ids_json="$(jq -c '[.data.entries[] | select(.domain=="opendisplay") | .entry_id]' "$ce_file")"
entry_count="$(jq 'length' <<<"$entry_ids_json")"

if [[ "$entry_count" -eq 0 ]]; then
  echo "ERROR: no opendisplay-domain config entries found in $ce_file." >&2
  echo "Complete the one-time hardware device bootstrap (dev/README.md) before snapshotting," \
    "or use dev/inject-displays.py if you only need fabricated devices." >&2
  exit 1
fi

jq '.data.entries |= map(select(.domain=="opendisplay"))' "$ce_file" \
  >"$SEED_DIR/config_entries.json"
echo "snapshot: config_entries -> $entry_count opendisplay entr$([[ $entry_count -eq 1 ]] && echo y || echo ies)"

# --- device_registry: keep devices referencing a retained entry_id ------
dr_file="$STORAGE_DIR/$(storage_file_for device_registry)"
if [[ -f "$dr_file" ]]; then
  assert_storage_key device_registry "$dr_file"
  jq --argjson ids "$entry_ids_json" '
    .data.devices |= map(
      select((.config_entries // []) as $dce | any($ids[]; . as $id | $dce | index($id) != null))
    )
  ' "$dr_file" >"$SEED_DIR/device_registry.json"
  device_count="$(jq '.data.devices | length' "$SEED_DIR/device_registry.json")"
  echo "snapshot: device_registry -> $device_count device(s)"
else
  echo "WARNING: $dr_file not found; skipping device_registry (restore.sh will leave it untouched)." >&2
  rm -f "$SEED_DIR/device_registry.json"
fi

# --- entity_registry: keep entities referencing a retained entry_id -----
er_file="$STORAGE_DIR/$(storage_file_for entity_registry)"
if [[ -f "$er_file" ]]; then
  assert_storage_key entity_registry "$er_file"
  jq --argjson ids "$entry_ids_json" '
    .data.entities |= map(
      select(.config_entry_id as $id | $ids | index($id) != null)
    )
  ' "$er_file" >"$SEED_DIR/entity_registry.json"
  entity_count="$(jq '.data.entities | length' "$SEED_DIR/entity_registry.json")"
  echo "snapshot: entity_registry -> $entity_count entit$([[ $entity_count -eq 1 ]] && echo y || echo ies)"
else
  echo "WARNING: $er_file not found; skipping entity_registry (restore.sh will leave it untouched)." >&2
  rm -f "$SEED_DIR/entity_registry.json"
fi

jq -n --argjson ids "$entry_ids_json" --arg captured_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
  {
    captured_at: $captured_at,
    mode: "filtered",
    entry_ids: $ids,
    note: "Filtered to domain=opendisplay, referencing the entry_ids listed above."
  }
' >"$SEED_DIR/MANIFEST.json"

echo "snapshot: wrote dev/seed/*.json (dev/seed/MANIFEST.json records how)."
