#!/usr/bin/env bash
# Shared mapping between the three .storage files snapshot.sh/restore.sh
# touch and the seed/*.json files they produce/consume. Sourced, not run.
#
# HA storage key -> on-disk filename under .storage/, the JSON array path
# holding the records, and the field that uniquely identifies a record
# (used for upsert-by-id in restore.sh). These three keys have been stable
# across HA releases for years; get_storage_key/get_array_path/get_id_field
# below assert the expected "key" field in the file so a future rename fails
# loudly here instead of silently mis-filtering.

storage_seed_names=(config_entries device_registry entity_registry)

storage_file_for() {
  case "$1" in
  config_entries) echo "core.config_entries" ;;
  device_registry) echo "core.device_registry" ;;
  entity_registry) echo "core.entity_registry" ;;
  *)
    echo "storage-files.sh: unknown seed name '$1'" >&2
    return 1
    ;;
  esac
}

storage_array_path_for() {
  case "$1" in
  config_entries) echo ".data.entries" ;;
  device_registry) echo ".data.devices" ;;
  entity_registry) echo ".data.entities" ;;
  *)
    echo "storage-files.sh: unknown seed name '$1'" >&2
    return 1
    ;;
  esac
}

storage_id_field_for() {
  case "$1" in
  config_entries) echo "entry_id" ;;
  device_registry) echo "id" ;;
  entity_registry) echo "entity_id" ;;
  *)
    echo "storage-files.sh: unknown seed name '$1'" >&2
    return 1
    ;;
  esac
}

# Fail loudly if a .storage file's own "key" field doesn't match what we
# expect, rather than silently filtering the wrong structure.
assert_storage_key() {
  local seed_name="$1" file="$2" expected actual
  expected="$(storage_file_for "$seed_name")"
  actual="$(jq -r '.key // empty' "$file")"
  if [[ "$actual" != "$expected" ]]; then
    echo "ERROR: $file has key '$actual', expected '$expected'." \
      "HA's .storage schema for this file may have changed; script needs updating." >&2
    exit 1
  fi
}
