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
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 500,
            "system": (
                "You are a helpful assistant running on a disposable VM. "
                "Respond concisely. Respond with a JSON array of actions. "
                'Each action is: {"type": "telegram", "text": "your message"}. '
                "Respond with ONLY the raw JSON array, no markdown fences."
            ),
            "messages": messages + [{"role": "assistant", "content": "["}],
        },
        "timeout_ms": 30000,
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

def handle_claude_response():
    response_text = os.environ.get("RESPONSE", "")
    if not response_text:
        print("No response text")
        return
    
    # Parse actions - Claude continues from our "[" prefill
    raw = response_text if response_text.startswith("[") else "[" + response_text
    try:
        actions = json.loads(raw)
    except json.JSONDecodeError:
        actions = [{"type": "telegram", "text": response_text[:500]}]
    
    state = load_state()
    chat_id = state.get("chat_id", "6522858700")
    
    for action in actions:
        if action.get("type") == "telegram":
            send_telegram(chat_id, action.get("text", ""))
    
    # Save assistant response to conversation
    state["messages"].append({"role": "assistant", "content": raw})
    save_state(state)
    print(f"Processed {len(actions)} actions")

if __name__ == "__main__":
    trigger = sys.argv[1] if len(sys.argv) > 1 else ""
    if trigger == "telegram":
        handle_telegram()
    elif trigger == "claude_response":
        handle_claude_response()
    else:
        print(f"Unknown trigger: {trigger}")
