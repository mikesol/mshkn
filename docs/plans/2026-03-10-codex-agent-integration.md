# Using OpenAI Codex Subscriptions as Agent Brains on mshkn

Date: 2026-03-10

## Motivation

mshkn computers are designed to be ephemeral — checkpoint, tear down, restore. An agent runtime that idles inside the VM (thinking, waiting for API responses) fights this pattern. The ideal architecture keeps the LLM outside the VM and only restores computers when there's actual work to execute.

OpenAI Codex subscriptions (ChatGPT Plus/Pro/Team) provide flat-rate access to GPT models. Combined with mshkn's checkpoint/restore, this enables an agent loop where the computer only exists for the seconds it takes to run commands.

## Architecture

```
Agent loop (runs on host or orchestrator):

1. Call Codex API with task + prior context
2. Codex returns tool calls / commands to execute
3. Restore mshkn computer from checkpoint
4. Execute commands via SSH
5. Capture output, checkpoint computer, tear down
6. Feed output back to Codex → goto 1
```

The computer is OFF while the LLM thinks. No idle burn.

## How OpenAI Codex Subscription Auth Works

Research source: OpenClaw (github.com/openclaw/openclaw), which implements Codex as a first-class provider.

### Two OpenAI auth methods

| Method | Provider ID | Base URL | Billing |
|--------|------------|----------|---------|
| API key | `openai` | `api.openai.com` | Pay-per-token |
| Codex subscription | `openai-codex` | `chatgpt.com/backend-api` | Flat monthly rate |

### OAuth flow

The Codex subscription uses OAuth against `auth.openai.com`:

1. Start local HTTP server on a callback port (OpenClaw uses 1455)
2. Open browser to `https://auth.openai.com/oauth/authorize` with redirect to `localhost:<port>`
3. User logs into ChatGPT in browser
4. Callback delivers auth code to local server
5. Exchange code for access token + refresh token

OpenClaw delegates this to `@mariozechner/pi-ai` (npm, v0.57.1):

```typescript
import { loginOpenAICodex } from "@mariozechner/pi-ai/oauth";

const creds = await loginOpenAICodex({
  onAuth: async ({ url }) => { /* open URL in browser */ },
  onPrompt: async (prompt) => { /* get user input if needed */ },
  onProgress: (msg) => { /* status updates */ },
});
// creds.access = bearer token
// creds.refresh = refresh token
```

### Token storage

```json
{
  "openai-codex:default": {
    "type": "oauth",
    "provider": "openai-codex",
    "access": "<bearer-token>",
    "refresh": "<refresh-token>",
    "expires": 1234567890000
  }
}
```

### Making API calls

- **Base URL**: `https://chatgpt.com/backend-api`
- **Auth header**: `Authorization: Bearer <access_token>`
- **Transport**: WebSocket preferred, SSE fallback
- **Request format**: Standard OpenAI chat completions (messages array, model, tools, etc.)
- **Models**: `gpt-5.4`, `gpt-5.3-codex`, `gpt-5.2-codex`, etc.

### Usage / rate limit monitoring

```
GET https://chatgpt.com/backend-api/wham/usage
Authorization: Bearer <token>
ChatGPT-Account-Id: <optional>
```

Response includes:
- `rate_limit.primary_window` — 3-hour usage window with used_percent and reset_at
- `rate_limit.secondary_window` — daily/weekly cap
- `plan_type` — subscription tier
- `credits.balance` — remaining credits

### Token refresh

OpenClaw has special fallback logic: if refresh fails with "Failed to extract accountId from token", it retries with the cached access token. The `ChatGPT-Account-Id` header may be required for some endpoints.

## Running OAuth on mshkn Computers (Port Forwarding)

The OAuth flow requires a browser on one machine and a callback server on another. This works with port forwarding — same pattern applies to any OAuth flow (Codex, Claude Code Max, etc.).

### Option A: SSH tunnel

```bash
# Create mshkn computer
computer_id=$(curl -s -X POST https://mshkn.dev/computers \
  -H "Authorization: Bearer $API_KEY" \
  -d '{"capabilities": ["node"]}' | jq -r '.id')

# Tunnel the OAuth callback port from VM to local machine
ssh -L 1455:localhost:1455 root@${computer_ip} &

# Run the OAuth flow in the VM
# Browser opens locally, callback hits localhost:1455, tunnels to VM
ssh root@${computer_ip} "node oauth-login.js"
```

### Option B: mshkn HTTPS port forwarding via Caddy

mshkn already provides HTTPS forwarding through `*.mshkn.dev`. If the OAuth callback URL can be set to a routable address (not hardcoded to localhost), the flow works without SSH tunnels:

1. Map a port in the computer config
2. OAuth callback goes to `https://{computer_id}.mshkn.dev:{port}/callback`
3. Caddy routes it to the VM

### Option C: Auth once, checkpoint, fork many

The most practical approach:

1. Create a base computer
2. Run the OAuth flow once (using tunnel or manual paste)
3. Checkpoint the computer — credentials are now in the snapshot
4. Fork from this checkpoint for every agent run
5. All forks inherit the auth tokens

Token refresh would need handling — either refresh before each fork, or build refresh logic into the agent loop.

## Minimal Implementation

To build a Codex-powered agent on mshkn without depending on OpenClaw:

### 1. OAuth (one-time setup)

Use `@mariozechner/pi-ai` directly, or roll your own:
- Start HTTP server on fixed port
- Redirect to `auth.openai.com/oauth/authorize`
- Capture callback, exchange for tokens
- Store tokens

### 2. Agent loop (Python pseudocode)

```python
import httpx

CODEX_BASE = "https://chatgpt.com/backend-api"

async def agent_loop(task: str, checkpoint_id: str, token: str):
    messages = [{"role": "user", "content": task}]

    while True:
        # LLM call — no computer running
        resp = await httpx.post(
            f"{CODEX_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"model": "gpt-5.4", "messages": messages, "tools": TOOLS},
        )
        assistant_msg = resp.json()["choices"][0]["message"]
        messages.append(assistant_msg)

        if not assistant_msg.get("tool_calls"):
            break  # Done — no more commands to run

        # Restore computer, execute, checkpoint, tear down
        computer = await mshkn.restore(checkpoint_id)
        for call in assistant_msg["tool_calls"]:
            output = await computer.exec(call["function"]["arguments"]["command"])
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": output,
            })
        checkpoint_id = await computer.checkpoint()
        await computer.destroy()
```

### 3. Rate limit awareness

Poll `/backend-api/wham/usage` between iterations. If `used_percent` is high, back off until `reset_at`.

## Comparison: Codex Subscription vs Claude Code vs API Keys

| | Codex subscription | Claude Code (Max) | API keys (any provider) |
|---|---|---|---|
| Cost model | Flat monthly | Flat monthly | Per-token |
| Auth | OAuth (one-time) | OAuth (one-time) | API key env var |
| Runs inside VM? | No (external API) | Yes (agent runtime) | No (external API) |
| Fits checkpoint/restore? | Yes | No (idles in VM) | Yes |
| Rate limits | 3h window + daily cap | Similar windows | Pay for what you use |
| Headless viable? | Yes | Awkward (needs tunnel for auth, then idles) | Yes (trivially) |

## Key Files in OpenClaw (Reference)

If you need to study the implementation:

- `src/commands/openai-codex-oauth.ts` — OAuth flow orchestration
- `src/infra/provider-usage.fetch.codex.ts` — Usage/rate limit endpoint
- `src/agents/models-config.providers.static.ts:151` — Provider config (base URL, API type)
- `src/agents/pi-embedded-runner/model.provider-normalization.ts` — Model routing
- `src/agents/auth-profiles/oauth.ts` — Token storage and refresh
- `@mariozechner/pi-ai` (npm) — Underlying OAuth and streaming implementation
