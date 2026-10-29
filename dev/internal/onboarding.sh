#!/usr/bin/env bash
# Drive HA's own onboarding wizard through its public HTTP API
# (https://developers.home-assistant.io/docs/auth_api/, and
# homeassistant/components/onboarding/views.py) so run.sh can hand back a
# logged-in dev instance instead of the onboarding wizard. Same steps a human
# clicking through the wizard triggers ("user" -> "core_config" -> "analytics"
# -> "integration") — not a .storage/auth_provider hack. Idempotent: skips if
# the "user" step is already done, OR if /api/onboarding is already gone —
# HA's onboarding component only registers its views on a boot where
# async_is_onboarded() is still false, so on any boot *after* the one that
# completed it, GET /api/onboarding 404s. That's the normal steady state for
# a harness reused across stop/start cycles, not a failure -- UNLESS the
# process that finished onboarding never got as far as writing a real user
# (see "wedged" check below), which a TCC/CoreBluetooth abort mid-onboarding
# can leave behind (dev/README.md's macOS caveat) -- so a bare 404 alone is
# not treated as proof onboarding actually succeeded.
#
# Any other HTTP failure aborts loudly (set -e + curl -f); run.sh treats that
# as non-fatal (HA is still reachable, the maintainer can onboard by hand)
# but prints the failure prominently.
set -euo pipefail

HA_URL="${HA_URL:?onboarding.sh: HA_URL must be set}"
HA_DEV_USERNAME="${HA_DEV_USERNAME:-dev}"
HA_DEV_PASSWORD="${HA_DEV_PASSWORD:-opendisplay-dev-harness}"
HA_DEV_NAME="${HA_DEV_NAME:-Dev}"
# Optional: lets this script tell a genuinely-onboarded instance apart from
# a wedged one (see check_not_wedged below). Without it, a 404 is trusted at
# face value, same as before this check existed.
HA_CONFIG="${HA_CONFIG:-}"
client_id="${HA_URL%/}/"
# Same rationale as dev/run.sh's identical array (adversarial review round
# 3, finding 5): an HA that accepts the connection but hangs mid-response
# must not turn this script's own single-shot sequence into an unbounded
# one -- run.sh's own wait loop already confirmed HA is reachable before
# invoking this script, so a hang here is exactly the "accepting but
# wedged" case that finding was about, not a normal slow boot.
CURL_TIMEOUT=(--connect-timeout 5 --max-time 10)

command -v curl >/dev/null 2>&1 || {
  echo "ERROR: curl is required." >&2
  exit 1
}
command -v jq >/dev/null 2>&1 || {
  echo "ERROR: jq is required (brew install jq)." >&2
  exit 1
}

# Fetches body + HTTP status in one request (body\nSTATUS on stdout);
# deliberately not `curl -f`, since a 404 is a meaningful, expected outcome
# (see note above), not a transport failure. Split in the caller, not inside
# a function invoked via $(...) — a function can't leak a second variable out
# of the subshell command substitution creates.
fetch_onboarding_status() {
  curl -s "${CURL_TIMEOUT[@]}" -w '\n%{http_code}' "$HA_URL/api/onboarding"
}

user_step_done() {
  jq -e '.[] | select(.step=="user") | .done == true' <<<"$1" >/dev/null 2>&1
}

all_steps_done() {
  jq -e 'all(.[]; .done == true)' <<<"$1" >/dev/null 2>&1
}

# Checks storage directly for either of TWO wedged shapes this script has
# actually reproduced -- run UNCONDITIONALLY, before looking at what
# GET /api/onboarding itself claims, since the second shape below can occur
# with that endpoint reporting either 200 or 404 depending on exactly how
# far the killed process got:
#
#   1. Onboarding progressed far enough to write its own storage
#      (.storage/onboarding says at least the "user" step is done -- which
#      GET /api/onboarding surfaces as either a 404, once ALL steps are
#      done and its views unregister, or a 200 with the "user" step's own
#      `done: true`), but .storage/auth has no user account at all. A
#      process killed between "mark the user step done" and "finish
#      writing the user" (exactly what a TCC/CoreBluetooth abort can do
#      mid-onboarding, timing not otherwise bounded -- see dev/README.md)
#      leaves precisely this behind.
#   2. A process killed even earlier: .storage/auth_provider.homeassistant
#      exists (the auth component writes this on the very first boot,
#      independent of onboarding) but neither .storage/auth nor
#      .storage/onboarding exists yet -- onboarding never got far enough to
#      write anything of its own, but the auth store already isn't
#      pristine either.
#
# Both leave a state where letting onboarding "proceed normally" (create a
# fresh user, or declare success because a step object says done) is a
# guess, not a known-good path. Detected only when HA_CONFIG is known;
# otherwise this is unverifiable from here and whatever
# GET /api/onboarding reports is trusted at face value, as it was before
# this check existed.
check_not_wedged() {
  [[ -n "$HA_CONFIG" ]] || return 0
  local storage_dir="$HA_CONFIG/.storage"
  local auth_file="$storage_dir/auth"
  local auth_provider_file="$storage_dir/auth_provider.homeassistant"
  local onboarding_file="$storage_dir/onboarding"

  if [[ -f "$auth_file" ]] && jq -e '.data.users | length > 0' "$auth_file" >/dev/null 2>&1; then
    return 0
  fi

  if [[ -f "$onboarding_file" ]]; then
    echo "ERROR: onboarding's own storage ($onboarding_file) records at" \
      "least one step done, but $auth_file has no user account -- this is" \
      "a wedged half-onboarded state a killed process (e.g. a" \
      "TCC/CoreBluetooth abort mid-onboarding -- dev/README.md's macOS" \
      "caveat) can leave behind." >&2
    echo "Recover: rm -rf $storage_dir $HA_CONFIG/*.db* && dev/run.sh" >&2
    exit 1
  fi

  if [[ -f "$auth_provider_file" ]]; then
    echo "ERROR: $auth_provider_file exists but neither $auth_file nor" \
      "$onboarding_file does -- a process was killed even before" \
      "onboarding wrote any state of its own (e.g. mid the very first" \
      "boot's user-creation step). No user account exists to log in as." >&2
    echo "Recover: rm -rf $storage_dir $HA_CONFIG/*.db* && dev/run.sh" >&2
    exit 1
  fi

  # Neither file exists at all -- genuinely pristine (first-ever boot),
  # not wedged; let the normal onboarding flow below proceed.
}

# Onboarding's own core_config step (HA core, not this repo -- see
# homeassistant/components/onboarding/views.py CoreConfigOnboardingView)
# unconditionally *attempts* to set up google_translate/met/radio_browser/
# shopping_list as part of finishing onboarding, the same as a human
# clicking through the wizard would trigger -- not something this script's
# request body can opt out of. met and radio_browser make outbound network
# calls once set up, which sits at odds with this harness's own
# no-network-probing rationale for dropping default_config (dev/README.md).
# Removed right after onboarding finishes, on a short bounded poll -- the
# four are created via async background tasks (hass.async_create_task), so
# they don't necessarily exist yet the instant the core_config POST
# returns.
#
# `met` specifically never actually creates an entry here: its own
# `async_step_onboarding` (homeassistant/components/met/config_flow.py)
# aborts with "no_home" when the configured location is still onboarding's
# own placeholder default -- exactly what our `core_config` POST above
# (`-d '{}'`, no real location) leaves in place. Not hardcoded as
# domain-specific knowledge below (fragile, and other domains could behave
# the same way for their own reasons) -- instead the poll exits once a full
# pass finds nothing new to remove, rather than insisting on removing every
# domain in the list before it will stop.
remove_onboarding_added_integrations() {
  local access_token="$1"
  local unwanted=(google_translate met radio_browser shopping_list)
  local removed=()
  local attempt entries entry_id domain found_this_round already_removed
  for attempt in 1 2 3 4 5; do
    entries="$(curl -sf "${CURL_TIMEOUT[@]}" -H "Authorization: Bearer $access_token" \
      "$HA_URL/api/config/config_entries/entry")"
    found_this_round=0
    for domain in "${unwanted[@]}"; do
      already_removed=0
      for done_domain in "${removed[@]:-}"; do
        [[ "$done_domain" == "$domain" ]] && already_removed=1 && break
      done
      [[ "$already_removed" -eq 1 ]] && continue
      entry_id="$(jq -r --arg d "$domain" \
        '.[] | select(.domain==$d) | .entry_id' <<<"$entries" | head -1)"
      if [[ -n "$entry_id" && "$entry_id" != "null" ]]; then
        curl -sf "${CURL_TIMEOUT[@]}" -X DELETE -H "Authorization: Bearer $access_token" \
          "$HA_URL/api/config/config_entries/entry/$entry_id" >/dev/null || true
        removed+=("$domain")
        found_this_round=1
      fi
    done
    if [[ "${#removed[@]}" -ge "${#unwanted[@]}" ]]; then
      break
    fi
    # Stop once a pass finds nothing new AND at least one retry already
    # happened -- covers a domain (like `met`, above) that legitimately
    # never creates an entry at all, instead of burning every attempt
    # regardless of whether anything is still pending.
    if [[ "$found_this_round" -eq 0 && "$attempt" -ge 2 ]]; then
      break
    fi
    sleep 1
  done
  if [[ "${#removed[@]}" -gt 0 ]]; then
    echo "onboarding: removed auto-added integration(s) onboarding's own" \
      "core_config step creates unconditionally: ${removed[*]}"
  fi
}

# Run before branching on what /api/onboarding itself claims -- both wedge
# shapes it guards against can present under either a 404 or a 200 with
# the "user" step marked done, so it cannot be scoped to just one branch
# below (a real gap in an earlier version of this script: both wedge
# shapes reproduced verbatim, unnoticed, when this check ran only inside
# the 404 branch).
check_not_wedged

raw="$(fetch_onboarding_status)"
http_code="${raw##*$'\n'}"
status="${raw%$'\n'*}"
if [[ "$http_code" == "404" ]]; then
  echo "onboarding: /api/onboarding is gone (404) — HA already finished onboarding" \
    "in a prior boot (its views only register pre-onboarding); skipping."
  exit 0
fi
[[ "$http_code" == "200" ]] || {
  echo "ERROR: GET /api/onboarding returned HTTP $http_code: $status" >&2
  exit 1
}
if user_step_done "$status"; then
  echo "onboarding: already done (a user account exists) — skipping."
  exit 0
fi

echo "onboarding: creating dev user '$HA_DEV_USERNAME'..."
users_resp="$(curl -sf "${CURL_TIMEOUT[@]}" -X POST "$HA_URL/api/onboarding/users" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg name "$HA_DEV_NAME" --arg username "$HA_DEV_USERNAME" \
    --arg password "$HA_DEV_PASSWORD" --arg client_id "$client_id" \
    '{name: $name, username: $username, password: $password, client_id: $client_id, language: "en"}')")"
auth_code="$(jq -r '.auth_code' <<<"$users_resp")"
[[ -n "$auth_code" && "$auth_code" != "null" ]] || {
  echo "ERROR: onboarding/users did not return an auth_code: $users_resp" >&2
  exit 1
}

echo "onboarding: exchanging auth code for an access token..."
token_resp="$(curl -sf "${CURL_TIMEOUT[@]}" -X POST "$HA_URL/auth/token" \
  --data-urlencode "grant_type=authorization_code" \
  --data-urlencode "code=$auth_code" \
  --data-urlencode "client_id=$client_id")"
access_token="$(jq -r '.access_token' <<<"$token_resp")"
[[ -n "$access_token" && "$access_token" != "null" ]] || {
  echo "ERROR: /auth/token did not return an access_token: $token_resp" >&2
  exit 1
}

echo "onboarding: finishing core_config / analytics / integration steps..."
curl -sf "${CURL_TIMEOUT[@]}" -X POST "$HA_URL/api/onboarding/core_config" \
  -H "Authorization: Bearer $access_token" -H 'Content-Type: application/json' -d '{}' >/dev/null
curl -sf "${CURL_TIMEOUT[@]}" -X POST "$HA_URL/api/onboarding/analytics" \
  -H "Authorization: Bearer $access_token" -H 'Content-Type: application/json' -d '{}' >/dev/null
curl -sf "${CURL_TIMEOUT[@]}" -X POST "$HA_URL/api/onboarding/integration" \
  -H "Authorization: Bearer $access_token" -H 'Content-Type: application/json' \
  -d "$(jq -n --arg cid "$client_id" '{client_id: $cid, redirect_uri: $cid}')" >/dev/null

raw="$(fetch_onboarding_status)"
status="${raw%$'\n'*}"
all_steps_done "$status" || {
  echo "ERROR: onboarding steps still incomplete after driving the API: $status" >&2
  exit 1
}

remove_onboarding_added_integrations "$access_token"

cat <<EOF
onboarding: done. Dev login —
  URL:      $HA_URL
  username: $HA_DEV_USERNAME
  password: $HA_DEV_PASSWORD
(override with HA_DEV_USERNAME / HA_DEV_PASSWORD / HA_DEV_NAME before run.sh)
EOF
