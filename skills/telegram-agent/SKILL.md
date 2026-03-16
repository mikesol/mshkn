---
name: telegram-agent
description: Deploy a Telegram bot agent on mshkn disposable computers with lampas as the async LLM proxy. Use when someone wants to set up an AI agent that receives messages via Telegram, thinks via Claude, and executes tool calls on ephemeral VMs.
allowed-tools: Bash, Read, Write, Glob, Grep
---

# telegram-agent

A Telegram bot agent running entirely on mshkn disposable computers. No computer is ever "always on" — they boot, process one input, checkpoint, and die. The agent receives user messages via Telegram, thinks via Claude API (through lampas), and executes tool calls on ephemeral VMs.

## Architecture: Two Boxes

### Box A — The Brain

Runs `brain.py`. Handles all ingress (Telegram messages, LLM responses, tool results). On each boot:

1. Read input from exec payload (Telegram message, Claude response, or tool result)
2. Append to conversation history on disk (`/agent/state.json`)
3. Either call Claude via lampas or dispatch tool commands to Box B
4. Checkpoint and die

### Box B — The Hands

Executes tool commands from Claude (npm install, write files, build, deploy). Each tool batch runs on a fresh fork of the previous Box B checkpoint, preserving installed packages and files across batches.

### Flow

```
User sends Telegram message
  → Telegram webhook → mshkn ingress → fork Box A from "agent-brain" checkpoint
  → Box A reads message, calls Claude via lampas
  → Box A checkpoints, dies

Claude responds (via lampas callback)
  → mshkn ingress → fork Box A from latest "agent-brain" checkpoint
  → Box A parses JSON actions, sends Telegram messages, dispatches tools to Box B
  → Box A checkpoints, dies

Tool completes on Box B
  → Box B callbacks result to mshkn ingress → fork Box A
  → Box A reads result, calls Claude with it
  → Cycle repeats until Claude sends final Telegram message
```

## Prerequisites

- **mshkn API access**: Account + API key on a mshkn server
- **lampas**: Deployed at lampas.dev (or your own instance). Used as async proxy for Claude API calls.
- **Telegram Bot Token**: Create a bot via @BotFather
- **Anthropic API Key**: For Claude API calls via lampas
- **mshkn ingress rules**: Two rules — one for Telegram webhook, one for lampas callbacks

## Files

| File | Purpose |
|------|---------|
| [SETUP.md](SETUP.md) | Step-by-step provisioning guide |
| [REFERENCE.md](REFERENCE.md) | API reference, troubleshooting, architecture details |
| [scripts/brain.py](scripts/brain.py) | The agent brain — upload to Box A |

## Quick Start

See [SETUP.md](SETUP.md) for the full provisioning guide. The high-level steps:

1. Create two ingress rules (Telegram webhook + Claude callback)
2. Create recipes for Box A (python3+curl) and Box B (nodejs+npm+jq+file+curl)
3. Create Box A, upload brain.py + config.json, checkpoint as "agent-brain"
4. Configure Starlark transforms on ingress rules
5. Set Telegram webhook URL
6. Send a message to your bot
