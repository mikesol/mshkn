# Telegram Agent Design (Issue #34)

## Goal

Build a Telegram-based AI agent that runs on mshkn disposable computers, proving the pistachio compute model. The agent receives user messages via Telegram, thinks via Claude API (through lampas), and executes tool calls on ephemeral VMs. No computer is ever "always on" — they boot, do one thing, checkpoint, and die.

The deliverable is a Claude Code skill that lets any agent with an API key set up this Telegram agent on mshkn + lampas.

## Architecture: Two Boxes

### Box A — The Brain

- **Purpose**: All ingress (Telegram messages + LLM responses + tool results). All decision-making.
- **Lifecycle**: Ephemeral. Boots from labeled checkpoint, processes input, takes action, checkpoints, dies.
- **Exclusive mode**: `defer_on_conflict` with `meta_exec` batching. Multiple messages arriving while Box A is running get queued and processed on next boot.
- **State**: Conversation history lives on Box A's filesystem (checkpoint chain = memory).
- **Actions on each boot**:
  1. Read new input (Telegram message, tool result, or LLM response) from exec payload / deferred queue
  2. Append to conversation history on disk
  3. Call Claude via lampas (structured I/O, not native tool_use)
  4. LLM responds with structured actions → callback boots new Box A
  5. Box A executes actions: send Telegram message, dispatch tool to Box B, or both
  6. Track outstanding tool uses (emitted count)
  7. Checkpoint and die

### Box B — The Hands

- **Purpose**: Executes tool calls. Long-running processes (builds, file edits, etc.).
- **Lifecycle**: Longer-lived than Box A but still disposable. Created when first tool is dispatched, destroyed when all tools complete.
- **Exclusive mode**: `error_on_conflict` (409). Only one Box B per agent session.
- **Concurrency**: Multiple background processes via `POST /computers/{id}/exec/bg`.
- **Completion**: Each bg process, when done, callbacks to Box A (via mshkn fork-from-label) with raw result. Box A then calls Claude with the result.
- **Destruction**: Box A destroys Box B when `tool_uses_emitted == tool_results_received`.

### Flow Example

```
User sends "build my React app" via Telegram
  → Telegram webhook POSTs to mshkn → forks Box A from label "brain-{chat_id}"
  → Box A reads message, appends to conversation history
  → Box A calls Claude via lampas: "user wants React app built"
  → Claude responds: [{"type": "telegram", "text": "Starting build..."}, {"type": "tool", "id": "t1", "command": "npx create-react-app myapp && cd myapp && npm run build"}]
  → Callback → Box A boots, reads Claude's response
  → Box A sends Telegram message, creates/execs Box B with build command
  → Box A checkpoints, dies (outstanding_tools=1)
  → Box B runs build in background
  → Build completes → Box B exec script callbacks to Box A (fork from label with result)
  → Box A boots, reads tool result, calls Claude: "build completed, output: ..."
  → Claude responds: [{"type": "telegram", "text": "Build done! Deploying to here.now..."}]
  → Box A sends Telegram, destroys Box B (emitted==received), checkpoints, dies
```

## Structured I/O (Not Native Tool Use)

Classic Claude tool_use protocol is synchronous — tool_result must immediately follow tool_use in the message array. Our model is fundamentally async (multiple tool calls in flight, user messages arriving mid-execution). So we use structured output:

**Input to Claude**: Full conversation history as structured JSON, including:
- User messages (from Telegram)
- Prior Claude outputs
- Tool results (with IDs, matched to dispatches)
- System prompt describing available actions and current state

**Output from Claude**: JSON array of actions:
```json
[
  {"type": "telegram", "text": "Starting the build now..."},
  {"type": "tool", "id": "t1", "command": "npm run build"}
]
```

## Key Integration Points

- **lampas** (lampas.dev): Async HTTP proxy. Box A calls Claude via `POST /forward` with callback URL pointing back to mshkn (fork Box A's label). lampas calls Claude API, wraps response in envelope, POSTs to callback.
- **Telegram**: Webhook mode — Telegram POSTs to a public URL on mshkn when user sends message. This triggers a fork of Box A.
- **mshkn** (135.181.6.215:8000): Hosts both Box A and Box B. API handles fork, exec, checkpoint, destroy, callbacks.
- **Caddy** (*.mshkn.dev): Routes webhook URLs to mshkn API.

## Credentials

Stored in `.env`:
- `ANTHROPIC_API_KEY` — for Claude API calls via lampas
- `TELEGRAM_BOT_TOKEN` (StronglyNormalBot) — for Mike ↔ Claude Code communication
- `TEST_BOT_TOKEN` (@mshkn_test_agent_bot) — the test agent bot
- mshkn test account: `acct-mike` / `mk-test-key-2026`

## Validation Plan

### Atoms (~15, individual integration points)

1. lampas → Claude: POST /forward targeting Anthropic API, verify 202 + job_id
2. lampas callback delivery: Claude response envelope arrives at callback URL on mshkn
3. Claude structured output: Claude produces valid JSON matching our action schema
4. Test bot poll: getUpdates on @mshkn_test_agent_bot receives messages
5. Test bot send: sendMessage from test bot delivers to user
6. VM outbound HTTP: mshkn computer can curl external URLs
7. VM → lampas: mshkn computer can POST to lampas.dev
8. Telegram webhook: set webhook URL on test bot, receive POST on message
9. Pistachio turn: fork-from-label + exec + self_destruct + callback lifecycle
10. exec/bg: background process on computer, stays alive, queryable
11. Recipe bootstrap: create recipe with node Dockerfile, create computer with recipe_id, verify npx/npm work
12. Upload state: upload JSON to computer, readable inside VM
13. Deferred queue: defer_on_conflict queues, destroy drains
14. Caddy webhook route: public URL routes to mshkn API
15. Label round-trip: create labeled checkpoint, fork by label, verify lineage

### Molecules (~10, 2-3 atoms composed)

1. LLM-triggered fork: lampas → Claude → callback → mshkn fork
2. Telegram → compute: webhook → fork + exec + self_destruct
3. Compute → LLM: exec output → lampas → Claude → callback
4. A → B → A: Box A forks Box B, Box B callbacks to Box A
5. Telegram → reply: message → fork Box A → exec sends reply → self-destruct
6. Structured dispatch: Claude output parsed → correct action taken
7. bg exec + status: Box B running bg process, Box A queries status on same Box B
8. State persistence: upload JSON → checkpoint → fork → JSON survives
9. Deferred batching: multiple messages → all batch into meta_exec
10. Recipe + build: computer with node recipe, run create-react-app

### Cells (~5, complete sub-workflows)

1. Full agent turn: Telegram → Box A → Claude → Box A → Box B → callback → Box A → Telegram
2. Concurrent tools: Box A dispatches two bg processes to Box B, muxes results
3. Long process + interrupt: Box B builds, user asks status mid-build
4. Multi-turn conversation: 3+ turns preserving history across checkpoint chain
5. Error recovery: Box B fails, Box A reports error to user gracefully

### Organism

Full demo: user sends "build me a React todo app and deploy it to here.now" via Telegram → agent builds it across multiple turns using disposable computers → deploys → reports URL.

## Hard Constraints

- **No always-on computers.** Every computer is disposable between agent turns. This is the whole point.
- **No papering over failures.** If an atom fails, stop and regroup. Don't weaken the test.
- **Inform the user** after each atom/molecule/cell completion.
- **Ask when stuck** rather than defaulting to path of least resistance.
