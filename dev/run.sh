#!/usr/bin/env bash
# One command: bring up a local Home Assistant with this branch's OpenDisplay
# integration, reachable at http://127.0.0.1:8123. Native Python via uv --
# no Docker (maintainer ruling 2026-08-30: maximum KISS, no container
# runtime dependency). See dev/README.md for the full workflow.
set -euo pipefail

DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEV_DIR/.." && pwd)"
HA_CONFIG="$DEV_DIR/ha-config"
PID_FILE="$HA_CONFIG/.harness.pid"
LOG_FILE="$HA_CONFIG/ha.log"
# Port is always 8123, full stop -- no HA_PORT override (tier-1 round 2,
# finding 3: ripped out entirely). It never actually worked past onboarding:
# once onboarding stores its own network config, HA's own "HTTP YAML
# configuration is ignored after migration" Repair fires and the
# http_port.yaml !include this harness used to generate is silently
# ignored -- the feature was broken by platform behavior, not a bug in this
# harness's own wiring of it, and there is no supported way to make a
# YAML-configured http.server_port stick post-onboarding.
#
# 127.0.0.1, not localhost (tier-1 review, finding 3 from the PRIOR round):
# macOS resolves "localhost" to ::1 (IPv6) first, and HA's own auth/http
# stack can mismatch across that split (reproduced live -- a white designer
# page, /auth/token failures logged against ::1, while 127.0.0.1 worked
# immediately). Every probe, printed URL, and doc example derives from this
# one value, so fixing it here fixes all of them at once.
HA_URL="http://127.0.0.1:8123"
# Every wait/poll curl below uses this -- an HA that accepts the TCP
# connection but hangs mid-response (wedged onboarding storage, a stuck
# component setup) must not turn a bounded poll into an unbounded one
# (adversarial review round 3, finding 5).
CURL_TIMEOUT=(--connect-timeout 5 --max-time 10)
# The render-endpoint check's own POST does real image compositing work
# (not a poll) -- same connect timeout, longer overall budget so a slow
# but genuinely-working render isn't mistaken for a hang.
RENDER_CURL_TIMEOUT=(--connect-timeout 5 --max-time 30)

command -v uv >/dev/null 2>&1 || {
  echo "ERROR: uv not found on PATH (https://docs.astral.sh/uv/)." >&2
  exit 1
}

# pyproject.toml pins requires-python >=3.14.2, and __init__.py relies on
# syntax (an unparenthesized except-tuple) that is a SyntaxError below 3.14.
# uv resolves the interpreter itself, but confirm loudly rather than let a
# stale/pre-existing uv-managed interpreter fail with a confusing traceback
# three layers into hass's own startup.
resolved_version="$(cd "$REPO_ROOT" && uv run --no-default-groups --group dev python3 -c \
  'import sys; print(".".join(map(str, sys.version_info[:3])))')"
resolved_major="${resolved_version%%.*}"
resolved_minor="$(cut -d. -f2 <<<"$resolved_version")"
if ((resolved_major != 3 || resolved_minor < 14)); then
  echo "ERROR: uv resolved Python $resolved_version, but this repo requires >=3.14.2" \
    "(pyproject.toml). Run 'uv python install 3.14' and retry." >&2
  exit 1
fi

mkdir -p "$HA_CONFIG"

# Home Assistant only looks for custom_components/ inside its own config
# directory (it adds that directory to sys.path specifically so
# `import custom_components` resolves) -- it never walks up from the repo
# checkout to find one. Symlink this branch's integration in, same effect
# as the old Docker bind-mount, kept in sync automatically since it's a
# symlink (no copy to go stale). Verified necessary: without this, HA 2026.8's
# own core-shipped `opendisplay` integration silently answers the domain
# instead (same name, no error) -- see dev/README.md.
mkdir -p "$HA_CONFIG/custom_components"
integration_link="$HA_CONFIG/custom_components/opendisplay"
if [[ -e "$integration_link" && ! -L "$integration_link" ]]; then
  echo "ERROR: $integration_link exists and is a real directory, not a" \
    "symlink -- refusing to ln -sfn over it (that either fails outright or" \
    "silently nests the link inside it, depending on the platform)." \
    "Remove it by hand first if it's stale: rm -rf $integration_link" >&2
  exit 1
fi
ln -sfn "$REPO_ROOT/custom_components/opendisplay" "$integration_link"

already_running=false
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  already_running=true
fi

if [[ "$already_running" == false ]]; then
  if [[ -f "$PID_FILE" ]]; then
    echo "run: stale PID file (process gone) -- removing." >&2
    rm -f "$PID_FILE"
  fi
  # Port-free check: only fail if something OTHER than our own (stopped)
  # instance holds it -- a bare TCP probe can't tell whose process that is,
  # so this is best-effort; hass binding the port below is the authoritative
  # check (it fails loudly and this script's wait-loop below times out).
  if (exec 3<>"/dev/tcp/127.0.0.1/8123") 2>/dev/null; then
    exec 3>&- 3<&-
    echo "ERROR: port 8123 is already in use by something else. Free it" \
      "and rerun (no HA_PORT override any more -- see this script's own" \
      "top-of-file comment for why)." >&2
    exit 1
  fi

  echo "run: starting Home Assistant (uv run --group dev hass --config dev/ha-config)..."
  (
    cd "$REPO_ROOT"
    nohup uv run --no-default-groups --group dev hass --config "$HA_CONFIG" \
      >"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
  )
else
  echo "run: already running (pid $(cat "$PID_FILE"))."
fi

# --- Shared helpers (used by the wait loop below AND the final liveness
# check after onboarding -- a process can die at either point, and both get
# the identical crash diagnosis rather than two different, inconsistent
# messages depending on exactly when it happened) ---

ha_is_actually_up() {
  [[ -f "$PID_FILE" ]] || return 1
  kill -0 "$(cat "$PID_FILE")" 2>/dev/null || return 1
  curl -sf "${CURL_TIMEOUT[@]}" -o /dev/null "$HA_URL/manifest.json" || return 1
  return 0
}

# Prints the last N lines of a file with a labeled header, or says why it
# couldn't -- the shared "show me what happened" tail every failure path
# below uses, so a failure never just says "check the log" without also
# showing some of it right here.
tail_with_header() {
  local file="$1" label="$2" lines="${3:-30}"
  echo "--- last $lines lines: $label ($file) ---" >&2
  if [[ -s "$file" ]]; then
    tail -n "$lines" "$file" >&2
  else
    echo "(empty or missing)" >&2
  fi
}

# Prints a diagnosis for "the hass process is not up" and exits 1. The
# crash signature is specific and narrow, on purpose (round 3 of
# adversarial review): a NON-EMPTY macOS Python fault dump containing
# either `__TCC_CRASHING_DUE_TO_PRIVACY_VIOLATION__` or `Fatal Python
# error` -- nothing else counts. Two earlier, broader signatures were both
# reproduced as false positives and removed:
#   - "a .fault file exists" alone -- Home Assistant creates an empty one
#     on ordinary healthy boots too, via Python's own faulthandler
#     pre-registration.
#   - grepping $LOG_FILE (the regular stdout/stderr log) for
#     "corebluetooth\|bluetooth\|tcc" -- routine, unrelated cascade
#     failures mention "bluetooth" in perfectly ordinary log lines (e.g.
#     "Setup failed for 'bluetooth': Could not setup dependencies", or the
#     "Could not find Bluetooth device with address ..." reachability
#     message _drawcustom_for_device's own diagnostics produce), so a
#     plain port-8123-bind failure (nothing to do with TCC at all) matched
#     this arm and printed the full TCC remediation. Reproduced live and
#     removed entirely -- the fault dump is the only place this decision
#     is made now.
report_process_death_and_exit() {
  local context="$1"
  echo
  echo "ERROR: $context" >&2

  local fault_file="$HA_CONFIG/home-assistant.log.fault"
  local is_tcc_crash_signature=false
  if [[ -s "$fault_file" ]] && \
     grep -qE '__TCC_CRASHING_DUE_TO_PRIVACY_VIOLATION__|Fatal Python error' "$fault_file" 2>/dev/null; then
    is_tcc_crash_signature=true
  fi

  if [[ "$is_tcc_crash_signature" == true ]]; then
    cat <<'EOF' >&2

This matches the macOS/TCC CoreBluetooth crash (dev/README.md's "macOS /
CoreBluetooth caveat"): opendisplay's bluetooth_adapters manifest
dependency touches real CoreBluetooth, and macOS's TCC privacy framework
can hard-abort a process without an app-bundle Bluetooth entitlement --
this is a SIGABRT, not a Python exception, and nothing in this repo can
catch it. Timing is nondeterministic; it does not reliably happen on any
particular attempt.

Remediation to try: System Settings -> Privacy & Security -> Bluetooth ->
grant access to the terminal application running `uv` (Terminal.app,
iTerm2, etc.), then retry dev/run.sh. UNVERIFIED whether this actually
prevents the abort: TCC's permission model expects a signed .app bundle
declaring NSBluetoothAlwaysUsageDescription, which a uv-managed venv does
not have, so macOS may have nothing grantable to offer for a bare python3
process in the first place. Success without granting it is also possible
(this is nondeterministic, not "always fails") -- just not reliable.
EOF
  else
    echo "No macOS crash-dump evidence of the TCC/CoreBluetooth abort" \
      "dev/README.md documents (a plain kill, a port conflict, an" \
      "unrelated crash, or something else). See the log below for what" \
      "actually happened." >&2
  fi

  tail_with_header "$LOG_FILE" "hass stdout/stderr"
  tail_with_header "$fault_file" "macOS Python fault dump"
  exit 1
}

# Best-effort: log in as the known dev user via the same login_flow ->
# token exchange onboarding.sh itself uses (not a stored long-lived
# token -- nothing in this harness creates one). Prints the access token
# on stdout on success; prints nothing and returns non-zero on any
# failure, which every caller below treats as "skip the checks that need
# auth" rather than fatal -- a harness whose dev user has different
# credentials than the ones this script knows (hand-onboarded, or
# HA_DEV_USERNAME/PASSWORD changed after the fact) shouldn't block on a
# login this script was never guaranteed to be able to do.
fetch_dev_access_token() {
  local client_id="${HA_URL%/}/"
  local flow flow_id step code token_resp token
  flow="$(curl -sf "${CURL_TIMEOUT[@]}" -X POST "$HA_URL/auth/login_flow" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n --arg cid "$client_id" \
      '{client_id: $cid, handler: ["homeassistant", null], redirect_uri: $cid}')" \
    2>/dev/null)" || return 1
  flow_id="$(jq -r '.flow_id // empty' <<<"$flow" 2>/dev/null)"
  [[ -n "$flow_id" ]] || return 1
  step="$(curl -sf "${CURL_TIMEOUT[@]}" -X POST "$HA_URL/auth/login_flow/$flow_id" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n --arg u "${HA_DEV_USERNAME:-dev}" \
      --arg p "${HA_DEV_PASSWORD:-opendisplay-dev-harness}" --arg cid "$client_id" \
      '{username: $u, password: $p, client_id: $cid}')" \
    2>/dev/null)" || return 1
  code="$(jq -r '.result // empty' <<<"$step" 2>/dev/null)"
  [[ -n "$code" ]] || return 1
  token_resp="$(curl -sf "${CURL_TIMEOUT[@]}" -X POST "$HA_URL/auth/token" \
    --data-urlencode "grant_type=authorization_code" \
    --data-urlencode "code=$code" \
    --data-urlencode "client_id=$client_id" 2>/dev/null)" || return 1
  token="$(jq -r '.access_token // empty' <<<"$token_resp" 2>/dev/null)"
  [[ -n "$token" ]] || return 1
  printf '%s' "$token"
}

echo "run: waiting for HA to answer at $HA_URL ..."
deadline=$((SECONDS + 120))
until curl -sf "${CURL_TIMEOUT[@]}" -o /dev/null "$HA_URL/manifest.json"; do
  if ((SECONDS > deadline)); then
    echo "ERROR: HA did not become reachable at $HA_URL within 120s." \
      "Check the log: $LOG_FILE" >&2
    exit 1
  fi
  if [[ -f "$PID_FILE" ]] && ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    report_process_death_and_exit \
      "the hass process exited before becoming reachable."
  fi
  sleep 2
done
echo "run: HA is up."

echo "run: attempting scripted onboarding (idempotent, non-fatal if it fails)..."
if HA_URL="$HA_URL" HA_CONFIG="$HA_CONFIG" "$DEV_DIR/internal/onboarding.sh"; then
  onboarded=true
else
  onboarded=false
  echo
  echo "WARNING: scripted onboarding failed (see error above)." \
    "HA is still up — onboard by hand in the browser (dev/README.md, ~30s)." >&2
  echo
fi

# Final liveness check (adversarial review, B3): onboarding.sh's own HTTP
# calls can succeed moments before a TCC/CoreBluetooth abort takes the
# process down -- measured not to be bounded by the wait loop above. A
# curl that happened to land in that window must never produce a false
# "HA is up" banner; check both the tracked PID and a fresh reachability
# probe again, right here, not just inside the wait loop.
if ! ha_is_actually_up; then
  report_process_death_and_exit \
    "HA is not actually up (the process died, or stopped answering, sometime after the checks above passed)."
fi

# Redefine success (adversarial review round 2's B3 new blocker; revised
# by tier-1 finding 2): reachable at /manifest.json is necessary but not
# sufficient -- HA itself can be up and fine while the *integration* failed
# to set up entirely, and the checks above would never notice. Assert the
# actual reason this harness exists, not just that a web server answers:
#   (a) any opendisplay config entries that exist reach state `loaded`;
#   (b) the designer panel's static asset view answers 200 -- but ONLY
#       required once (a) has found at least one entry. Tier-1 finding 2
#       removed configuration.yaml's bare `opendisplay:` key (it raised a
#       visible Repair in the UI), so async_setup_designer() -- which
#       registers this view -- now never runs at all until the first
#       config entry exists. A totally pristine, zero-entry boot
#       legitimately has no panel; treating that as a failure would
#       false-fire on every fresh dev/run.sh before dev/inject-displays.py
#       has ever run. (a) is therefore determined FIRST, below, and (b) is
#       only required once (a) has something to require it against;
#   (c) if (a) found loaded entries, the render endpoint answers 200 for
#       one of their devices (skipped if a dev-user token can't be
#       obtained -- see fetch_dev_access_token's own doc comment for why
#       that's OK).
# Any failure that IS required below is exactly as fatal as the liveness
# check above: loud error, the log tails, exit 1 -- the banner may only
# print once everything required has passed.

panel_check_url="$HA_URL/api/opendisplay/designer/static/panel/opendisplay-designer-panel.js"
# Bounded poll for (b), shared by both call sites below (entries known
# positive: required; entries unknown: best-effort) so the "check liveness
# on every failed attempt, not just once outside the loop" logic
# (adversarial review round 3, finding 3 -- a dead process mid-poll must
# report as a death, never as a misdiagnosed setup failure) lives in
# exactly one place. Not a single-shot check: async_setup() for
# opendisplay (which is what registers this view) can still be genuinely
# in flight a moment after HA itself answers -- a one-shot check here
# raced that window and failed spuriously on an otherwise-healthy boot,
# caught live in an earlier round's own testing. Sets the global
# `panel_ok`; callers decide what a false result means for them.
wait_for_panel_registration() {
  panel_ok=false
  local panel_deadline=$((SECONDS + 15))
  while true; do
    if curl -sf "${CURL_TIMEOUT[@]}" -o /dev/null "$panel_check_url"; then
      panel_ok=true
      return
    fi
    if ! ha_is_actually_up; then
      report_process_death_and_exit \
        "the hass process died (or stopped answering) while polling for the designer panel to register."
    fi
    ((SECONDS > panel_deadline)) && return
    sleep 1
  done
}

# Every "|| true"-guarded step below (adversarial review round 3, finding
# 4) resolves one of two ways, never silently: either it's a legitimate,
# explicitly-printed skip (no dev-user credentials match, a request that
# failed for its own reason while hass stayed alive), or hass actually died
# in the window and that's checked and reported FIRST, before any skip
# message is allowed to print -- a death must never be swallowed on the
# way to the success banner.
dev_token="$(fetch_dev_access_token)" || dev_token=""
# "" = couldn't determine; "0" = confirmed zero; anything else = a known
# positive count. Three different states, not just true/false -- the
# panel-check policy above depends on telling "confirmed zero" apart from
# "couldn't find out" (see the ambiguous-case message below).
entry_count=""
opendisplay_entries="[]"
if [[ -n "$dev_token" ]]; then
  entries_json="$(curl -sf "${CURL_TIMEOUT[@]}" -H "Authorization: Bearer $dev_token" \
    "$HA_URL/api/config/config_entries/entry" 2>/dev/null)" || entries_json=""
  if [[ -z "$entries_json" ]]; then
    if ! ha_is_actually_up; then
      report_process_death_and_exit \
        "the hass process died (or stopped answering) while fetching config entries for the success checks."
    fi
    echo "run: could not fetch config entries (hass is alive; the request" \
      "itself failed) -- skipping the config-entries and render-endpoint" \
      "success checks (best-effort; not fatal on its own)."
  else
    opendisplay_entries="$(jq -c '[.[] | select(.domain=="opendisplay")]' <<<"$entries_json" 2>/dev/null || echo '[]')"
    entry_count="$(jq 'length' <<<"$opendisplay_entries" 2>/dev/null || echo 0)"
  fi
else
  if ! ha_is_actually_up; then
    report_process_death_and_exit \
      "the hass process died (or stopped answering) while logging in as the dev user for the success checks."
  fi
  echo "run: could not obtain a dev-user access token (hass is alive; the" \
    "login itself failed -- a hand-onboarded harness with different" \
    "credentials, or HA_DEV_USERNAME/PASSWORD changed after the fact) --" \
    "skipping the config-entries and render-endpoint success checks" \
    "(best-effort; not fatal on its own)."
fi

if [[ "$entry_count" == "0" ]]; then
  echo "run: no opendisplay config entries exist yet (nothing from" \
    "dev/inject-displays.py, no real device onboarded) -- the designer" \
    "panel and the render-endpoint check are both vacuously skipped" \
    "(legitimate, not an error: without a config entry," \
    "async_setup_designer() never runs -- see dev/README.md's \"Why the" \
    "designer panel and opendisplay.* services are absent before the" \
    "first config entry\"). Run dev/stop.sh, then 'uv run --group dev" \
    "python dev/inject-displays.py', then dev/run.sh again to exercise it."
elif [[ -n "$entry_count" ]]; then
  # entry_count is a known positive count -- (b) and (c) are both required.
  wait_for_panel_registration
  if [[ "$panel_ok" == false ]]; then
    # Reaching here means: still alive (the poll above would have exited
    # via report_process_death_and_exit otherwise) but never answered 200
    # in 15s, despite $entry_count entries existing -- a genuine
    # "integration setup failed" case, not a misdiagnosed death and not
    # the legitimate zero-entry state (entry_count is known positive).
    echo
    echo "ERROR: the designer panel's static asset view did not answer 200" \
      "at $panel_check_url within 15s (hass itself is still alive), despite" \
      "$entry_count opendisplay config entry(ies) existing. This is what" \
      "'HA is up but the opendisplay integration itself failed to set up'" \
      "looks like from here (its static view only registers if" \
      "async_setup_designer() ran)." >&2
    tail_with_header "$LOG_FILE" "hass stdout/stderr"
    exit 1
  fi

  # Bounded poll, not a single-shot check (caught live testing tier-1
  # finding 2's fix): entry setup for several fabricated devices can still
  # be genuinely in flight a moment after the panel check above already
  # passed -- a one-shot check here raced that window and reported entries
  # as not_loaded that reached 'loaded' barely a second later on a
  # perfectly healthy boot.
  not_loaded_deadline=$((SECONDS + 15))
  while true; do
    not_loaded="$(jq -c '[.[] | select(.state != "loaded") | {title, state}]' <<<"$opendisplay_entries")"
    not_loaded_count="$(jq 'length' <<<"$not_loaded")"
    [[ "$not_loaded_count" -eq 0 ]] && break
    if ! ha_is_actually_up; then
      report_process_death_and_exit \
        "the hass process died (or stopped answering) while polling for opendisplay config entries to reach state 'loaded'."
    fi
    if ((SECONDS > not_loaded_deadline)); then
      echo
      echo "ERROR: $not_loaded_count opendisplay config entry(ies) did not" \
        "reach state 'loaded' within 15s: $not_loaded" >&2
      tail_with_header "$LOG_FILE" "hass stdout/stderr"
      exit 1
    fi
    sleep 1
    entries_json="$(curl -sf "${CURL_TIMEOUT[@]}" -H "Authorization: Bearer $dev_token" \
      "$HA_URL/api/config/config_entries/entry" 2>/dev/null)" || entries_json=""
    opendisplay_entries="$(jq -c '[.[] | select(.domain=="opendisplay")]' <<<"$entries_json" 2>/dev/null || echo '[]')"
  done

  # (c): only meaningful once (a) found at least one loaded entry -- ask
  # it for a real device_id and confirm the render endpoint itself
  # actually answers, not just that the static view exists.
  # config_entries/entry doesn't itself carry a device_id -- resolve the
  # first opendisplay device from the device registry via a template
  # render instead (the same technique this harness's own manual
  # verification used).
  device_id="$(curl -sf "${CURL_TIMEOUT[@]}" -X POST -H "Authorization: Bearer $dev_token" \
    -H 'Content-Type: application/json' \
    -d '{"template":"{{ (integration_entities(\"opendisplay\") | select(\"match\", \"^image\\\\.\") | list | first | device_id) if (integration_entities(\"opendisplay\") | select(\"match\", \"^image\\\\.\") | list | length) > 0 else \"\" }}"}' \
    "$HA_URL/api/template" 2>/dev/null)" || device_id=""
  if [[ -z "$device_id" ]]; then
    if ! ha_is_actually_up; then
      report_process_death_and_exit \
        "the hass process died (or stopped answering) while resolving a device_id for the render-endpoint check."
    fi
    echo "run: could not resolve an opendisplay device_id via" \
      "/api/template (hass is alive; the template call itself failed" \
      "or returned empty) -- skipping the render-endpoint check" \
      "(best-effort; not fatal on its own)."
  else
    render_status="$(curl -s "${RENDER_CURL_TIMEOUT[@]}" -o /dev/null -w '%{http_code}' \
      -X POST -H "Authorization: Bearer $dev_token" \
      -H 'Content-Type: application/json' \
      -d "$(jq -n --arg d "$device_id" \
        '{device_id: $d, payload: [{type: "text", value: "harness-check", x: 0, y: 0, size: 10}]}')" \
      "$HA_URL/api/opendisplay/designer/render" 2>/dev/null || echo 000)"
    if [[ "$render_status" != "200" ]]; then
      if ! ha_is_actually_up; then
        report_process_death_and_exit \
          "the hass process died (or stopped answering) while the render-endpoint check's request was in flight."
      fi
      echo
      echo "ERROR: the render endpoint returned HTTP $render_status for" \
        "device $device_id, not 200 (hass is alive)." >&2
      tail_with_header "$LOG_FILE" "hass stdout/stderr"
      exit 1
    fi
    echo "run: render endpoint check passed (device $device_id -> 200)."
  fi
else
  # entry_count is unknown (the token/entries fetch above already printed
  # why) -- attempt the panel check anyway, informationally, but its
  # result is non-fatal either way: an absent panel here is
  # indistinguishable from the legitimate zero-entry case above without a
  # working token to confirm entry_count, and guessing wrong in the fatal
  # direction would false-fire on a perfectly healthy zero-entry boot.
  wait_for_panel_registration
  if [[ "$panel_ok" == false ]]; then
    echo "run: the designer panel's static asset view did not answer 200" \
      "within 15s, but this harness could not confirm whether any" \
      "opendisplay config entries exist (see the token/entries message" \
      "above) -- treating this as indistinguishable from the legitimate" \
      "zero-entry case rather than a fatal error. Not fatal on its own."
  fi
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
  2. Stop it (fabricated entries are written straight into HA's storage,
     which must not be rewritten under a live process):
       dev/stop.sh
  3. uv run --group dev python dev/inject-displays.py — writes N fabricated
     OpenDisplay config entries (small mono / medium BWR / large BWRY, by
     default) into dev/ha-config/.storage/core.config_entries.
  4. dev/run.sh                 — bring HA back up; each fabricated entry
     sets up from its own cache with no BLE connection (sleepy-device
     fallback in __init__.py) and gets its device + entities created.

Try the designer panel (this branch's own feature, no hardware needed):
  1. Open $HA_URL, sidebar -> "OpenDisplay Designer".
  2. Pick a display (a fabricated one from step 3 above works), edit the
     YAML/canvas, toggle "Display preview" for a real server-rendered
     preview -- see docs/designer.md for what's actually happening.
  3. Or curl the render endpoint directly with a token from your own
     Profile page -> Security tab -> Long-Lived Access Tokens:
       curl -X POST -H "Authorization: Bearer \$TOKEN" \\
         -H 'Content-Type: application/json' \\
         $HA_URL/api/opendisplay/designer/render \\
         -d '{"device_id":"<id>","payload":[{"type":"text","value":"hi","x":10,"y":10}]}' \\
         -o preview.png

With real OpenDisplay hardware instead:
  1. Add the OpenDisplay integration via the UI and pair the device (needs
     a machine with real BLE reach — see dev/README.md's macOS caveat).
  2. dev/snapshot.sh   — capture that device's config/device/entity registry
     entries into dev/seed/ so you don't need hardware again.
  3. dev/restore.sh    — inject a dev/seed/ snapshot into a fresh instance.

Stop it:  dev/stop.sh
Logs:     tail -f $LOG_FILE

NOTE (macOS): opendisplay's bluetooth_adapters manifest dependency touches
real CoreBluetooth on native macOS, and this process has no app-bundle
Bluetooth entitlement to grant — macOS's TCC framework aborts it the first
time it tries, sometime between a few seconds and roughly a minute in (this
run made it past that window, going by the liveness check just above, but
a later stretch of idle time is not guaranteed to). See dev/README.md's
"macOS / CoreBluetooth caveat" for the measured details.
EOF
