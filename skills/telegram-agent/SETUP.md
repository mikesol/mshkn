# Telegram Agent Setup

Step-by-step guide to deploy a Telegram bot agent on mshkn with lampas.

## What You Need

| Item | How to get it |
|------|--------------|
| mshkn API URL | Your mshkn server (e.g., `http://135.181.6.215:8000`) |
| mshkn API key | From your mshkn account |
| Telegram bot token | Create via [@BotFather](https://t.me/BotFather) on Telegram |
| Anthropic API key | From [console.anthropic.com](https://console.anthropic.com) |
| lampas endpoint | `https://lampas.dev` (or your own deployment) |

Set these as environment variables for the commands below:

```bash
export MSHKN_API_URL="http://YOUR_SERVER:8000"
export MSHKN_PUBLIC_URL="https://api.yourdomain.dev"  # Public HTTPS URL for callbacks
export MSHKN_API_KEY="your-mshkn-api-key"
export TELEGRAM_BOT_TOKEN="your-telegram-bot-token"
export ANTHROPIC_API_KEY="your-anthropic-api-key"
export LAMPAS_URL="https://lampas.dev"
```

## Step 1: Create Ingress Rules

Create two ingress rules — one for the Telegram webhook, one for lampas callbacks.

```bash
# Create Telegram webhook ingress rule
TELEGRAM_RULE=$(curl -s -X POST "$MSHKN_API_URL/ingress_rules" \
  -H "Authorization: Bearer $MSHKN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"starlark_source": "def transform(req):\n    return None"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['rule_id'])")
echo "Telegram rule: $TELEGRAM_RULE"

# Create Claude callback ingress rule
CALLBACK_RULE=$(curl -s -X POST "$MSHKN_API_URL/ingress_rules" \
  -H "Authorization: Bearer $MSHKN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"starlark_source": "def transform(req):\n    return None"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['rule_id'])")
echo "Callback rule: $CALLBACK_RULE"
```

Save these rule IDs — you'll need them throughout.

## Step 2: Create Recipes

```bash
# Box A recipe (brain) — python3 + curl
BOXA_RECIPE=$(curl -s -X POST "$MSHKN_API_URL/recipes" \
  -H "Authorization: Bearer $MSHKN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"dockerfile": "FROM mshkn-base\nRUN apt-get update && apt-get install -y python3 curl\n"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['recipe_id'])")
echo "Box A recipe: $BOXA_RECIPE"

# Box B recipe (hands) — nodejs + npm + tools
BOXB_RECIPE=$(curl -s -X POST "$MSHKN_API_URL/recipes" \
  -H "Authorization: Bearer $MSHKN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"dockerfile": "FROM mshkn-base\nRUN apt-get update && apt-get install -y nodejs npm jq file curl\n"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['recipe_id'])")
echo "Box B recipe: $BOXB_RECIPE"
```

Wait for recipes to build (poll status until `ready`):

```bash
for RECIPE_ID in $BOXA_RECIPE $BOXB_RECIPE; do
  while true; do
    STATUS=$(curl -s "$MSHKN_API_URL/recipes/$RECIPE_ID" \
      -H "Authorization: Bearer $MSHKN_API_KEY" \
      | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
    echo "Recipe $RECIPE_ID: $STATUS"
    [ "$STATUS" = "ready" ] && break
    sleep 3
  done
done
```

## Step 3: Create and Provision Box A

```bash
# Create Box A from recipe
BOXA_ID=$(curl -s -X POST "$MSHKN_API_URL/computers" \
  -H "Authorization: Bearer $MSHKN_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"recipe_id\": \"$BOXA_RECIPE\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['computer_id'])")
echo "Box A: $BOXA_ID"
```

Upload brain.py (from this skill's `scripts/` directory):

```bash
curl -s -X POST "$MSHKN_API_URL/computers/$BOXA_ID/upload?path=/agent/brain.py" \
  -H "Authorization: Bearer $MSHKN_API_KEY" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @skills/telegram-agent/scripts/brain.py
```

Upload config.json:

```bash
cat > /tmp/agent-config.json << EOF
{
  "bot_token": "$TELEGRAM_BOT_TOKEN",
  "api_key": "$ANTHROPIC_API_KEY",
  "api_key_mshkn": "$MSHKN_API_KEY",
  "callback_rule": "$CALLBACK_RULE",
  "recipe_id": "$BOXB_RECIPE",
  "mshkn_api_url": "$MSHKN_API_URL",
  "callback_base_url": "$MSHKN_PUBLIC_URL",
  "lampas_url": "$LAMPAS_URL"
}
EOF

curl -s -X POST "$MSHKN_API_URL/computers/$BOXA_ID/upload?path=/agent/config.json" \
  -H "Authorization: Bearer $MSHKN_API_KEY" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @/tmp/agent-config.json
```

Upload initial state:

```bash
CHAT_ID=$(curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['id'])")

echo '{"messages": [], "turn": 0, "chat_id": ""}' | \
  curl -s -X POST "$MSHKN_API_URL/computers/$BOXA_ID/upload?path=/agent/state.json" \
  -H "Authorization: Bearer $MSHKN_API_KEY" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @-
```

## Step 4: Checkpoint Box A

```bash
# Checkpoint as "agent-brain"
CKPT_ID=$(curl -s -X POST "$MSHKN_API_URL/computers/$BOXA_ID/checkpoint" \
  -H "Authorization: Bearer $MSHKN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"label": "agent-brain"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['checkpoint_id'])")
echo "Checkpoint: $CKPT_ID"

# Destroy the original computer (checkpoint is the identity now)
curl -s -X DELETE "$MSHKN_API_URL/computers/$BOXA_ID" \
  -H "Authorization: Bearer $MSHKN_API_KEY"
```

## Step 5: Configure Ingress Rules

### Claude Callback Rule

This rule receives responses from lampas (Claude responses and tool results), writes them to a file, and runs brain.py:

```bash
curl -s -X PUT "$MSHKN_API_URL/ingress_rules/$CALLBACK_RULE" \
  -H "Authorization: Bearer $MSHKN_API_KEY" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c "
import json
starlark = '''
def transform(req):
    body = req.get(\"body_json\")
    if body == None:
        return None

    response_body = body.get(\"response_body\")
    if response_body != None:
        text = \"\"
        content = response_body.get(\"content\") if hasattr(response_body, \"get\") else None
        if content != None and len(content) > 0:
            item = content[0]
            text = item.get(\"text\", \"\") if hasattr(item, \"get\") else \"\"
        if not text:
            text = str(response_body)
    else:
        text = body.get(\"text\", \"\")
        if not text:
            text = str(body)

    if not text:
        return None

    delimiter = \"__MSHKN_RESP_EOF_7a3f__\"
    cmd = \"cat > /tmp/response.txt << '\" + delimiter + \"'\\\\n\" + text + \"\\\\n\" + delimiter + \"\\\\npython3 /agent/brain.py claude_response\"
    return {\"action\": \"fork\", \"label\": \"agent-brain\", \"exec\": cmd, \"self_destruct\": True, \"exclusive\": \"defer_on_conflict\"}
'''
print(json.dumps({'starlark_source': starlark}))
")"
```

### Telegram Webhook Rule

This rule receives Telegram webhook payloads and triggers brain.py with the message:

```bash
curl -s -X PUT "$MSHKN_API_URL/ingress_rules/$TELEGRAM_RULE" \
  -H "Authorization: Bearer $MSHKN_API_KEY" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c "
import json
starlark = '''
def transform(req):
    body = req.get(\"body_json\")
    if body == None:
        return None
    msg = body.get(\"message\")
    if msg == None:
        return None
    chat = msg.get(\"chat\", {})
    chat_id = chat.get(\"id\", 0)
    text = msg.get(\"text\", \"\")
    if not text:
        return None
    safe_text = text.replace(\"'\", \"'\\\"'\\\"'\")
    cmd = \"CHAT_ID='\" + str(chat_id) + \"' MSG_TEXT='\" + safe_text + \"' python3 /agent/brain.py telegram\"
    return {\"action\": \"fork\", \"label\": \"agent-brain\", \"exec\": cmd, \"self_destruct\": True, \"exclusive\": \"defer_on_conflict\"}
'''
print(json.dumps({'starlark_source': starlark}))
")"
```

## Step 6: Set Telegram Webhook

```bash
WEBHOOK_URL="$MSHKN_API_URL/ingress/$TELEGRAM_RULE"

# If your mshkn server has a public domain (e.g., api.mshkn.dev):
# WEBHOOK_URL="https://api.yourdomain.dev/ingress/$TELEGRAM_RULE"

curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -H "Content-Type: application/json" \
  -d "{\"url\": \"$WEBHOOK_URL\"}"
```

**Note**: Telegram requires HTTPS for webhooks. Your mshkn server must be behind a TLS-terminating proxy (e.g., Caddy) with a public domain.

## Step 7: Test It

Send a message to your bot on Telegram. You should see:

1. Telegram webhook triggers ingress rule
2. Box A forks from "agent-brain" checkpoint
3. brain.py runs, calls Claude via lampas
4. Claude responds with actions (tool calls + telegram messages)
5. Bot sends you a reply

## Monitoring

Check ingress logs:

```bash
# Telegram webhook logs
curl -s "$MSHKN_API_URL/ingress_rules/$TELEGRAM_RULE/logs" \
  -H "Authorization: Bearer $MSHKN_API_KEY" | python3 -m json.tool

# Claude callback logs
curl -s "$MSHKN_API_URL/ingress_rules/$CALLBACK_RULE/logs" \
  -H "Authorization: Bearer $MSHKN_API_KEY" | python3 -m json.tool
```

Check checkpoints (activity indicator):

```bash
curl -s "$MSHKN_API_URL/checkpoints?label=agent-brain" \
  -H "Authorization: Bearer $MSHKN_API_KEY" | python3 -m json.tool
```

## Teardown

```bash
# Remove Telegram webhook
curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/deleteWebhook"

# Delete checkpoints
for CKPT in $(curl -s "$MSHKN_API_URL/checkpoints" \
  -H "Authorization: Bearer $MSHKN_API_KEY" \
  | python3 -c "import json,sys; [print(c['checkpoint_id']) for c in json.load(sys.stdin) if c.get('label') in ('agent-brain', 'box-b-tools')]"); do
  curl -s -X DELETE "$MSHKN_API_URL/checkpoints/$CKPT" \
    -H "Authorization: Bearer $MSHKN_API_KEY"
done

# Delete ingress rules
curl -s -X DELETE "$MSHKN_API_URL/ingress_rules/$TELEGRAM_RULE" \
  -H "Authorization: Bearer $MSHKN_API_KEY"
curl -s -X DELETE "$MSHKN_API_URL/ingress_rules/$CALLBACK_RULE" \
  -H "Authorization: Bearer $MSHKN_API_KEY"
```
