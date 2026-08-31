# OpenDisplay local dev HA harness

One command gives you a local Home Assistant at `http://127.0.0.1:8123`
running this branch's `custom_components/opendisplay`, for manual exploration
— **no OpenDisplay hardware required**. Native Python via `uv run hass` — no
Docker, no container runtime (maintainer ruling 2026-08-30: maximum KISS).
`dev/inject-displays.py` fabricates config entries for a small mono / medium
BWR / large BWRY panel, each set up from cache with no BLE connection
(`_cache_setup_if_sleepy` in `__init__.py`); `opendisplay.drawcustom` dry-run
rendering is all CPU-side, no BLE either.

**Use `127.0.0.1`, not `localhost`, in every URL** (tier-1 review, finding
3): macOS resolves `localhost` to `::1` (IPv6) first, and Home Assistant's
own auth/http stack can mismatch across that split — reproduced live as a
white designer panel with `/auth/token` failures logged against `::1`,
while `127.0.0.1` worked immediately with the identical config. `dev/run.sh`
itself only ever prints/probes `127.0.0.1` URLs; this is a caution for
typing a URL in by hand.

## Quickstart

```bash
dev/run.sh                                        # 1. bring up HA, onboard
dev/stop.sh                                       # 2. stop (storage can't be
                                                   #    rewritten under a live
                                                   #    process)
uv run --group dev python dev/inject-displays.py  # 3. fabricate 3 devices
dev/run.sh                                        # 4. bring HA back up
```

Open `http://127.0.0.1:8123`, log in, and the three fabricated devices are
there with `image`/`sensor`/`binary_sensor` entities. Call
`opendisplay.drawcustom` (Developer Tools → Actions) against one of them with
`dry-run: true` and its image entity updates immediately.

Re-running `dev/run.sh` is idempotent: it reuses the running process (tracked
by PID in `dev/ha-config/.harness.pid`) and skips onboarding if a user
already exists. Re-running `inject-displays.py` replaces exactly the
fabricated entries it created previously (matched by a title prefix — see
"Fabricated devices" below); it never touches a real hardware-bootstrapped
OpenDisplay entry or any other integration's entries.

Stop it: `dev/stop.sh` (or `Ctrl-C` if you started it in the foreground —
`dev/run.sh` itself always backgrounds it and hands control back).
Logs: `tail -f dev/ha-config/ha.log`
Reset everything: `dev/stop.sh && rm -rf dev/ha-config && dev/run.sh` --
everything under `dev/ha-config/` except `configuration.yaml` is generated
and gitignored (`.storage/`, `custom_components/` symlink, `*.log`/
`*.log.*`/`*.log.fault` -- macOS's Python crash dump, see the TCC caveat
below -- `home-assistant_v2.db*`, `.harness.pid`, `blueprints/`, `deps/`,
`tts/`, `.HA_VERSION`, `.ha_run.lock`), so wiping the whole directory is
always sufficient and never loses anything worth keeping.

## Minimal `configuration.yaml`

`dev/ha-config/configuration.yaml` does **not** use `default_config:` — that
pulls in the full discovery/scraping suite (`bluetooth`, `dhcp`, `ssdp`,
`zeroconf`, `usb`, `cloud`, `mobile_app`, ...), which this harness has no use
for and does not want silently probing the dev machine (maintainer ruling
2026-08-30). Instead it lists exactly what breaks without it:

| Key | Why |
|---|---|
| `homeassistant: country: US` | Kills the "Country not configured" Repair (tier-1 round 2, finding 3) — `hass.config.country` is `None` otherwise and `core_config.py` raises an issue for it on every boot. Arbitrary country code, no other significance. |
| `frontend:` | The Lovelace UI and the onboarding wizard's own assets are served through it — without it there is nothing to open in a browser, and `dev/internal/onboarding.sh`'s API calls 404. |
| `config:` | Developer Tools → Actions/States/Template, which is how this harness's own verification (calling `opendisplay.drawcustom`, reading `device_id()`/`device_attr()` Jinja) is done by hand. |
| `api:` | The plain REST API — `onboarding.sh` and this harness's own curl-based verification go through it directly. |

There is deliberately **no** `http:` key either (tier-1 round 2, finding 3):
an earlier version of this file had one,
`!include`ing a generated `http_port.yaml` to make a non-default `HA_PORT`
actually bind. Ripped out entirely: HA shows a "HTTP YAML configuration is
ignored after migration" Repair once onboarding stores its own network
config, meaning that `!include` was already silently ignored past the
first boot regardless of what it said. Port is always 8123.

There is deliberately **no** bare `opendisplay:` key (tier-1 review, finding
2 — an earlier version of this file had one; removed after it turned out to
raise a visible red "does not support YAML setup" Repair in the UI, not
just a harmless log line as that version assumed). See "Why the designer
panel and `opendisplay.*` services are absent before the first config
entry" below for what that actually costs.

Determined empirically: booted with exactly these keys plus `logger:`,
confirmed `GET /manifest.json` returns 200 and the onboarding API responds,
before adding anything else. No `bluetooth:` key exists anywhere in this
config — see "Why no BLE connection happens" below for what that buys.

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
| Fabricated Small Mono Tag | 200×200 (1.54") | mono | 15 min |
| Fabricated Medium BWR Tag | 296×128 (2.9") | BWR | 5 min |
| Fabricated Large BWRY Tag | 800×480 (7.3") | BWRY | 30 min |

Small Mono is 200×200, not the 2.13"-class 250×122 an earlier version of
this fixture used (tier-1 round 2, finding 4) — 250 isn't a multiple of 8,
and py-opendisplay's direct-write path warns on every MONO render/send at a
width that isn't byte-aligned (firmware sizes the upload from the raw pixel
count and truncates otherwise). 200 is; see `dev/inject-displays.py`'s own
comment on that `DisplayConfig` for the full explanation.

Each entry's `options` also forces `CONF_SLEEP_MODE = "on"` — redundant with
the genuine battery/deep-sleep `PowerOption` on top of it (either alone
already makes `SleepProfile.is_sleepy` resolve `True` under the default
"auto" mode), but it makes the sleepy-cache path unconditional, matching
exactly what the investigation behind this script needed to confirm.

**Must run with the dev HA instance stopped** (same rule as `restore.sh`):
storage must not be rewritten under a live process. The script checks by
reading `dev/ha-config/.harness.pid` and testing whether that process is
still alive, and fails loudly if it is.

**Idempotent by title marker**: every fabricated entry's title starts with
`OpenDisplay (dev-harness fabricated)`. A re-run finds and replaces exactly
those entries, leaving a real hardware-bootstrapped entry (titled
`OpenDisplay <MAC>`, no such prefix) or any other integration's entries
untouched.

**Fake but well-formed BLE addresses** (`AA:BB:CC:DD:EE:01`, `:02`, `:03`,
...) — never a real OUI. **Encryption key omitted** (`entry.data` carries no
`CONF_ENCRYPTION_KEY`): these are unencrypted devices, and the sleepy-cache
setup path never calls `_get_encryption_key` anyway (see below).

## The designer panel and render endpoint

This branch's own feature, not inherited from `dev-harness` — no OpenDisplay
hardware needed, fabricated devices (above) work fine as targets:

1. `dev/run.sh` (onboarded, at least one fabricated or real device present).
2. Open `http://127.0.0.1:8123`, log in, sidebar → **OpenDisplay Designer**.
3. Pick a display from the **Display** picker (a fabricated device from
   above works) — the canvas resolution/color mode adopt that device's real
   published capabilities and the picker locks.
4. Edit the YAML or use the canvas toolbar to add an element. Toggle
   **Display preview** on — the canvas replaces its client-side render with
   a real server-rendered image; the browser's network panel shows
   `POST /api/opendisplay/designer/render → 200 OK`. See
   [`docs/designer.md`](../docs/designer.md) for the full contract and the
   preview-isolation guarantee (a live dashboard showing that same device's
   `image.*` entity never changes because of this).
5. Click **Send to display** to actually send via the real
   `opendisplay.drawcustom` service — unlike step 4, this **does** update
   the target's `image.*` entity (and, for a real/awake device, actually
   delivers).

To call the render endpoint directly instead of through the panel, get a
token the same way the panel's own `hass.fetchWithAuth` does (your own
**Profile page → Security tab → Long-Lived Access Tokens**, or drive
`/auth/login_flow` → `/auth/token` by hand the way `dev/run.sh`'s own
`fetch_dev_access_token` does for the dev user):

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  http://127.0.0.1:8123/api/opendisplay/designer/render \
  -d '{"device_id":"<id>","payload":[{"type":"text","value":"hi","x":10,"y":10}]}' \
  -o preview.png
```

`<id>` is an HA **device registry** id (not an entity id) — resolve one via
`/api/template` with `{{ device_id('image.your_entity_id') }}`, or read it
off a fabricated device's `image.*` entity attributes (`device_id` is
published there — see "Why no BLE connection happens" below for what else
lives in those attributes).

## Why no BLE connection happens (the investigation this script encodes)

`async_setup_entry` (`__init__.py`) starts with:

```python
ble_device = async_ble_device_from_address(hass, address, connectable=True)
```

This is a **lookup** against Bluetooth devices HA's `bluetooth` integration
has already discovered advertising — not a connection attempt. This harness's
`configuration.yaml` never loads that integration at all (no
`default_config`, no explicit `bluetooth:` key), so nothing is ever
discovered and this call returns `None` **immediately**, every time. There is
no live-connect attempt to time out or hang on for a fabricated (or any
other) address — the `else` branch that does the bounded
`asyncio.timeout(SETUP_DEADLINE_S)` active connect is simply never reached.

With `ble_device is None`, setup goes straight to `_cache_setup_if_sleepy`:
cached config/firmware are loaded from `entry.data[CONF_CACHED_STATE]` and
`SleepProfile.from_entry` resolves `is_sleepy` from the entry's `options` and
the cached device's own power config (see the table above). If sleepy, setup
proceeds entirely from cache — the device registry entry, platform entities
(`image`/`sensor`/`binary_sensor`), and services all come up exactly as they
would for a real device, with the delivery manager left waiting on a wake
advertisement that will simply never arrive here (harmless: it only sets
flags and subscribes to a dispatcher signal — no reconnect attempt happens
synchronously or on a timer).

`opendisplay.drawcustom` with `dry-run: true` never touches BLE either: it
device-registry-looks-up the target, renders via `odl_renderer.generate_image`
(pure CPU), and dispatches the resulting JPEG straight to the image entity.

### macOS / CoreBluetooth caveat — TCC crashes the process (important, verified)

`opendisplay`'s manifest depends on `bluetooth_adapters` (the adapter
enumeration library several HA Bluetooth integrations share) — a hard
manifest dependency, unconditionally set up by HA whether or not
`configuration.yaml` mentions `bluetooth` at all, so nothing here can turn
it off.

On real macOS (unlike the old Docker-on-Linux-VM setup, which had no
Bluetooth device node at all, so `bleak` never touched real hardware),
`bluetooth_adapters` engages `bleak`'s CoreBluetooth backend for real, and
**macOS's TCC (privacy/consent) framework hard-aborts the process** the
first time it does — this is not a Python exception and cannot be caught:

```
Fatal Python error: Aborted
...
File "...pycares/__init__.py", line 430 in _run_safe_shutdown_loop
...
Binary file ".../TCC.framework/.../TCC", at __TCC_CRASHING_DUE_TO_PRIVACY_VIOLATION__
```

**Root cause**: TCC's Bluetooth permission can only be granted to a real
signed `.app` bundle with an `NSBluetoothAlwaysUsageDescription` entry in its
`Info.plist`. A bare `python3` binary in a `uv`-managed venv has neither, so
there is no permission to grant in System Settings at all — when Bluetooth
access for the process is denied, TCC's answer is to kill it (`SIGABRT`).
This is a long-standing, widely reported constraint on running Home
Assistant Core natively on macOS (it's exactly why the HA core devcontainer
targets Linux/Docker), **not** anything specific to this integration, this
PR, or this harness's `configuration.yaml` — a minimal `bluetooth_adapters:`
-only config with zero other integrations crashes the same way.

**Verified live**, in two separate sessions on the same development
machine: reproduced with `bluetooth_adapters:` as the *only* non-core key in
`configuration.yaml` (no `opendisplay`, no other integration) — same
`Fatal Python error: Aborted` / `__TCC_CRASHING_DUE_TO_PRIVACY_VIOLATION__`
crash, confirming this is a platform property, not something this
integration triggers.

**What is actually known about timing**: the abort is nondeterministic,
happening on some boot attempts and not others on the very same machine and
configuration. An adversarial-review pass measured 7 `dev/run.sh` attempts
on one development machine, with a mix of outcomes across them (including
at least one abort and at least two clean successes that finished
onboarding against a fresh config) — not a consistent "always aborts" or
"always succeeds," and not enough of a sample to state a rate with any
confidence either way. **Do not plan around either a "short runs are safe"
or a "this machine is broken" assumption** — measure on the machine in
question before relying on this harness for anything time-sensitive, and
do not trust an unsupported specific fraction (an earlier draft of this
section claimed measured failure counts and an escalating-likelihood
pattern that later re-measurement did not actually support; both have been
removed).

**`dev/run.sh` never claims success without checking again, right before
printing the banner**: it re-verifies the tracked PID is alive AND the port
answers immediately before the "OpenDisplay dev HA" banner, and — if either
check fails — looks for the crash signature **only** in the macOS Python
fault dump (`home-assistant.log.fault`), and **only** for the two
TCC-specific strings `__TCC_CRASHING_DUE_TO_PRIVACY_VIOLATION__` or `Fatal
Python error` — never merely "a `.log.fault` file exists" (HA creates one
empty on ordinary healthy boots too, via Python's own faulthandler
pre-registration), and, since adversarial-review round 3, never a grep of
the regular `ha.log` for a bare "bluetooth" substring either: that arm was
removed after being reproduced as a false positive — routine, unrelated
cascade lines (HA's own "Setup failed for 'bluetooth': Could not setup
dependencies", or `_drawcustom_for_device`'s own "Could not find Bluetooth
device with address ..." reachability diagnostic) both contain the word
and both matched, so a plain port-8123-bind failure with nothing to do
with TCC printed the full TCC remediation. A plain `kill -9`, a port
conflict, or an unrelated crash now gets a generic "HA process died"
message plus the actual log tail instead of the TCC-specific text. It also
asserts the actual reason this harness exists before that banner — any
injected config entries reach `state: loaded`, the designer panel's static
view answers 200, and (once entries exist) the render endpoint answers 200
for one of their devices — not just that the web server itself answers,
since HA can be "up" while `opendisplay` itself failed to set up entirely;
every check in this cascade, including the bounded panel-registration poll
and the dev-token/config-entries/device-id lookups that follow it, checks
process liveness before printing any "skipped" message, so a death in that
window is reported as a death, never silently misdiagnosed as an
integration-setup failure or swallowed on the way to the banner (both
reproduced live and fixed in adversarial-review round 3). Onboarding
itself can also leave a **wedged half-onboarded state** if the process
aborts mid-onboarding — in either of two shapes: `.storage/onboarding`
records a step (e.g. "user") done but `.storage/auth` has no real user
written, or (a process killed even earlier) `.storage/auth_provider.
homeassistant` exists with neither `.storage/auth` nor `.storage/onboarding`
written at all yet. `dev/internal/onboarding.sh` checks storage for both
shapes **unconditionally**, before looking at what `/api/onboarding` itself
reports (an earlier version of this check ran only when `/api/onboarding`
404s, which missed the first shape entirely whenever it instead showed up
as a 200 with just the "user" step done — reproduced and fixed), and fails
loudly with a `rm -rf`-and-retry instruction instead of reporting "already
onboarded" against a login nobody can actually use.

**What is known, and what isn't, about a fix**: the only known remediation
to try is System Settings → Privacy & Security → Bluetooth → grant access
to the terminal application running `uv` (Terminal.app, iTerm2, etc.), then
retry `dev/run.sh`. **UNVERIFIED** whether this actually prevents the
abort — this session has no way to open System Settings or grant a
permission from its own sandboxed shell, so this has never actually been
tested. It may not even be *available* to grant: TCC's Bluetooth permission
model expects a signed `.app` bundle declaring
`NSBluetoothAlwaysUsageDescription` in its `Info.plist`, which a bare
`uv`-managed venv process has none of, so macOS may have nothing grantable
to offer a plain `python3` binary regardless of which terminal app launched
it. Success **without** granting anything is also possible — this is
nondeterministic, not "granted or it always fails" — just not reliable
enough to plan around. If you try this and it works (or doesn't), that is
worth recording here for the next person on this repo — right now this
section states what was measured, not what the fix is.

**This is a real limitation of running this harness natively on macOS**,
independent of the Docker→native change otherwise requested — flagged
prominently rather than worked around silently, since no code change in
this repository can fix a TCC entitlement gap it has never been able to
verify a workaround for. Options, in rough order of effort: try the
Bluetooth-permission remediation above and report back; package `hass`
inside a minimal signed `.app` bundle declaring
`NSBluetoothAlwaysUsageDescription` (out of scope for this harness); or
accept a container runtime for local dev specifically to avoid real
CoreBluetooth access (the tradeoff the previous Docker-based harness made,
whether or not that was the reason originally stated for it).

## Onboarding: what's scripted, what isn't, and why

Home Assistant's first-run onboarding wizard (create admin user, confirm
location, etc.) normally needs a few clicks in the browser. Two ways exist to
skip it:

- **Pre-seed `.storage/auth` + `.storage/auth_provider.homeassistant` with a
  hand-computed user record.** This is the pattern some HA devcontainer
  setups use. We deliberately did **not** ship this: it means committing (or
  scripting) a bcrypt hash into an internal, undocumented storage schema that
  a Home Assistant release can change under us at any pull — brittle in a way
  that fails silently (a stale/mismatched auth store can leave you locked out
  with no error explaining why).
- **Drive the public onboarding HTTP API** (`/api/onboarding/users`,
  `/auth/token`, `/api/onboarding/{core_config,analytics,integration}` —
  documented at <https://developers.home-assistant.io/docs/auth_api/> and
  `homeassistant/components/onboarding/views.py`). This is the same code path
  the wizard's own JS calls, so it isn't a schema hack, and it fails loudly
  (curl `-f` + `set -e`) rather than silently.

`dev/internal/onboarding.sh` (called by `run.sh`) does the latter, against
`http://127.0.0.1:8123` — the same public HTTP API regardless of whether HA
is running in a container or natively, so nothing about it changed with the
move off Docker.

If a future HA release changes this API, `run.sh` treats onboarding failure
as **non-fatal** — HA is still up and reachable, you just onboard by hand:
open `http://127.0.0.1:8123`, create a user (any name/password), click
through "Next" on the remaining steps, "Finish". ~30 seconds. `run.sh` prints
a loud warning telling you to do exactly this when the script fails.

Dev login is `dev` / `opendisplay-dev-harness` by default; override with
`HA_DEV_USERNAME` / `HA_DEV_PASSWORD` / `HA_DEV_NAME` env vars before running
`dev/run.sh`.

## Why the designer panel and `opendisplay.*` services are absent before the first config entry

`dev/ha-config/configuration.yaml` deliberately has **no** bare
`opendisplay:` key (tier-1 review, finding 2 — see that key's own removal
comment in `configuration.yaml`). The integration's `CONFIG_SCHEMA` is
`cv.config_entry_only_config_schema` (no YAML options supported), and an
earlier version of this file had the bare key anyway on the theory that
it's what makes HA call the domain's `async_setup()` (which registers both
`async_setup_services` and the designer panel's own
`async_setup_designer()`, `custom_components/opendisplay/__init__.py`)
even with zero config entries. That's true, but a bare domain key for a
config-entry-only integration also raises a visible red "does not support
YAML setup" Repair in Home Assistant's own UI — not just the harmless log
line that version assumed; the maintainer hit it live.

The key was never actually load-bearing: HA calls
`async_setup()`/`async_setup_entry()` for a domain on its own once at least
one config entry exists for it (real or fabricated), regardless of whether
a bare YAML key was ever present — **verified live this round**: a totally
pristine boot (no `opendisplay:` key, zero config entries) has neither the
`opendisplay.*` services nor the designer sidebar panel; after
`dev/inject-displays.py` and a restart, both appear, with no
"does not support YAML setup" error anywhere in the log. So removing the
key costs exactly one thing — the services/panel aren't available on a
totally fresh, zero-entry instance until the first entry exists — and
that's the normal `dev/run.sh` → `dev/stop.sh` → inject →
`dev/run.sh` workflow anyway (see "Fabricated OpenDisplay devices" above),
not an extra step.

## Expected warnings (harmless, cannot be removed)

Two warnings show up on every boot of this harness and are not fixable from
here — both are inherent to what this harness *is*, not a config gap:

- **"Not running on a supported system"** / installation-type warning — HA
  core distinguishes a handful of supported installation types (Home
  Assistant OS, Supervised, Container, Core-via-official-Docker-image); a
  bare `hass` process launched by `uv run` (this harness's entire point --
  native Python, no container runtime, maintainer ruling 2026-08-30) is
  none of those. There is no config key or flag that changes what
  installation type HA detects itself running under.
- **"We found a custom integration opendisplay which has not been tested by
  Home Assistant"** (`homeassistant.loader`, logged once per boot) — every
  `custom_components/` integration not shipped in HA core gets this,
  unconditionally; it is what "custom integration" *means* to HA's loader,
  not something this integration's manifest can opt out of.

The `country`/`http:` fixes above (tier-1 round 2) removed the Repairs that
WERE fixable (country-not-configured, the deprecated `http:` YAML include);
these two are not in that category — expect them, ignore them.

## Real-hardware bootstrap needs real BLE reach

`config_flow.py` hard-gates device creation on a live BLE connection test (no
virtual/manual path exists in this integration), so the one-time real-device
bootstrap (adding the OpenDisplay integration via the UI, pairing a device)
needs to happen on a machine with actual Bluetooth reach to the panel — this
dev harness itself, run natively, has whatever BLE reach the host machine
has (unlike the old Docker setup, which had none at all on macOS). If your
dev machine has no usable Bluetooth adapter, do the one-time bootstrap on one
that does, then `dev/snapshot.sh`/`dev/restore.sh` (below) to carry the
result anywhere else. `dev/inject-displays.py` sidesteps this entirely by
writing the config entry directly (see above) rather than going through the
config flow.

## Real-hardware snapshot / restore (`dev/snapshot.sh`, `dev/restore.sh`)

If you have real OpenDisplay hardware and want to capture its state instead
of (or alongside) fabricated devices:

```bash
dev/run.sh                # 1. bring up HA, onboard
#    2. ONE TIME, on a machine with real BLE reach (see above):
#       add the OpenDisplay integration via the UI, pair your device.
dev/snapshot.sh            # 3. capture that device's config/device/entity
                            #    registry entries (opendisplay-domain only)
                            #    into dev/seed/*.json
#    ... dev/seed/*.json is gitignored (contains the device's BLE address
#    and encryption key) — keep it locally, or share it out-of-band with
#    a teammate who wants to skip their own hardware bootstrap.
dev/restore.sh              # 4. inject dev/seed/*.json into a fresh instance
                            #    (stops the dev HA instance itself first — HA
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
filter+merge logic against synthetic fixtures in `dev/seed/fixtures/` (no HA,
no hardware) — see "Verified vs UNVERIFIED" below for exactly what this does
and doesn't prove.

## Iterating on the integration

`custom_components/opendisplay` runs straight from your checkout (`uv run
--group dev hass --config dev/ha-config` — no mount, no container, nothing
copied) — edit files in your normal editor and a debugger attaches directly
into the running process, same as any other native Python program. Restart to
pick up changes: `dev/stop.sh && dev/run.sh`.

`dev/run.sh` and `scripts/test --min-ha` both resolve the same shared
`.venv` but pin conflicting Home Assistant versions (`dev` vs `min-ha` —
`pyproject.toml`'s own `[tool.uv] conflicts`); don't run them interleaved/
concurrently, and if you do and something looks wrong afterwards, rebuild
the venv (`rm -rf .venv && uv sync --group dev`) rather than debugging a
half-resolved environment.

## Custom component discovery: `custom_components/` must live in the config dir

Home Assistant only looks for `custom_components/` **inside its own config
directory** (`--config`) — it adds exactly that directory to `sys.path`, it
never walks up from wherever `hass` happens to be invoked. `dev/run.sh`
symlinks `dev/ha-config/custom_components/opendisplay` to this checkout's
`custom_components/opendisplay` on every run (kept in sync automatically —
it's a symlink, not a copy) so this branch's integration is actually the one
that loads.

**Verified live this session that this matters**: without the symlink, HA
2026.8's own core-shipped `opendisplay` integration (a *different*,
version-less implementation bundled with Home Assistant itself — check
`homeassistant/components/opendisplay/manifest.json` in your installed HA
version) silently answers the domain instead, with no error indicating a
mismatch. Fabricated config entries created by `dev/inject-displays.py` went
into `setup_retry` under the core integration (its own BLE-lookup diagnostic
message, not ours) with zero entities created; after the symlink exists,
all three fabricated entries reach `state: loaded` and their `image`
entities appear.

## Verified vs UNVERIFIED

The first list below is what the original implementation session verified —
all still literally true observations from real runs, but see the "macOS /
CoreBluetooth caveat" section above before treating "it booted this time" as
"it reliably boots": the abort is nondeterministic (measured across a mix
of outcomes on the same machine, not a consistent always-fails or
always-succeeds), so a clean run proves the harness works, not that it
will again next time. Read both lists, not just the first, before drawing
a conclusion about reliability.

**Verified live** (original implementation session, native `uv run hass`,
no Docker, no real OpenDisplay hardware, no mocking):

- `dev/run.sh` end to end: Python-version check, `custom_components`
  symlink creation, PID-file tracking, HA became reachable at
  `http://127.0.0.1:8123`; scripted onboarding (`dev/internal/onboarding.sh`)
  completed all four steps from a totally fresh `dev/ha-config`.
- Minimal `configuration.yaml` (`frontend:`/`config:`/`api:`/`opendisplay:`,
  no `default_config:`, no `bluetooth:`) boots cleanly: `GET /manifest.json`
  → 200. **This session's `configuration.yaml` had a bare `opendisplay:`
  key; tier-1 review later removed it (finding 2, above) after it turned
  out to raise a visible Repair in the UI, not just the harmless log line
  this session assumed** — the two bullets below describe that
  since-removed key's behavior, kept here as the historical record of what
  was actually run at the time.
- `opendisplay:` in `configuration.yaml` → the same two-line mechanism as
  before injecting any device: the YAML-setup ERROR log line, and
  `GET /api/services` listing all six `opendisplay.*` services with zero
  config entries present.
- `dev/inject-displays.py` (3 devices): wrote the fabricated entries into
  `dev/ha-config/.storage/core.config_entries`; the "must be stopped" guard
  correctly refused while `dev/run.sh`'s hass process was up, based on the
  PID file, and a fresh re-run after a real stop succeeded.
- Full boot with the 3 fabricated entries present, **with the
  `custom_components` symlink in place**: all three entries reached
  `state: loaded` via `GET /api/config/config_entries/entry`; `GET
  /api/states` showed 13 opendisplay-domain entities including all 3
  `image.*` entities, with **no BLE connection attempt, no hang** in the log
  — confirming the `async_ble_device_from_address` → `None` →
  `_cache_setup_if_sleepy` path described above.
- `opendisplay.drawcustom` dry-run against the fabricated Small Mono Tag
  (real `device_id` resolved via the `device_id()` Jinja function, payload
  `[{"type": "text", "value": "hi", "x": 10, "y": 10}]`): the call succeeded
  (empty result list, no error), the image entity's state timestamp updated,
  and its `entity_picture` URL (`/api/image_proxy/...`) returned a real JPEG
  at **exactly** that device's fabricated resolution, 250×122.
- `dev/stop.sh`: SIGTERM to the tracked PID, process exits, `/manifest.json`
  stops responding, PID file removed; re-running `dev/stop.sh` against an
  already-stopped instance reports "nothing to stop" rather than erroring.
- The TCC/CoreBluetooth hard-abort described above: reproduced directly
  (full Python fault trace showing `Fatal Python error: Aborted` and
  `__TCC_CRASHING_DUE_TO_PRIVACY_VIOLATION__`), including in isolation with
  `bluetooth_adapters:` as the only non-core config key and no opendisplay
  integration at all — confirms it is a macOS/TCC platform property, not
  something this integration or its designer-panel additions introduce.
- `dev/snapshot.sh` / `dev/restore.sh` / `dev/verify-scripts.sh`: full
  filter → merge → idempotent-rerun cycle against the synthetic fixtures in
  `dev/seed/fixtures/`, including that unrelated (non-opendisplay) registry
  entries survive a restore untouched and the target files' own
  `version`/`minor_version`/`key` envelope is preserved.

**Verified live** (first adversarial-review round, same machine, later
session):

- The reliability claim in the first list above was overclaimed: real
  aborts do happen, on some attempts, on the same machine and
  configuration that also boots cleanly on other attempts.
- `dev/run.sh`'s liveness check (re-verify the tracked PID AND a fresh port
  probe immediately before the success banner, not just inside the earlier
  wait loop): reproduced the exact failure this exists for — onboarding's
  own HTTP calls succeeded, then the process aborted before the banner,
  and the script correctly printed a crash diagnosis instead of a false
  "HA is up".
- `dev/internal/onboarding.sh`'s `remove_onboarding_added_integrations`:
  confirmed `core_config`'s own onboarding step does unconditionally
  *attempt* to create `google_translate`/`met`/`radio_browser`/
  `shopping_list` config entries (traced to
  `homeassistant/components/onboarding/views.py`
  `CoreConfigOnboardingView`, not something this repo's request body can
  opt out of) — traced further (second adversarial-review round) to find
  `met` specifically never actually creates an entry against this
  harness's own onboarding call (its `async_step_onboarding` aborts
  "no_home" against the placeholder location an empty `core_config` POST
  body leaves in place), which is why the removal loop's exit condition
  needed fixing (see below) rather than the removal logic itself being
  wrong.
- The render endpoint's B1 parity fix (`tests/test_designer_render.py`):
  on a real measured-palette panel (IC `0x21` + BWR), the endpoint's actual
  output now matches `prepare_image(tone="auto", use_measured_palettes=
  False)` and differs from what `prepare_image`'s own bare defaults
  (`tone=0.0, use_measured_palettes=True`) would have produced — the parity
  gap is pixel-observable on a real measured IC, not just a kwarg-inspection
  nicety. Verified via the test suite (`scripts/test`).

**Verified live** (second adversarial-review round, same machine, later
session — this round's own fixes):

- Both wedged-onboarding shapes, fabricated directly on disk (no live HA
  needed — `check_not_wedged` inspects storage files only): (1)
  `.storage/onboarding` recording the "user" step done with `.storage/auth`
  present but empty, and (2) `.storage/auth_provider.homeassistant` present
  with neither `.storage/auth` nor `.storage/onboarding` on disk at all —
  both now caught with `exit 1` and the `rm -rf`-and-retry message, and a
  genuinely pristine (no storage files at all) state confirmed to fall
  through and proceed normally, unflagged.
- The crash-signature detection no longer keys on "a `.log.fault` file
  exists" (confirmed present-but-empty after an ordinary healthy `kill`,
  i.e. the previous version's own check always fired) — see "Live-tested
  detector paths" below for the three cases actually run.
- The redefined success checks (config entries reach `loaded`, panel
  static view answers 200, render endpoint answers 200 once entries
  exist): exercised against this session's own boot attempts — see below
  for which of the three paths this session's harness stability allowed
  reaching.

### Live-tested detector paths (second adversarial-review round)

All three cases the review asked to test directly were reached and
confirmed in this session:

1. **`kill -9` (should print a generic message, no TCC text)** —
   VERIFIED. Killed the real `hass` child process (not the tracked `uv`
   wrapper PID -- see the process-tree caveat below) right after "run: HA
   is up." printed, before onboarding could finish. Output: "No
   CoreBluetooth/TCC/bluetooth mention in the log or crash dump -- this
   does not look like the macOS/TCC crash..." plus the log tail; the fault
   dump tail correctly reported "(empty or missing)" -- `kill -9` doesn't
   trigger Python's own faulthandler dump the way a real `SIGABRT` does.
   Exit 1, no false remediation text.
2. **Healthy boot (should print no false crash detection at all)** —
   VERIFIED, twice: once with zero config entries (checks (a)/(c) vacuously
   skipped, (b) passed) and once after injecting the 3 fabricated displays
   (all three checks ran for real: entries reached `loaded`, panel static
   200, and `run: render endpoint check passed (device
   41b61eb49b158df2c882b4885c87c2e4 -> 200)` for a real fabricated device).
   Both printed the full success banner, no false crash text.
3. **A real TCC abort (should print the TCC-specific remediation)** — VERIFIED.
   One occurred naturally during this session's own testing (not
   simulated): the fault dump's C stack trace named
   `__TCC_CRASHING_DUE_TO_PRIVACY_VIOLATION__` and
   `CoreBluetooth._CoreBluetooth` among its loaded extension modules;
   `dev/run.sh` correctly detected the non-empty fault file's content,
   printed "This matches the macOS/TCC CoreBluetooth crash..." and the
   Bluetooth-permission remediation text, and exited 1.

**Also verified live, the exact scenario the new B3 blocker was filed
against**: with `async_setup()` in `__init__.py` made to unconditionally
raise (a deliberate, reverted-after test edit simulating "Setup failed for
custom integration 'opendisplay'"), HA itself came up fine and reachable,
but the new panel-static-view check (b) correctly caught the failure --
"ERROR: the designer panel's static asset view did not answer 200 ... This
is what 'HA is up but the opendisplay integration itself failed to set up'
looks like from here" -- exit 1, no banner, log tail showing the real
`homeassistant.setup` traceback. This is the exact false-positive the
review reported (twice) against the previous version of this script.

**A related, smaller issue found while live-testing** (not one of this
round's filed findings, noted for completeness): `dev/stop.sh`'s SIGKILL
escalation path (after a 20s SIGTERM grace period) kills only the tracked
PID, which is `uv`'s own wrapper process, not the real `hass` child it
launches -- `uv run` does not exec-replace itself on this machine. A
`SIGTERM` reaches the child fine (observed working correctly throughout
this entire session's testing), but `SIGKILL` to the parent cannot be
forwarded (the kernel delivers it directly, with nothing left alive to
forward anything) and can leave the real `hass` process orphaned, still
bound to the port. Only reachable via `stop.sh`'s rare escalation path (a
process that ignored `SIGTERM` for 20+ seconds); not fixed as part of this
round, flagged for a follow-up (find the actual child via the tracked PID
and signal that instead, or track the child's own PID rather than the
`uv` wrapper's from the start).

### Live-tested detector paths (third adversarial-review round)

**`HA_PORT` itself was removed entirely in a later round (tier-1 round 2,
finding 3)** — it never actually worked past onboarding (HA's own "HTTP
YAML configuration is ignored after migration" Repair silently ignores the
`!include` this relied on once onboarding stores its own network config).
Kept below as the historical record of what was actually run at the time
this section was written; do not follow it as current instructions — the
port is always 8123 now, with no override.

A deeper re-test found the second round's own detectors still imprecise
(see the crash-detector paragraph above for the false-positive this
round removed). All testing below ran on a scratch git worktree with its
own `dev/ha-config` and `HA_PORT` set to a non-default port, specifically
to avoid ever touching an already-running HA on the maintainer's own port
8123 — confirmed to be the same PID, untouched, before and after this
round's entire testing session.

1. **Plain `kill -9` mid-boot (should print a generic message, no TCC
   text)** — VERIFIED. Killed the real `hass` child right after "run: HA
   is up." printed. Output: the generic "No macOS crash-dump evidence of
   the TCC/CoreBluetooth abort ..." message, correctly landing inside the
   config-entries fetch step (not silently skipped — see finding 4 below);
   fault dump correctly reported empty/missing.
2. **`HA_PORT` actually binds the port it claims (previously inert)** —
   VERIFIED. `HA_PORT=8199 dev/run.sh` bound real port 8199 (`lsof`
   confirmed a distinct PID from the maintainer's own 8123 process, which
   stayed listening throughout, unaffected). A second `dev/run.sh`
   invocation with the same `HA_PORT=8199` while the first was still up
   correctly hit the preflight port-in-use check and refused to start
   (`ERROR: port 8199 is already in use by something else.`).
   **Noted while testing (browser-only, not a shell-level finding)**: the
   first login after a `server_port` change shows HA's own "Confirm new
   HTTP server configuration" dialog (a core safety feature — the setting
   auto-reverts to 8123 within 5 minutes unless confirmed) — click
   **Confirm** once per fresh `dev/ha-config`, or a non-default `HA_PORT`
   silently reverts to 8123 five minutes into the session.
3. **Death mid the panel-registration poll (should report a death, not an
   "integration failed to set up" misdiagnosis)** — VERIFIED. This window
   is normally sub-second (the panel usually registers near-instantly), so
   a temporary, test-only `sleep` was added to widen it in a scratch copy
   of `dev/run.sh` never committed anywhere (verifies the real, unmodified
   poll loop's own logic — the delay only widens the timing window, it
   does not change which branch runs). Killing the process during the
   widened window produced: "ERROR: the hass process died (or stopped
   answering) while polling for the designer panel to register." — the log
   naturally contained an unrelated `google_translate`/`tts`
   `ModuleNotFoundError: No module named 'mutagen'` traceback at the time,
   confirming the fix doesn't accidentally key on log content either.
4. **The post-poll soft-skip window (every skip must report a death first,
   or be an explicit, non-silent reason)** — VERIFIED. The `kill -9` in
   case 1 above landed during the config-entries fetch and was correctly
   reported as a death ("... while fetching config entries for the success
   checks"), not a silent skip. A separate healthy boot with zero config
   entries printed the new explicit message: "run: no opendisplay config
   entries exist yet ... vacuously skipped (legitimate, not an error)" —
   previously this case printed nothing at all.
5. **Healthy boot with real injected displays (full banner, all three
   success checks, correct token wording)** — VERIFIED. After injecting
   the 3 fabricated displays and rebooting on `HA_PORT=8199`: "run: render
   endpoint check passed (device ... -> 200)" printed, followed by the full
   success banner, whose "Or curl the render endpoint directly with a
   token from your own Profile page -> Security tab -> Long-Lived Access
   Tokens" line reflects the corrected navigation wording (finding 6).

### Live-tested detector paths (TIER 1 findings round)

Following the maintainer's own TIER 1 manual test run (see PR-BODY.md's
"Maintainer's own TIER 1 run" for the full findings), the same scratch-
worktree/non-8123-port discipline as the third round above:

1. **Zero-entry boot with no bare `opendisplay:` key** — VERIFIED. Panel
   and `opendisplay.*` services both absent (panel static view: 404), no
   "does not support YAML setup" line anywhere in the log, full success
   banner still printed with the new explicit "no opendisplay config
   entries exist yet ... vacuously skipped" message (unchanged from round
   3's finding 4 wording, still correct here).
2. **Inject + restart with the key still absent** — VERIFIED. Panel
   registered (`custom_components.opendisplay.designer: OpenDisplay
   designer panel registered` in the log), all 3 entries reached `loaded`,
   render-endpoint check passed. **A latent race caught in the same
   pass**: the very first attempt at this reported all 3 entries as
   `not_loaded` despite hass being alive and healthy — confirmed
   independently (a fresh authenticated query moments later) that they had
   in fact all reached `loaded`, just not by the instant the single-shot
   check ran. Fixed with the same bounded-poll pattern the panel check
   already used; re-verified clean afterward.
3. **`127.0.0.1` vs `localhost` at the raw HTTP level** — checked directly
   on this session's own machine: both `http://127.0.0.1:8299/auth/
   login_flow` and `http://[::1]:8299/auth/login_flow` answered
   identically (200, a valid flow). The mismatch the maintainer hit did
   not reproduce at the curl level here, consistent with it being a
   browser/frontend-session artifact rather than a raw transport failure
   — see PR-BODY.md finding 3 for the full discussion. The fix (this
   harness only ever prints/uses `127.0.0.1` now) sidesteps it regardless.
4. **Designer panel exercised in a real browser session** — logged in via
   the browser, confirmed HA's own "Confirm new HTTP server configuration"
   dialog appears after the `server_port` change (documented above),
   opened the OpenDisplay Designer panel, and reproduced the YAML editor's
   state-name autocomplete issue (PR-BODY.md finding 4) against a real
   pushed state (`sensor.backup_backup_manager_state`) — characterized,
   not fixed here (designer-repo territory).

**UNVERIFIED** (needs the maintainer's real OpenDisplay hardware, a longer
interactive session than this harness's TCC crash window reliably survives,
or both):

- The panel JS's rotation-delta fix (M1: `_rotateDeltaFor`, deriving the
  render endpoint's `rotate` field from the designer's live canvas
  orientation control vs. the target's base capability rotation) — verified
  by reading the designer's own `.d.ts` contract and `node --check`, not by
  clicking the 90°/180°/270° buttons against a live panel and comparing the
  preview, since no run this session stayed up long enough to try it.
  Similarly for the debounce max-wait (M3) and the dark-theme fix.
- The one-time real-hardware device bootstrap itself (pairing over BLE,
  `config_flow.py`'s connection probe) — the TCC crash above means this
  cannot even be attempted from this sandboxed environment; on the
  maintainer's own Mac, whether Bluetooth ever becomes usable natively
  depends on the app-bundle/entitlement constraint described above, not on
  anything in this repository.
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
- Whether a HA instance left running natively for an extended interactive
  session (rather than this session's short scripted checks) survives long
  enough for comfortable manual UI exploration, given the TCC crash's
  non-deterministic timing (observed 4s to over a minute across repeated
  runs in this session).
