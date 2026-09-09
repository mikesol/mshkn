#!/usr/bin/env bash
# Hatch an embryo (spec §8): mint the brain's scoped key, build the brain recipe,
# create the brain, install the membrane, write .env and the priors, checkpoint it
# as `brain`, destroy the computer, open a closed public door. Prints one JSON line.
#
#   MSHKN_API_URL=https://api.mshkn.dev MSHKN_API_KEY=<account key> embryo/hatch.sh
#
# Optional: BRAIN_API_URL (what the brain dials; default MSHKN_API_URL),
# MEMBRANE_MODEL (anthropic|scripted; default anthropic), MEMBRANE_MODEL_ID (the
# model the brain runs; the membrane defaults to claude-opus-5), MEMBRANE_EFFORT
# (low|medium|high|xhigh|max; default the API's), ANTHROPIC_API_KEY and
# OPENAI_API_KEY (required for anthropic), ANTHROPIC_BASE_URL (optional, the
# model's base URL; in scripted mode the server's route). Needs uv, curl and jq.
set -euo pipefail

: "${MSHKN_API_URL:?}"
: "${MSHKN_API_KEY:?}"
BRAIN_API_URL="${BRAIN_API_URL:-$MSHKN_API_URL}"
MEMBRANE_MODEL="${MEMBRANE_MODEL:-anthropic}"
if [ "$MEMBRANE_MODEL" = anthropic ]; then
  : "${ANTHROPIC_API_KEY:?required when MEMBRANE_MODEL=anthropic}"
  : "${OPENAI_API_KEY:?required when MEMBRANE_MODEL=anthropic}"
fi
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

api() { # method path [json]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -fsS -X "$method" "$MSHKN_API_URL$path" -H "Authorization: Bearer $MSHKN_API_KEY" \
      -H 'Content-Type: application/json' --data "$body"
  else
    curl -fsS -X "$method" "$MSHKN_API_URL$path" -H "Authorization: Bearer $MSHKN_API_KEY"
  fi
}
upload() { # computer_id remote_path local_file
  curl -fsS -X POST "$MSHKN_API_URL/computers/$1/upload?path=$2" -H "Authorization: Bearer $MSHKN_API_KEY" \
    -H 'Content-Type: application/octet-stream' --data-binary "@$3" > /dev/null
}
run() { # computer_id command  (exec over SSE; fails unless the exit event is 0)
  local out
  out="$(curl -fsS -N -X POST "$MSHKN_API_URL/computers/$1/exec" -H "Authorization: Bearer $MSHKN_API_KEY" \
    -H 'Content-Type: application/json' --data "$(jq -cn --arg c "$2" '{command: $c, timeout_seconds: 300}')")"
  local code
  code="$(printf '%s\n' "$out" | tr -d '\r' | awk '/^event: exit/{getline; sub(/^data: /, ""); print}' | tail -1)"
  if [ "$code" != "0" ]; then
    echo "exec failed ($code): $2" >&2
    printf '%s\n' "$out" >&2
    return 1
  fi
}

echo "building the membrane wheel" >&2
(cd "$HERE/.." && uv build --package membrane --out-dir "$TMP/dist" >&2)
WHEEL="$(ls "$TMP"/dist/membrane-*.whl)"
WHEEL_NAME="$(basename "$WHEEL")"

echo "building the brain recipe" >&2
RECIPE_JSON="$(api POST /recipes "$(jq -cn --rawfile d "$HERE/Dockerfile.brain" '{dockerfile: $d}')")"
RECIPE_ID="$(jq -r .recipe_id <<<"$RECIPE_JSON")"
for _ in $(seq 1 200); do
  STATUS="$(api GET "/recipes/$RECIPE_ID" | jq -r .status)"
  [ "$STATUS" = ready ] && break
  if [ "$STATUS" = failed ]; then
    api GET "/recipes/$RECIPE_ID" | jq -r .build_log >&2
    echo "brain recipe failed" >&2
    exit 1
  fi
  sleep 3
done
[ "$STATUS" = ready ] || { echo "brain recipe not ready after 600 s" >&2; exit 1; }

ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://api.anthropic.com}"
SERVER_ID=null
if [ "$MEMBRANE_MODEL" = scripted ]; then
  echo "starting the scripted model server" >&2
  CREATED="$(api POST /computers "$(jq -cn --arg r "$RECIPE_ID" '{recipe_id: $r}')")"
  SID="$(jq -r .computer_id <<<"$CREATED")"
  upload "$SID" "/tmp/$WHEEL_NAME" "$WHEEL"
  printf 'MSHKN_API_URL=%s\nMSHKN_API_KEY=unused\nMEMBRANE_MODEL=scripted\n' "$BRAIN_API_URL" > "$TMP/server-env"
  upload "$SID" /brain/.env "$TMP/server-env"
  run "$SID" "/brain/venv/bin/pip install --no-deps -q /tmp/$WHEEL_NAME && ln -sf /brain/venv/bin/membrane /usr/local/bin/membrane"
  api POST "/computers/$SID/exec/bg" '{"command": "membrane serve --port 8000"}' > /dev/null
  # the computer's route, port-prefixed: https://8000-<id>.<domain>
  ANTHROPIC_BASE_URL="$(jq -r .url <<<"$CREATED" | sed 's#^https://#https://8000-#')"
  SERVER_ID="\"$SID\""
fi

echo "minting the brain's scoped key" >&2
SCOPES="$(jq -cn --arg t "$ANTHROPIC_BASE_URL/" '{recipes: {create: true, read: true}, computers: {create_from: "*"}, labels: ["verb/"], relay: {targets: [$t], deliver: {label: "brain", exec: "membrane resume"}}}')"
KEY_JSON="$(api POST /keys "$(jq -cn --argjson s "$SCOPES" '{scopes: $s, label: "brain"}')")"
KEY_ID="$(jq -r .id <<<"$KEY_JSON")"
BRAIN_KEY="$(jq -r .secret <<<"$KEY_JSON")"

echo "creating the brain" >&2
CID="$(api POST /computers "$(jq -cn --arg r "$RECIPE_ID" '{recipe_id: $r, needs: {ram: "512MB", cores: 2}}')" | jq -r .computer_id)"
# pip reads the version and the tags off the filename (PEP 427), so the wheel keeps its name.
upload "$CID" "/tmp/$WHEEL_NAME" "$WHEEL"
upload "$CID" /brain/seed.md "$HERE/seed.md"
upload "$CID" /brain/policy.json "$HERE/policy.json"
{
  echo "MSHKN_API_URL=$BRAIN_API_URL"
  echo "MSHKN_API_KEY=$BRAIN_KEY"
  echo "MEMBRANE_MODEL=$MEMBRANE_MODEL"
  [ -n "${MEMBRANE_MODEL_ID:-}" ] && echo "MEMBRANE_MODEL_ID=$MEMBRANE_MODEL_ID"
  [ -n "${MEMBRANE_EFFORT:-}" ] && echo "MEMBRANE_EFFORT=$MEMBRANE_EFFORT"
  [ -n "${ANTHROPIC_API_KEY:-}" ] && echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"
  [ -n "${OPENAI_API_KEY:-}" ] && echo "OPENAI_API_KEY=$OPENAI_API_KEY"
  echo "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL"
  true
} > "$TMP/env"
upload "$CID" /brain/.env "$TMP/env"
run "$CID" "/brain/venv/bin/pip install --no-deps -q /tmp/$WHEEL_NAME && ln -sf /brain/venv/bin/membrane /usr/local/bin/membrane && chmod 600 /brain/.env && membrane root list > /dev/null"

echo "checkpointing as brain" >&2
CKPT_ID="$(api POST "/computers/$CID/checkpoint" '{"label": "brain"}' | jq -r .checkpoint_id)"
api DELETE "/computers/$CID" > /dev/null

echo "opening the (closed) public door" >&2
RULE_JSON="$(api POST /ingress_rules "$(jq -cn --rawfile s "$HERE/ingress.star" '{name: "brain", starlark_source: $s, response_mode: "sync", rate_limit_rpm: 30}')")"

jq -cn --arg url "$(jq -r .ingress_url <<<"$RULE_JSON")" --arg rule "$(jq -r .id <<<"$RULE_JSON")" \
  --arg key "$KEY_ID" --arg recipe "$RECIPE_ID" --arg ckpt "$CKPT_ID" --argjson server "$SERVER_ID" \
  '{ingress_url: $url, rule_id: $rule, key_id: $key, recipe_id: $recipe, checkpoint_id: $ckpt, server_id: $server}'
