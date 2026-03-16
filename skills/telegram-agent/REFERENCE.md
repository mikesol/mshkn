# Telegram Agent Reference

## Architecture Details

### The Pistachio Compute Model

Every computer is a pistachio shell — one bite, one shell. No computer is ever "always on". The checkpoint chain is the stable identity; computers are ephemeral instances that process a single input and die.

```
Input arrives → Fork from checkpoint → Process → Checkpoint → Die
```

This applies to both Box A (brain) and Box B (hands).

### Concurrency: defer_on_conflict

When multiple inputs arrive simultaneously (e.g., a Telegram message while Claude is responding), the `exclusive: "defer_on_conflict"` flag queues the second request. It runs on the next fork after the current computer checkpoints and dies.

This prevents race conditions on the conversation state — only one Box A processes at a time, and each one sees the latest state.

### Box B Checkpoint Forking

Box B (tool execution) uses labeled checkpoints (`box-b-tools`) to preserve state across tool batches. When Claude responds with tool commands:

1. brain.py checks for an existing `box-b-tools` checkpoint
2. If found, forks from it (preserving npm packages, files, etc.)
3. If not, creates a fresh VM from the Box B recipe
4. After tools complete, results are callbacked to Box A via the ingress rule

## brain.py Configuration

### config.json

```json
{
  "bot_token": "telegram-bot-token",
  "api_key": "anthropic-api-key",
  "api_key_mshkn": "mshkn-api-key",
  "callback_rule": "ingress-rule-id-for-callbacks",
  "recipe_id": "recipe-id-for-box-b",
  "mshkn_api_url": "http://your-server:8000",
  "callback_base_url": "https://api.yourdomain.dev",
  "lampas_url": "https://lampas.dev"
}
```

### state.json

```json
{
  "messages": [],
  "turn": 0,
  "chat_id": "telegram-chat-id",
  "tools_emitted": 0,
  "tools_received": 0
}
```

The `messages` array follows the Anthropic API format (`role: "user" | "assistant"`, `content: string`).

### System Prompt

The system prompt tells Claude to respond with a JSON array of actions:

```json
[
  {"type": "telegram", "text": "message to send to user"},
  {"type": "tool", "id": "t1", "command": "bash command to run on Box B"}
]
```

Claude must respond with ONLY the raw JSON array, no markdown fences. The system prompt also instructs Claude to:

- Keep tool commands under 6000 characters
- Split large files across multiple tool calls
- Use heredoc syntax for writing files

### Customizing the System Prompt

Edit the `system` string in `call_claude()` within brain.py. The system prompt defines:

- What tools are available on Box B
- How to deploy (publish.sh for here.now, or your own deployment method)
- Memory/resource constraints
- Output format (JSON action array)

## lampas Integration

[lampas](https://lampas.dev) is an async HTTP proxy. brain.py sends a request to `POST /forward` with:

```json
{
  "target": "https://api.anthropic.com/v1/messages",
  "method": "POST",
  "forward_headers": {
    "x-api-key": "...",
    "anthropic-version": "2023-06-01",
    "content-type": "application/json"
  },
  "callbacks": [{"url": "https://your-mshkn-domain/ingress/CALLBACK_RULE_ID"}],
  "body": { "model": "claude-sonnet-4-6", "max_tokens": 16384, "stream": true, ... },
  "timeout_ms": 180000
}
```

lampas forwards the request to the Anthropic API, waits for the response (with streaming to avoid Cloudflare timeouts), reconstructs the SSE stream into a final JSON response, and POSTs the result to the callback URL.

**Why streaming?** The Anthropic API sits behind Cloudflare, which kills idle connections after ~100 seconds. Streaming keeps data flowing.

**Why lampas?** brain.py runs on an ephemeral VM that dies after calling `curl`. There's no process alive to receive the response. lampas holds the connection open and delivers the response asynchronously via callback.

## mshkn API Reference

### Key Endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /computers` | Create a new computer from a recipe |
| `POST /computers/{id}/upload` | Upload a file to a computer |
| `POST /computers/{id}/checkpoint` | Snapshot computer state |
| `DELETE /computers/{id}` | Destroy a computer |
| `POST /checkpoints/{id}/fork` | Fork a new computer from a checkpoint |
| `POST /ingress_rules` | Create an ingress rule |
| `PUT /ingress_rules/{id}` | Update ingress rule starlark |
| `GET /ingress_rules/{id}/logs` | View ingress logs |
| `POST /recipes` | Create a recipe from a Dockerfile |

### Ingress Starlark API

The `transform(req)` function receives a dict with:

- `body_json`: Parsed JSON body of the incoming request
- `headers`: Request headers
- `method`: HTTP method
- `path`: Request path

It returns a dict with:

- `action`: `"fork"` (fork from labeled checkpoint)
- `label`: Checkpoint label to fork from
- `exec`: Shell command to run on the forked computer
- `self_destruct`: `true` to checkpoint and destroy after exec
- `exclusive`: `"defer_on_conflict"` to queue if another fork is active

## Troubleshooting

### Bot doesn't respond

1. Check Telegram webhook is set: `curl https://api.telegram.org/bot$TOKEN/getWebhookInfo`
2. Check ingress logs for the Telegram rule — is the webhook being received?
3. Check if "agent-brain" checkpoint exists: `GET /checkpoints?label=agent-brain`
4. Check server logs: `journalctl -u mshkn --since '5 min ago'`

### Claude doesn't respond (no callback)

1. Check ingress logs for the callback rule — any entries after the trigger?
2. Verify lampas is reachable: `curl https://lampas.dev/health`
3. Check lampas job status: the job_id is in brain.py's stdout (if you have log retention)
4. Try sending the same request to lampas manually (see SETUP.md monitoring section)

### Truncated responses

brain.py detects truncated JSON (starts with `[` but no closing `]`) and asks Claude to continue with smaller chunks. If this happens frequently, the `max_tokens` in brain.py may need increasing, or the system prompt should emphasize smaller tool commands.

### Box B tools fail

1. Check if the Box B recipe has all needed packages
2. Check ingress logs for tool result callbacks
3. Fork the latest `box-b-tools` checkpoint and inspect the filesystem

### Conversation gets stuck

The conversation state lives in `/agent/state.json` on the checkpoint chain. To reset:

```bash
# Fork the latest agent-brain checkpoint
CKPT=$(curl -s "$MSHKN_API_URL/checkpoints?label=agent-brain" \
  -H "Authorization: Bearer $MSHKN_API_KEY" \
  | python3 -c "import json,sys; ckpts=json.load(sys.stdin); ckpts.sort(key=lambda c: c['created_at'], reverse=True); print(ckpts[0]['checkpoint_id'])")

# Fork, reset state, and re-checkpoint
curl -s -X POST "$MSHKN_API_URL/checkpoints/$CKPT/fork" \
  -H "Authorization: Bearer $MSHKN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"exec": "echo '{\"messages\": [], \"turn\": 0, \"chat_id\": \"\"}' > /agent/state.json", "self_destruct": true}'
```
