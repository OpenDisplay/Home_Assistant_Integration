# OpenDisplay local dev HA harness

One command gives you a local Home Assistant at `http://localhost:8123`
running this branch's `custom_components/opendisplay`, for manual exploration
— **no OpenDisplay hardware required**. `dev/inject-displays.py` fabricates
config entries for a small mono / medium BWR / large BWRY panel, each set up
from cache with no BLE connection (`_cache_setup_if_sleepy` in `__init__.py`);
`opendisplay.drawcustom` dry-run rendering is all CPU-side, no BLE either.

## Quickstart

```bash
dev/run.sh                             # 1. bring up HA, onboard
docker compose -f dev/docker-compose.yml down   # 2. stop (storage can't be
                                                 #    rewritten under a live
                                                 #    process)
uv run --group dev python dev/inject-displays.py  # 3. fabricate 3 devices
dev/run.sh                             # 4. bring HA back up
```

Open `http://localhost:8123`, log in, and the three fabricated devices are
there with `image`/`sensor`/`binary_sensor` entities. Call
`opendisplay.drawcustom` (Developer Tools → Actions) against one of them with
`dry-run: true` and its image entity updates immediately.

Re-running `dev/run.sh` is idempotent: it reuses the running container and
skips onboarding if a user already exists. Re-running `inject-displays.py`
replaces exactly the fabricated entries it created previously (matched by a
title prefix — see "Fabricated devices" below); it never touches a real
hardware-bootstrapped OpenDisplay entry or any other integration's entries.

Stop it: `docker compose -f dev/docker-compose.yml down`
Logs: `docker compose -f dev/docker-compose.yml logs -f`
Reset everything: `rm -rf dev/ha-config` (fully disposable, gitignored) then
`dev/run.sh` again.

## Fabricated devices (`dev/inject-displays.py`)

```bash
uv run --group dev python dev/inject-displays.py           # 3 devices (default)
uv run --group dev python dev/inject-displays.py --count 2 # fewer
```

Writes fabricated OpenDisplay config entries straight into
`dev/ha-config/.storage/core.config_entries` — no BLE, no config flow, no
device_registry/entity_registry seeding needed, because `__init__.py`'s own
`async_setup_entry` creates the device and platform entities itself on every
setup (see "Why no BLE connection happens" below). Each entry carries a
`CONF_CACHED_STATE` payload built from a real `opendisplay.GlobalConfig` (the
same model class the library uses for a real device), so `config_from_json`
round-trips it exactly like a real cached interrogation would:

| Device | Panel | Color scheme | Deep-sleep interval |
|---|---|---|---|
| Fabricated Small Mono Tag | 250×122 (2.13") | mono | 15 min |
| Fabricated Medium BWR Tag | 296×128 (2.9") | BWR | 5 min |
| Fabricated Large BWRY Tag | 800×480 (7.3") | BWRY | 30 min |

Each entry's `options` also forces `CONF_SLEEP_MODE = "on"` — redundant with
the genuine battery/deep-sleep `PowerOption` on top of it (either alone
already makes `SleepProfile.is_sleepy` resolve `True` under the default
"auto" mode), but it makes the sleepy-cache path unconditional, matching
exactly what the investigation behind this script needed to confirm.

**Must run with the dev HA container stopped** (same rule as `restore.sh`):
storage must not be rewritten under a live process. The script checks via
`docker compose ps` and fails loudly if the container is up.

**Idempotent by title marker**: every fabricated entry's title starts with
`OpenDisplay (dev-harness fabricated)`. A re-run finds and replaces exactly
those entries, leaving a real hardware-bootstrapped entry (titled
`OpenDisplay <MAC>`, no such prefix) or any other integration's entries
untouched.

**Fake but well-formed BLE addresses** (`AA:BB:CC:DD:EE:01`, `:02`, `:03`,
...) — never a real OUI. **Encryption key omitted** (`entry.data` carries no
`CONF_ENCRYPTION_KEY`): these are unencrypted devices, and the sleepy-cache
setup path never calls `_get_encryption_key` anyway (see below).

## Why no BLE connection happens (the investigation this script encodes)

`async_setup_entry` (`__init__.py`) starts with:

```python
ble_device = async_ble_device_from_address(hass, address, connectable=True)
```

This is a **lookup** against Bluetooth devices HA's `bluetooth` integration
has already discovered advertising — not a connection attempt. Docker
Desktop / Rancher Desktop on macOS gives the container no Bluetooth adapter
at all (`bluetooth_adapters` reports zero adapters), so nothing is ever
discovered and this call returns `None` **immediately**, every time. There is
no live-connect attempt to time out or hang on for a fabricated (or any
other) address in this environment — the `else` branch that does the bounded
`asyncio.timeout(SETUP_DEADLINE_S)` active connect is simply never reached.

With `ble_device is None`, setup goes straight to `_cache_setup_if_sleepy`:
cached config/firmware are loaded from `entry.data[CONF_CACHED_STATE]` and
`SleepProfile.from_entry` resolves `is_sleepy` from the entry's `options` and
the cached device's own power config (see the table above). If sleepy, setup
proceeds entirely from cache — the device registry entry, platform entities
(`image`/`sensor`/`binary_sensor`), and services all come up exactly as they
would for a real device, with the delivery manager left waiting on a wake
advertisement that, in this container, will simply never arrive (harmless:
it only sets flags and subscribes to a dispatcher signal — no reconnect
attempt happens synchronously or on a timer).

`opendisplay.drawcustom` with `dry-run: true` never touches BLE either: it
device-registry-looks-up the target, renders via `odl_renderer.generate_image`
(pure CPU), and dispatches the resulting JPEG straight to the image entity.
**Verified live** end to end (this session, real
`homeassistant/home-assistant:stable`, no mocking): after injecting 3
devices and rebooting, `opendisplay.drawcustom` dry-run against each fabricated
device's `device_id` (resolved via the `device_id()` Jinja function over the
REST `/api/template` endpoint — `device_registry.json` itself lags a save
debounce after boot) returned a service call with no error, the image
entity's state (`image_last_updated`) changed, and fetching
`entity_picture`'s `/api/image_proxy/...` URL returned a genuine JPEG at
**exactly** each device's fabricated resolution: 250×122, 296×128, and
800×480.

## Onboarding: what's scripted, what isn't, and why

Home Assistant's first-run onboarding wizard (create admin user, confirm
location, etc.) normally needs a few clicks in the browser. Two ways exist to
skip it:

- **Pre-seed `.storage/auth` + `.storage/auth_provider.homeassistant` with a
  hand-computed user record.** This is the pattern some HA devcontainer
  setups use. We deliberately did **not** ship this: it means committing (or
  scripting) a bcrypt hash into an internal, undocumented storage schema that
  `homeassistant/home-assistant:stable` can change under us at any pull —
  brittle in a way that fails silently (a stale/mismatched auth store can
  leave you locked out with no error explaining why).
- **Drive the public onboarding HTTP API** (`/api/onboarding/users`,
  `/auth/token`, `/api/onboarding/{core_config,analytics,integration}` —
  documented at <https://developers.home-assistant.io/docs/auth_api/> and
  `homeassistant/components/onboarding/views.py`). This is the same code path
  the wizard's own JS calls, so it isn't a schema hack, and it fails loudly
  (curl `-f` + `set -e`) rather than silently.

`dev/internal/onboarding.sh` (called by `run.sh`) does the latter. **Verified
live** against a real `homeassistant/home-assistant:stable` container in this
repo's dev environment: user creation, token exchange, and all four
onboarding steps completed successfully end to end.

If a future HA release changes this API, `run.sh` treats onboarding failure
as **non-fatal** — HA is still up and reachable, you just onboard by hand:
open `http://localhost:8123`, create a user (any name/password), click
through "Next" on the remaining steps, "Finish". ~30 seconds. `run.sh` prints
a loud warning telling you to do exactly this when the script fails.

Dev login is `dev` / `opendisplay-dev-harness` by default; override with
`HA_DEV_USERNAME` / `HA_DEV_PASSWORD` / `HA_DEV_NAME` env vars before running
`dev/run.sh`.

## Why `opendisplay.drawcustom` and friends show up with zero devices

`dev/ha-config/configuration.yaml` has a bare `opendisplay:` key. The
integration's `CONFIG_SCHEMA` is `cv.config_entry_only_config_schema` (no
YAML options supported), so this key doesn't configure anything and HA logs
an ERROR ("does not support YAML setup, please remove it") — but the
component's `async_setup()` still runs, which is what registers the
`opendisplay.*` services (`async_setup_services`, `__init__.py`) regardless
of whether any config entry exists yet. Once at least one config entry
exists (real or fabricated), HA calls `async_setup()`/`async_setup_entry()`
for the domain regardless of this key — so it only matters for a totally
pristine instance with zero entries. The ERROR log line is expected and
harmless — it's the mechanism, not a bug. **Verified live**: `curl` of
`/api/services` before any injection lists `opendisplay.drawcustom`,
`upload_image`, `activate_led`, `activate_buzzer`, `write_nfc`,
`play_melody`.

## BLE-in-Docker-on-macOS caveat

This container **cannot** reach real OpenDisplay hardware over BLE — Docker
Desktop / Rancher Desktop on macOS runs containers inside a Linux VM with no
Bluetooth passthrough, and `custom_components/opendisplay/config_flow.py`
hard-gates device creation on a live BLE connection test (no virtual/manual
path exists in this integration). `dev/inject-displays.py` sidesteps this
entirely by writing the config entry directly (see above) rather than going
through the config flow.

For the **real-hardware** path (`dev/snapshot.sh`/`dev/restore.sh` below),
the one-time bootstrap (adding the OpenDisplay integration via the UI and
pairing a device) needs to happen somewhere with real BLE reach to the
device — a Linux box with a Bluetooth adapter running this same
`custom_components/opendisplay` (bare `hass`, this compose file with
`network_mode: host` on Linux, or a full HA install), or the panel physically
near a machine's built-in adapter on a non-Docker-Desktop setup.

## Real-hardware snapshot / restore (`dev/snapshot.sh`, `dev/restore.sh`)

If you have real OpenDisplay hardware and want to capture its state instead
of (or alongside) fabricated devices:

```bash
dev/run.sh                # 1. bring up HA, onboard
#    2. ONE TIME, on hardware with real BLE reach (see caveat above):
#       add the OpenDisplay integration via the UI, pair your device.
dev/snapshot.sh            # 3. capture that device's config/device/entity
                            #    registry entries (opendisplay-domain only)
                            #    into dev/seed/*.json
#    ... dev/seed/*.json is gitignored (contains the device's BLE address
#    and encryption key) — keep it locally, or share it out-of-band with
#    a teammate who wants to skip their own hardware bootstrap.
dev/restore.sh              # 4. inject dev/seed/*.json into a fresh instance
                            #    (stops the compose stack itself first — HA
                            #    must not have .storage rewritten under it)
dev/run.sh                  # 5. bring HA back up; the opendisplay config
                            #    entry should set up from cache, no BLE
                            #    needed
```

`dev/restore.sh` upserts by id (never duplicates on repeat runs) and
preserves any other integrations' entries already in `dev/ha-config` — it
only touches records in the seed. `dev/snapshot.sh` fails loudly if no
opendisplay-domain config entry exists yet (nothing to capture) rather than
writing an empty/useless seed. Unlike `dev/inject-displays.py`, this path
also seeds `device_registry`/`entity_registry` records captured from the real
device — belt and suspenders on top of `__init__.py` recreating them itself,
matching what a real one-time bootstrap actually produced.

`dev/verify-scripts.sh` exercises `snapshot.sh`/`restore.sh`'s `jq`
filter+merge logic against synthetic fixtures in `dev/seed/fixtures/` (no
Docker, no HA, no hardware) — see "Verified vs UNVERIFIED" below for exactly
what this does and doesn't prove.

## Iterating on the integration

`custom_components/opendisplay` is mounted **read-only**, so nothing in the
container writes back into your checkout — edit files in your normal editor,
outside the container. Restart to pick up changes:
`docker compose -f dev/docker-compose.yml restart homeassistant`.

## Verified vs UNVERIFIED

**Verified live** (this session, against a real
`homeassistant/home-assistant:stable` container, no hardware, no mocking):

- `dev/run.sh` end to end: Docker checks, `docker compose up -d`, HA became
  reachable at `http://localhost:8123`; scripted onboarding
  (`dev/internal/onboarding.sh`) completed all four steps.
- `opendisplay:` in `configuration.yaml` → the same two-line mechanism as
  before injecting any device: the YAML-setup ERROR log line, and
  `GET /api/services` listing all six `opendisplay.*` services with zero
  config entries present.
- `dev/inject-displays.py` (3 devices, then re-run with `--count 2`, then
  `--count 3` again): wrote/replaced the fabricated entries in
  `dev/ha-config/.storage/core.config_entries`; the "must be stopped" guard
  correctly refused while the compose stack was up.
- Full boot with the 3 fabricated entries present: `GET /api/states` showed
  9 entities (`image`/`sensor`/`binary_sensor` × 3 devices) with **no BLE
  connection attempt, no hang, no error** — confirming the
  `async_ble_device_from_address` → `None` → `_cache_setup_if_sleepy` path
  described above.
- Device registry attributes for the fabricated devices (via the
  `device_attr()` Jinja function): `Seeed Studio / 2.9" BWR / OpenDisplay
  (dev-harness fabricated) Fabricated Medium BWR Tag (AA:BB:CC:DD:EE:02)` —
  matches the fabricated `GlobalConfig` exactly.
- `opendisplay.drawcustom` dry-run against all 3 fabricated devices (real
  `device_id`s, minimal payload `[{"type": "text", "value": "hi", "x": 10,
  "y": 10}]`): each call succeeded, each image entity's state timestamp
  updated, and each `entity_picture` URL returned a real JPEG at that
  device's exact fabricated resolution (250×122, 296×128, 800×480).
- `dev/snapshot.sh` / `dev/restore.sh` / `dev/verify-scripts.sh`: full
  filter → merge → idempotent-rerun cycle against the synthetic fixtures in
  `dev/seed/fixtures/`, including that unrelated (non-opendisplay) registry
  entries survive a restore untouched and the target files' own
  `version`/`minor_version`/`key` envelope is preserved.

**UNVERIFIED** (needs the maintainer's real OpenDisplay hardware — no
virtual/manual path exists in `config_flow.py`):

- The one-time real-hardware device bootstrap itself (pairing over BLE,
  `config_flow.py`'s connection probe).
- `dev/snapshot.sh` against a **real** device's `.storage` output — the
  fixture-based test in `dev/verify-scripts.sh` proves the `jq` logic is
  correct for the documented schema shape, not that a real capture matches
  it byte-for-byte.
- A **live-device** (rather than fabricated) sleepy-cache setup —
  `_cache_setup_if_sleepy` is the same code path either way, but this
  session only exercised it against fabricated `GlobalConfig`s.
- An actual delivery (non-dry-run) send to a real panel and the wake/queue
  behavior around it (`DeliveryManager`) — this harness's dry-run proof
  deliberately never reaches BLE, by design.
