# Fixtures

Synthetic `.storage` files shaped like Home Assistant's documented
`core.config_entries` / `core.device_registry` / `core.entity_registry`
schema, each carrying one `opendisplay`-domain record plus one unrelated
record (`met`). Used by `dev/verify-scripts.sh` to exercise the `jq` filter
and merge logic in `snapshot.sh` / `restore.sh` without real hardware.

These are **not** real device data — no genuine BLE address or encryption
key. Don't feed them to a real HA instance.
