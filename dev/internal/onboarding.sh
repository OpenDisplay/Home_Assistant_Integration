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
# a harness reused across `docker compose down`/`up` cycles, not a failure.
# Any other HTTP failure aborts loudly (set -e + curl -f); run.sh treats that
# as non-fatal (HA is still reachable, the maintainer can onboard by hand)
# but prints the failure prominently.
set -euo pipefail

HA_URL="${HA_URL:?onboarding.sh: HA_URL must be set}"
HA_DEV_USERNAME="${HA_DEV_USERNAME:-dev}"
HA_DEV_PASSWORD="${HA_DEV_PASSWORD:-opendisplay-dev-harness}"
HA_DEV_NAME="${HA_DEV_NAME:-Dev}"
client_id="${HA_URL%/}/"

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
  curl -s -w '\n%{http_code}' "$HA_URL/api/onboarding"
}

user_step_done() {
  jq -e '.[] | select(.step=="user") | .done == true' <<<"$1" >/dev/null 2>&1
}

all_steps_done() {
  jq -e 'all(.[]; .done == true)' <<<"$1" >/dev/null 2>&1
}

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
users_resp="$(curl -sf -X POST "$HA_URL/api/onboarding/users" \
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
token_resp="$(curl -sf -X POST "$HA_URL/auth/token" \
  --data-urlencode "grant_type=authorization_code" \
  --data-urlencode "code=$auth_code" \
  --data-urlencode "client_id=$client_id")"
access_token="$(jq -r '.access_token' <<<"$token_resp")"
[[ -n "$access_token" && "$access_token" != "null" ]] || {
  echo "ERROR: /auth/token did not return an access_token: $token_resp" >&2
  exit 1
}

echo "onboarding: finishing core_config / analytics / integration steps..."
curl -sf -X POST "$HA_URL/api/onboarding/core_config" \
  -H "Authorization: Bearer $access_token" -H 'Content-Type: application/json' -d '{}' >/dev/null
curl -sf -X POST "$HA_URL/api/onboarding/analytics" \
  -H "Authorization: Bearer $access_token" -H 'Content-Type: application/json' -d '{}' >/dev/null
curl -sf -X POST "$HA_URL/api/onboarding/integration" \
  -H "Authorization: Bearer $access_token" -H 'Content-Type: application/json' \
  -d "$(jq -n --arg cid "$client_id" '{client_id: $cid, redirect_uri: $cid}')" >/dev/null

raw="$(fetch_onboarding_status)"
status="${raw%$'\n'*}"
all_steps_done "$status" || {
  echo "ERROR: onboarding steps still incomplete after driving the API: $status" >&2
  exit 1
}

cat <<EOF
onboarding: done. Dev login —
  URL:      $HA_URL
  username: $HA_DEV_USERNAME
  password: $HA_DEV_PASSWORD
(override with HA_DEV_USERNAME / HA_DEV_PASSWORD / HA_DEV_NAME before run.sh)
EOF
