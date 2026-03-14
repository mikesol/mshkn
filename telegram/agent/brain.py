#!/usr/bin/env python3
"""Agent brain — processes triggers, manages state, dispatches actions."""
import json
import os
import subprocess
import sys
import traceback

STATE_FILE = "/agent/state.json"
CONFIG_FILE = "/agent/config.json"
RESPONSE_FILE = "/tmp/response.txt"

import re

def fix_json_newlines(text):
    """Fix unescaped newlines inside JSON string values.
    Claude sometimes produces JSON with literal newlines in strings
    instead of \\n escape sequences."""
    # Strategy: find all string values and escape newlines within them
    result = []
    i = 0
    in_string = False
    while i < len(text):
        ch = text[i]
        if not in_string:
            result.append(ch)
            if ch == '"':
                in_string = True
        else:
            if ch == '\\' and i + 1 < len(text):
                # Escaped character — keep as-is
                result.append(ch)
                result.append(text[i + 1])
                i += 2
                continue
            elif ch == '"':
                result.append(ch)
                in_string = False
            elif ch == '\n':
                result.append('\\n')
            elif ch == '\r':
                result.append('\\r')
            elif ch == '\t':
                result.append('\\t')
            else:
                result.append(ch)
        i += 1
    return ''.join(result)

def load_config():
    return json.load(open(CONFIG_FILE))

def load_state():
    try:
        return json.load(open(STATE_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"messages": [], "turn": 0, "chat_id": ""}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    json.dump(state, open(STATE_FILE, "w"))

def send_telegram(chat_id, text):
    config = load_config()
    r = subprocess.run([
        "curl", "-s", "-X", "POST",
        f"https://api.telegram.org/bot{config['bot_token']}/sendMessage",
        "-d", f"chat_id={chat_id}",
        "-d", f"text={text}",
    ], capture_output=True, text=True)
    print(f"Sent telegram to {chat_id}: {text[:80]}")

def call_claude(messages):
    config = load_config()
    callback_url = f"https://api.mshkn.dev/ingress/{config['callback_rule']}"
    body = json.dumps({
        "target": "https://api.anthropic.com/v1/messages",
        "method": "POST",
        "forward_headers": {
            "x-api-key": config["api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        "callbacks": [{"url": callback_url}],
        "body": {
            "model": "claude-sonnet-4-6",
            "max_tokens": 16384,
            "system": (
                "You are a coding assistant running on a disposable Linux VM (Ubuntu 24.04). "
                "You have node, npm, npx, jq, file, and curl available. "
                "The VM has 232MB RAM so use lightweight tools (esbuild, not webpack/react-scripts).\n\n"
                "To deploy websites, use publish.sh at /usr/local/bin/publish.sh: "
                "`publish.sh ./build` (any directory with index.html). "
                "It returns a public URL like https://slug.here.now.\n\n"
                "For React apps: npm install react react-dom esbuild, then bundle with "
                "`npx esbuild src/index.jsx --bundle --outfile=public/bundle.js --minify`.\n\n"
                "Respond with a JSON array of actions. Available actions:\n"
                '- {"type": "telegram", "text": "message to user"}\n'
                '- {"type": "tool", "id": "t1", "command": "bash command"}\n'
                "Respond with ONLY the raw JSON array, no markdown fences. "
                "Use tools to build, run, install, and deploy code on the VM. "
                "When writing files, use heredoc syntax (cat > file << 'EOF'). "
                "IMPORTANT: Keep each tool command under 6000 characters. "
                "If a file is large, split it across multiple tool calls "
                "(e.g., write part 1 to a temp file, then append part 2). "
                "For multi-step tasks, use multiple tool actions rather than one massive command. "
                "Files and installed packages persist between tool batches — "
                "you can npm install in one response and use the packages in the next."
            ),
            "messages": messages,
        },
        "timeout_ms": 120000,
    })
    # Write body to file to avoid massive command-line args
    with open("/tmp/lampas_body.json", "w") as f:
        f.write(body)
    r = subprocess.run(
        ["curl", "-s", "-X", "POST", "https://lampas.dev/forward",
         "-H", "Content-Type: application/json", "-d", "@/tmp/lampas_body.json"],
        capture_output=True, text=True,
    )
    print(f"Called lampas: {r.stdout[:100]}")

def handle_telegram():
    chat_id = os.environ.get("CHAT_ID", "")
    text = os.environ.get("MSG_TEXT", "")
    if not text:
        print("No message text")
        return

    state = load_state()
    state["messages"].append({"role": "user", "content": text})
    state["turn"] += 1
    state["chat_id"] = str(chat_id)
    save_state(state)

    print(f"Turn {state['turn']}: user said '{text}'")
    call_claude(state["messages"])

def find_latest_checkpoint(label):
    """Find the most recent checkpoint with the given label."""
    base = "http://135.181.6.215:8000"
    r = subprocess.run(
        ["curl", "-s", f"{base}/checkpoints",
         "-H", "Authorization: Bearer mk-test-key-2026"],
        capture_output=True, text=True,
    )
    try:
        ckpts = json.loads(r.stdout)
        matching = [c for c in ckpts if c.get("label") == label]
        if matching:
            # Sort by created_at descending, return most recent
            matching.sort(key=lambda c: c["created_at"], reverse=True)
            return matching[0]["checkpoint_id"]
    except (json.JSONDecodeError, KeyError):
        pass
    return None

def create_box_b(tool_commands):
    """Create Box B (the hands) and dispatch tool commands as bg processes.

    Forks from the latest box-b-tools checkpoint if one exists (preserving
    installed packages and files from previous tool runs). Otherwise creates
    a fresh VM from the recipe.
    """
    config = load_config()
    base = "http://135.181.6.215:8000"
    callback_rule = config["callback_rule"]

    # Build a script that runs all tools and callbacks results
    script_parts = [
        "#!/bin/bash",
        "set -e",
        "# Install publish.sh for here.now deployment",
        "curl -fsSL https://raw.githubusercontent.com/heredotnow/skill/main/here-now/scripts/publish.sh -o /usr/local/bin/publish.sh 2>/dev/null && chmod +x /usr/local/bin/publish.sh || true",
    ]
    for tool in tool_commands:
        tid = tool["id"]
        cmd = tool["command"]
        callback_url = f"https://api.mshkn.dev/ingress/{callback_rule}"
        script_parts.append(f"""
# Tool {tid}
(
  RESULT=$({cmd} 2>&1) || RESULT="ERROR: $?"
  # Truncate to 4000 chars and use jq for safe JSON encoding
  RESULT=$(echo "$RESULT" | head -c 4000)
  PAYLOAD=$(jq -n --arg text "Tool {tid} result: $RESULT" \
    '{{"response_body": {{"content": [{{"type": "text", "text": $text}}]}}}}')
  curl -s -X POST {callback_url} \
    -H 'Content-Type: application/json' \
    -d "$PAYLOAD"
) &
""")
    script_parts.append("wait")
    full_script = "\n".join(script_parts)

    # Try to fork from the latest box-b-tools checkpoint (preserves state)
    existing_ckpt = find_latest_checkpoint("box-b-tools")
    if existing_ckpt:
        print(f"Forking Box B from checkpoint {existing_ckpt}")
        payload = json.dumps({
            "exec": full_script,
            "self_destruct": True,
        })
        with open("/tmp/boxb_payload.json", "w") as f:
            f.write(payload)
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", f"{base}/checkpoints/{existing_ckpt}/fork",
             "-H", "Authorization: Bearer mk-test-key-2026",
             "-H", "Content-Type: application/json",
             "-d", "@/tmp/boxb_payload.json"],
            capture_output=True, text=True,
        )
    else:
        print("No existing box-b-tools checkpoint, creating fresh Box B")
        payload = json.dumps({
            "recipe_id": config.get("recipe_id"),
            "exec": full_script,
            "self_destruct": True,
            "label": "box-b-tools",
        })
        with open("/tmp/boxb_payload.json", "w") as f:
            f.write(payload)
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", f"{base}/computers",
             "-H", "Authorization: Bearer mk-test-key-2026",
             "-H", "Content-Type: application/json",
             "-d", "@/tmp/boxb_payload.json"],
            capture_output=True, text=True,
        )
    print(f"Box B: stdout={r.stdout[:200]} stderr={r.stderr[:200]}")

def handle_claude_response():
    # Read response from file (preferred) or env var (fallback)
    response_text = ""
    if os.path.exists(RESPONSE_FILE):
        response_text = open(RESPONSE_FILE).read()
        print(f"Read response from file ({len(response_text)} bytes)")
    if not response_text:
        response_text = os.environ.get("RESPONSE", "")
        print(f"Read response from env ({len(response_text)} bytes)")
    if not response_text:
        print("No response text")
        return

    state = load_state()
    chat_id = state.get("chat_id", "6522858700")

    # Check if this is a lampas failure envelope
    try:
        envelope = json.loads(response_text)
        if isinstance(envelope, dict) and envelope.get("lampas_status") == "failed":
            print(f"Lampas call failed: {envelope}")
            call_claude(state["messages"])
            return
    except (json.JSONDecodeError, ValueError):
        pass

    # Check if this is a tool result (starts with "Tool t")
    if response_text.startswith("Tool t") and " result: " in response_text:
        state["tools_received"] = state.get("tools_received", 0) + 1
        state["messages"].append({
            "role": "user",
            "content": f"[Tool result]: {response_text}",
        })
        save_state(state)
        print(f"Received tool result ({state['tools_received']}/{state.get('tools_emitted', '?')})")
        call_claude(state["messages"])
        return

    # Parse actions from Claude's JSON response
    raw = response_text.strip()
    actions = None
    # Try direct parse, then with newline fix, then bracket extraction
    for attempt_raw in [raw, fix_json_newlines(raw)]:
        try:
            actions = json.loads(attempt_raw)
            break
        except json.JSONDecodeError:
            pass
    if actions is None:
        # Try to extract JSON array from surrounding text
        fixed = fix_json_newlines(raw)
        start = fixed.find("[")
        if start >= 0:
            depth = 0
            for i in range(start, len(fixed)):
                if fixed[i] == "[":
                    depth += 1
                elif fixed[i] == "]":
                    depth -= 1
                    if depth == 0:
                        try:
                            actions = json.loads(fixed[start:i+1])
                        except json.JSONDecodeError as e:
                            print(f"JSON parse failed after fix: {e}")
                        break
    if actions is None:
        # Detect truncated response: starts with [ but never closes
        stripped = raw.strip()
        if stripped.startswith("[") and not stripped.endswith("]"):
            print(f"Detected truncated response ({len(raw)} chars), asking Claude to continue")
            state["messages"].append({"role": "assistant", "content": raw})
            state["messages"].append({
                "role": "user",
                "content": (
                    "[System]: Your previous response was truncated (output too long). "
                    "Please split the work into smaller steps. Instead of writing an entire "
                    "large file in one command, break it into multiple smaller tool calls "
                    "(e.g., write sections to temp files, then combine them). "
                    "Resume from where you left off."
                ),
            })
            save_state(state)
            call_claude(state["messages"])
            return
        print(f"Could not parse actions, sending raw text as telegram. First 200 chars: {response_text[:200]}")
        actions = [{"type": "telegram", "text": response_text[:500]}]

    tool_commands = []
    for action in actions:
        if action.get("type") == "telegram":
            send_telegram(chat_id, action.get("text", ""))
        elif action.get("type") == "tool":
            tool_commands.append({
                "id": action.get("id", "t0"),
                "command": action.get("command", "echo no-command"),
            })

    # Dispatch tools to Box B if any
    if tool_commands:
        print(f"Dispatching {len(tool_commands)} tool(s) to Box B")
        state["tools_emitted"] = state.get("tools_emitted", 0) + len(tool_commands)
        save_state(state)
        create_box_b(tool_commands)

    # Save assistant response to conversation
    state["messages"].append({"role": "assistant", "content": raw})
    save_state(state)
    print(f"Processed {len(actions)} actions ({len(tool_commands)} tools)")

if __name__ == "__main__":
    trigger = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if trigger == "telegram":
            handle_telegram()
        elif trigger == "claude_response":
            handle_claude_response()
        else:
            print(f"Unknown trigger: {trigger}")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
