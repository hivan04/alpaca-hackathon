#!/usr/bin/env bash
# Confirm the dev and judged paper accounts are DIFFERENT and both reachable.
# Run this before pointing the agent at the judged account.
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a

check() {
  local label="$1" key="$2" secret="$3"
  if [ -z "$key" ] || [ -z "$secret" ]; then
    printf "  %-8s \033[33mnot configured\033[0m\n" "$label"
    return
  fi
  local body
  body=$(curl -sS https://paper-api.alpaca.markets/v2/account \
    -H "APCA-API-KEY-ID: $key" -H "APCA-API-SECRET-KEY: $secret") || {
      printf "  %-8s \033[31munreachable\033[0m\n" "$label"; return; }
  local id equity
  id=$(printf '%s' "$body" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("account_number","?"))' 2>/dev/null || echo "?")
  equity=$(printf '%s' "$body" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("equity","?"))' 2>/dev/null || echo "?")
  printf "  %-8s account %s  equity %s\n" "$label" "$id" "$equity"
  echo "$id"
}

echo "Alpaca paper accounts"
JUDGED_ID=$(check "judged" "${ALPACA_API_KEY:-}" "${ALPACA_SECRET_KEY:-}" | tail -1)
DEV_ID=$(check "dev" "${ALPACA_DEV_API_KEY:-}" "${ALPACA_DEV_SECRET_KEY:-}" | tail -1)

echo
if [ -n "${DEV_ID:-}" ] && [ "$JUDGED_ID" = "$DEV_ID" ]; then
  printf "\033[31mWARNING: dev and judged keys point at the SAME account.\033[0m\n"
  printf "Create a second paper account so debugging never pollutes the judged record.\n"
  exit 1
fi
if [ -n "${ALPACA_JUDGED_ACCOUNT_ID:-}" ] && [ "$ALPACA_JUDGED_ACCOUNT_ID" != "$JUDGED_ID" ]; then
  printf "\033[33mNote: ALPACA_JUDGED_ACCOUNT_ID (%s) != the account the judged keys reach (%s).\033[0m\n" \
    "$ALPACA_JUDGED_ACCOUNT_ID" "$JUDGED_ID"
fi
printf "\033[32mAccounts are distinct. Submit account ID: %s\033[0m\n" "$JUDGED_ID"
