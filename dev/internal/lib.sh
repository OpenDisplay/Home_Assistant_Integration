# shellcheck shell=bash
# shellcheck disable=SC2034
# This file is sourced-only, never checked as an entry point -- every
# variable it sets is used by whichever caller sources it (dev/run.sh,
# dev/ha), not within this file itself, which is exactly what SC2034 can't
# see when this file is linted standalone.
# Shared by dev/run.sh and dev/ha (the dispatcher) -- constants and the one
# bit of login logic both need, kept in exactly one place instead of
# copy-pasted (dev-UX consolidation round, maintainer ruling: "typing
# uv run --group dev ... is bad UX").
#
# Sourced, not executed: no shebang-exec bit, and deliberately no
# `set -euo pipefail` of its own -- the sourcing script's own `set` options
# govern. A library that sets its own strict mode can surprise a caller that
# deliberately catches a failure right after sourcing (e.g. dev/ha's
# `fetch_dev_access_token || true` pattern).

DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
# 127.0.0.1, not localhost (tier-1 review, an earlier round): macOS resolves
# "localhost" to ::1 (IPv6) first, and HA's own auth/http stack can
# mismatch across that split (reproduced live -- a white designer page,
# /auth/token failures logged against ::1, while 127.0.0.1 worked
# immediately). Every probe, printed URL, and doc example derives from this
# one value, so fixing it here fixes all of them at once.
HA_URL="http://127.0.0.1:8123"
# Every wait/poll curl uses this -- an HA that accepts the TCP connection
# but hangs mid-response (wedged onboarding storage, a stuck component
# setup) must not turn a bounded poll into an unbounded one (adversarial
# review round 3, finding 5).
CURL_TIMEOUT=(--connect-timeout 5 --max-time 10)

# Best-effort: log in as the known dev user via the same login_flow ->
# token exchange onboarding.sh itself uses (not a stored long-lived token --
# nothing in this harness creates one). Prints the access token on stdout
# on success; prints nothing and returns non-zero on any failure, which
# every caller treats as "skip the checks that need auth" rather than
# fatal -- a harness whose dev user has different credentials than the ones
# this script knows (hand-onboarded, or HA_DEV_USERNAME/PASSWORD changed
# after the fact) shouldn't block on a login this script was never
# guaranteed to be able to do.
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
