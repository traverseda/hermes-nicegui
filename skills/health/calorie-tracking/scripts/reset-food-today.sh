#!/bin/bash
# Reset daily food-tracking helpers in Home Assistant at midnight.
# Also clears the "logged today" flag so template sensors render missing (—)
# instead of 0 until the first meal is logged.
# Silent when successful (empty stdout = no delivery for no_agent cron).
# Reads HASS_URL/HASS_TOKEN from ~/.hermes/.env
set -u
ENV_FILE="$HOME/.hermes/.env"
TOKEN=$(grep '^HASS_TOKEN=' "$ENV_FILE" | cut -d= -f2)
# hearth.lan is the LAN-only HA address (the public https URL is
# Cloudflare-fronted and rejects API calls from scripts). Override with
# FOOD_RESET_BASE if the HA address ever changes.
BASE="${FOOD_RESET_BASE:-http://hearth.lan}"

if [ -z "$TOKEN" ]; then
  echo "ERROR: HASS_TOKEN not found in $ENV_FILE" >&2
  exit 1
fi

if [ "${DRY_RUN:-}" = "1" ]; then
  echo "DRY_RUN: would zero food_*_today helpers and turn off food_logged_today at $BASE"
  exit 0
fi

for ENTITY in food_calories_today food_protein_today food_carbs_today food_fat_today; do
  curl -s -m 15 -o /dev/null -w "%{http_code}" \
    -X POST "$BASE/api/services/input_number/set_value" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -H "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36" \
    -d "{\"entity_id\":\"input_number.$ENTITY\",\"value\":0}" || echo "FAIL:$ENTITY" >&2
  echo " $ENTITY"
done

# Clear the logged-today flag -> today sensors go unknown (missing)
curl -s -m 15 -o /dev/null -w "%{http_code}" \
  -X POST "$BASE/api/services/input_boolean/turn_off" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36" \
  -d '{"entity_id":"input_boolean.food_logged_today"}' || echo "FAIL:food_logged_today" >&2
echo " input_boolean.food_logged_today"
# Empty-ish output ok: cron no_agent delivers stdout; zero bytes = silent.
echo "food helpers reset"
