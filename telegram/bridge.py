#!/usr/bin/env python3
"""
Telegram <-> filesystem bridge.

Modes:
  --daemon   Run forever: poll messages, log them, send outgoing replies.
  --watch    Poll until new message(s) arrive, print them, then exit.
             Designed for Claude's run_in_background — the exit triggers a
             notification so Claude can process and re-launch.
  --send CHAT_ID TEXT   Send a single message and exit.
  --offset N            Start from this update offset (skip older messages).

The offset state is persisted in offset.txt so --watch picks up where it
left off across invocations.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Config ──────────────────────────────────────────────────────────────────

HERE = Path(__file__).resolve().parent

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

API = f"https://api.telegram.org/bot{TOKEN}"
FILES_DIR = HERE / "files"
INCOMING = HERE / "incoming.jsonl"
OUTGOING = HERE / "outgoing.jsonl"
OFFSET_FILE = HERE / "offset.txt"


# ── Helpers ─────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_offset() -> int | None:
    if OFFSET_FILE.exists():
        try:
            return int(OFFSET_FILE.read_text().strip())
        except ValueError:
            pass
    return None


def save_offset(offset: int) -> None:
    OFFSET_FILE.write_text(str(offset))


def get_updates(offset: int | None = None, timeout: int = 10) -> list[dict]:
    params: dict = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(f"{API}/getUpdates", params=params, timeout=timeout + 5)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        log(f"getUpdates error: {e}")
        time.sleep(5)  # backoff on errors to avoid tight spin loops
        return []


def send_message(chat_id: int, text: str) -> bool:
    try:
        r = requests.post(
            f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        r.raise_for_status()
        log(f"  -> sent to {chat_id}: {text[:80]}")
        return True
    except Exception as e:
        log(f"  -> send error: {e}")
        return False


def append_jsonl(path: Path, obj: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def download_file(file_id: str, file_name: str | None = None) -> Path | None:
    """Download a file from Telegram by file_id. Returns local path or None."""
    try:
        r = requests.get(f"{API}/getFile", params={"file_id": file_id}, timeout=10)
        r.raise_for_status()
        file_info = r.json().get("result", {})
        file_path = file_info.get("file_path")
        if not file_path:
            return None
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        local_name = file_name or Path(file_path).name
        # Prefix with timestamp to avoid collisions
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        local_path = FILES_DIR / f"{ts}_{local_name}"
        dl = requests.get(
            f"https://api.telegram.org/file/bot{TOKEN}/{file_path}", timeout=30
        )
        dl.raise_for_status()
        local_path.write_bytes(dl.content)
        log(f"  -> downloaded {local_name} ({len(dl.content)} bytes)")
        return local_path
    except Exception as e:
        log(f"  -> file download error: {e}")
        return None


def extract_file_info(msg: dict) -> dict | None:
    """Extract file_id and file_name from a message with an attachment."""
    if "document" in msg:
        doc = msg["document"]
        return {"file_id": doc["file_id"], "file_name": doc.get("file_name", "document")}
    if "photo" in msg:
        # Photos come as an array of sizes; grab the largest
        photo = msg["photo"][-1]
        return {"file_id": photo["file_id"], "file_name": "photo.jpg"}
    if "video" in msg:
        vid = msg["video"]
        return {"file_id": vid["file_id"], "file_name": vid.get("file_name", "video.mp4")}
    if "voice" in msg:
        return {"file_id": msg["voice"]["file_id"], "file_name": "voice.ogg"}
    if "audio" in msg:
        aud = msg["audio"]
        return {"file_id": aud["file_id"], "file_name": aud.get("file_name", "audio.mp3")}
    return None


def parse_updates(updates: list[dict]) -> list[dict]:
    """Extract message records from raw updates — accepts all chats."""
    records = []
    for u in updates:
        msg = u.get("message")
        if not msg:
            continue
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "") or msg.get("caption", "")
        user = msg.get("from", {})
        sender = user.get("first_name", "") or user.get("username", "unknown")

        record: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "update_id": u["update_id"],
            "chat_id": chat_id,
            "sender": sender,
            "text": text,
        }

        file_info = extract_file_info(msg)
        if file_info:
            local_path = download_file(file_info["file_id"], file_info["file_name"])
            record["file_name"] = file_info["file_name"]
            record["file_path"] = str(local_path) if local_path else None
            if not text:
                text = f"[file: {file_info['file_name']}]"
                record["text"] = text

        records.append(record)
    return records


def read_and_clear_outgoing() -> list[dict]:
    if not OUTGOING.exists():
        return []
    lines: list[dict] = []
    try:
        with open(OUTGOING, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    lines.append(json.loads(line))
        if lines:
            open(OUTGOING, "w").close()
    except Exception as e:
        log(f"outgoing read error: {e}")
    return lines


def verify_bot() -> str:
    try:
        me = requests.get(f"{API}/getMe", timeout=10).json()
        return me.get("result", {}).get("username", "?")
    except Exception as e:
        sys.exit(f"Cannot reach Telegram API: {e}")


# ── Modes ───────────────────────────────────────────────────────────────────

def mode_watch() -> None:
    """Poll until new message(s) arrive, print them, then exit."""
    bot_name = verify_bot()
    log(f"watching @{bot_name} for messages...")

    INCOMING.touch()
    offset = load_offset()

    while True:
        updates = get_updates(offset, timeout=30)
        if not updates:
            continue

        # Track offset
        new_offset = max(u["update_id"] for u in updates) + 1
        save_offset(new_offset)

        records = parse_updates(updates)
        for r in records:
            append_jsonl(INCOMING, r)

        if records:
            # Print a clear summary and exit — this triggers Claude's notification
            log(f"{len(records)} new message(s):")
            for r in records:
                line = f"  {r['sender']} (chat {r['chat_id']}): {r['text'][:200]}"
                if r.get("file_path"):
                    line += f"\n    -> file saved: {r['file_path']}"
                log(line)
            log("RELAUNCH: python telegram/bridge.py watch")
            return


def mode_daemon() -> None:
    """Run forever: poll, log, send outgoing."""
    bot_name = verify_bot()
    log(f"daemon started for @{bot_name}")

    INCOMING.touch()
    OUTGOING.touch()
    offset = load_offset()

    while True:
        updates = get_updates(offset)
        for u in updates:
            offset = u["update_id"] + 1
            save_offset(offset)

        records = parse_updates(updates)
        for r in records:
            append_jsonl(INCOMING, r)
            log(f"  <- {r['sender']} ({r['chat_id']}): {r['text'][:120]}")

        outgoing = read_and_clear_outgoing()
        for o in outgoing:
            chat_id = o.get("chat_id")
            text = o.get("text", "")
            if chat_id and text:
                send_message(int(chat_id), text)

        if not updates:
            time.sleep(3)


def mode_send(chat_id: int, text: str) -> None:
    """Send a single message and exit."""
    verify_bot()
    if send_message(chat_id, text):
        log("sent ok")
    else:
        sys.exit(1)


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram bridge")
    sub = parser.add_subparsers(dest="mode")

    sub.add_parser("watch", help="Wait for messages, print them, exit")
    sub.add_parser("daemon", help="Run forever")

    send_p = sub.add_parser("send", help="Send a message")
    send_p.add_argument("chat_id", type=int)
    send_p.add_argument("text")

    # Default to watch mode
    args = parser.parse_args()
    if args.mode is None:
        args.mode = "watch"

    if args.mode == "watch":
        mode_watch()
    elif args.mode == "daemon":
        mode_daemon()
    elif args.mode == "send":
        mode_send(args.chat_id, args.text)


if __name__ == "__main__":
    main()
