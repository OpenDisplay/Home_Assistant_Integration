#!/usr/bin/env bash
# Inject the dev/seed/*.json snapshot (see dev/snapshot.sh) into a fresh HA
# instance's .storage, so the opendisplay config entry / device / entities
# come back without repeating the real-hardware bootstrap. Idempotent:
# upserts by id, so re-running is a no-op past the first restore.
#
# CAVEAT: HA must be stopped while its .storage is rewritten underneath it —
# this script stops the dev instance itself (dev/stop.sh) before touching
# any file.
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

[[ -f "$SEED_DIR/config_entries.json" ]] || {
  echo "ERROR: $SEED_DIR/config_entries.json not found. Run dev/ha snapshot first" \
    "(after the one-time hardware device bootstrap), or copy a colleague's" \
    "dev/seed/ export there. For fabricated (no-hardware) devices, use" \
    "dev/inject-displays.py instead." >&2
  exit 1
}
[[ -f "$SEED_DIR/MANIFEST.json" ]] || {
  echo "ERROR: $SEED_DIR/MANIFEST.json missing — seed dir wasn't produced by snapshot.sh." >&2
  exit 1
}

mode="$(jq -r '.mode // "unknown"' "$SEED_DIR/MANIFEST.json")"
if [[ "$mode" != "filtered" ]]; then
  echo "WARNING: seed mode is '$mode', not 'filtered' — restore will overwrite whole" \
    "registries, not just the opendisplay slice. Any other integrations' entries in" \
    "this dev instance will be replaced by the snapshot's." >&2
fi

echo "restore: stopping the dev HA instance (storage must not be rewritten under a live process)..."
"$DEV_DIR/stop.sh"

[[ -d "$STORAGE_DIR" ]] || {
  echo "ERROR: $STORAGE_DIR not found. Run dev/ha run at least once (through onboarding)" \
    "before restoring, so HA has created its baseline registries." >&2
  exit 1
}

# Upsert seed/<name>.json's records into ha-config/.storage/<file> by id,
# keeping every existing record whose id isn't in the seed and replacing any
# that is. Preserves the target file's own version/minor_version/key envelope.
upsert_seed() {
  local seed_name="$1"
  local array_path id_field seed_file target_file tmp seed_array seed_ids total

  array_path="$(storage_array_path_for "$seed_name")" # e.g. ".data.entries"
  array_path="${array_path#.}"                        # -> "data.entries"
  id_field="$(storage_id_field_for "$seed_name")"
  seed_file="$SEED_DIR/$seed_name.json"
  target_file="$STORAGE_DIR/$(storage_file_for "$seed_name")"

  if [[ ! -f "$seed_file" ]]; then
    echo "restore: no $seed_name.json in seed (snapshot.sh warned about this) — skipping."
    return 0
  fi
  if [[ ! -f "$target_file" ]]; then
    echo "ERROR: $target_file not found. Run dev/ha run through onboarding first so HA" \
      "has created its baseline $seed_name store." >&2
    exit 1
  fi
  assert_storage_key "$seed_name" "$target_file"

  seed_array="$(jq -c ".$array_path" "$seed_file")"
  seed_ids="$(jq -c "[.[].$id_field]" <<<"$seed_array")"

  tmp="$(mktemp "$target_file.XXXXXX")"
  jq --argjson seed "$seed_array" --argjson ids "$seed_ids" \
    ".$array_path |= ((. // []) | map(select((.$id_field) as \$i | (\$ids | index(\$i)) == null)) + \$seed)" \
    "$target_file" >"$tmp"
  mv "$tmp" "$target_file"

  total="$(jq ".$array_path | length" "$target_file")"
  echo "restore: $seed_name -> merged $(jq 'length' <<<"$seed_ids") upserted, $total total in store."
}

for name in "${storage_seed_names[@]}"; do
  upsert_seed "$name"
done

echo "restore: done. Start the harness with dev/ha run — the opendisplay config entry" \
  "should set up from cache (no BLE needed) per __init__.py's sleepy-cache fallback."
