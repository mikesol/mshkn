#!/usr/bin/env python3
"""Agent brain — processes triggers, manages state, dispatches actions."""
import json
import os
import subprocess
import sys

STATE_FILE = "/agent/state.json"
CONFIG_FILE = "/agent/config.json"

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
            "max_tokens": 8192,
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
                "For multi-step tasks, chain commands with && or use a single script."
            ),
            "messages": messages,
        },
        "timeout_ms": 60000,
    })
    r = subprocess.run(
        ["curl", "-s", "-X", "POST", "https://lampas.dev/forward",
         "-H", "Content-Type: application/json", "-d", body],
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

def create_box_b(tool_commands):
    """Create Box B (the hands) and dispatch tool commands as bg processes."""
    config = load_config()
    base = "http://135.181.6.215:8000"
    headers = {"Authorization": f"Bearer {config.get('api_key_mshkn', 'mk-test-key-2026')}"}
    callback_rule = config["callback_rule"]

    # Build a script that runs all tools and callbacks results
    # Download publish.sh for here.now deployment capability
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
        # Run each tool, capture output, and callback result
        script_parts.append(f"""
# Tool {tid}
(
  RESULT=$({cmd} 2>&1) || RESULT="ERROR: $?"
  curl -s -X POST {callback_url} \\
    -H 'Content-Type: application/json' \\
    -d '{{"lampas_job_id": "tool-{tid}", "lampas_status": "completed", "response_status": 200, "response_headers": {{}}, "response_body": {{"content": [{{"type": "text", "text": "Tool {tid} result: '"$RESULT"'"}}]}}}}'
) &
""")
    script_parts.append("wait")
    full_script = "\n".join(script_parts)

    # Create Box B computer with the script
    r = subprocess.run(
        ["curl", "-s", "-X", "POST", f"{base}/computers",
         "-H", "Authorization: Bearer mk-test-key-2026",
         "-H", "Content-Type: application/json",
         "-d", json.dumps({
             "recipe_id": config.get("recipe_id"),
             "exec": full_script,
             "self_destruct": True,
             "label": "box-b-tools",
         })],
        capture_output=True, text=True,
    )
    print(f"Created Box B: {r.stdout[:200]}")

def handle_claude_response():
    response_text = os.environ.get("RESPONSE", "")
    if not response_text:
        print("No response text")
        return

    state = load_state()
    chat_id = state.get("chat_id", "6522858700")

    # Check if this is a tool result (starts with "Tool t")
    if response_text.startswith("Tool t") and " result: " in response_text:
        # This is a tool result from Box B, not a Claude response
        state["tools_received"] = state.get("tools_received", 0) + 1
        state["messages"].append({
            "role": "user",
            "content": f"[Tool result]: {response_text}",
        })
        save_state(state)
        print(f"Received tool result ({state['tools_received']}/{state.get('tools_emitted', '?')})")

        # Call Claude with the tool result
        call_claude(state["messages"])
        return

    # Parse actions from Claude's JSON response
    raw = response_text.strip()
    try:
        actions = json.loads(raw)
    except json.JSONDecodeError:
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
        state["tools_emitted"] = state.get("tools_emitted", 0) + len(tool_commands)
        save_state(state)
        create_box_b(tool_commands)

    # Save assistant response to conversation
    state["messages"].append({"role": "assistant", "content": raw})
    save_state(state)
    print(f"Processed {len(actions)} actions ({len(tool_commands)} tools)")

if __name__ == "__main__":
    trigger = sys.argv[1] if len(sys.argv) > 1 else ""
    if trigger == "telegram":
        handle_telegram()
    elif trigger == "claude_response":
        handle_claude_response()
    else:
        print(f"Unknown trigger: {trigger}")
