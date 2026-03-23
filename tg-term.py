#!/usr/bin/env python3
"""tg-term: Remote terminal via Telegram -> WezTerm.

Send messages from your phone to control terminal sessions on your Mac.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_env():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

load_env()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

ALLOWED_USER_IDS: set[int] = set()
_raw = os.environ.get("ALLOWED_USER_IDS", "")
if _raw:
    ALLOWED_USER_IDS = {int(x.strip()) for x in _raw.split(",") if x.strip()}

MAX_LINES = 50
MAX_CHARS = 4000          # Telegram limit is 4096; leave room for wrapper
CAPTURE_DELAY = 1.5       # seconds to wait after sending a command
POLL_TIMEOUT = 30         # long-poll timeout in seconds

# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def tg(method: str, **kwargs):
    try:
        r = requests.post(f"{API}/{method}", json=kwargs, timeout=POLL_TIMEOUT + 10)
        return r.json()
    except requests.RequestException as e:
        print(f"  tg error: {e}")
        return {"ok": False}

def reply(chat_id: int, text: str):
    text = text or "(no output)"
    # split into Telegram-safe chunks
    for i in range(0, len(text), MAX_CHARS):
        tg("sendMessage", chat_id=chat_id, text=text[i:i + MAX_CHARS])

def poll(offset: int | None):
    params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.post(f"{API}/getUpdates", json=params, timeout=POLL_TIMEOUT + 10)
        data = r.json()
        return data.get("result", []) if data.get("ok") else []
    except requests.RequestException:
        time.sleep(3)
        return []

# ---------------------------------------------------------------------------
# WezTerm helpers
# ---------------------------------------------------------------------------

def wez(*args: str, stdin: str | None = None) -> str:
    r = subprocess.run(
        ["wezterm", "cli", *args],
        input=stdin, capture_output=True, text=True, timeout=10,
    )
    return r.stdout.strip()

def wez_spawn(name: str | None = None) -> int:
    pane_id = int(wez("spawn"))
    if name:
        try:
            wez("set-tab-title", name, "--pane-id", str(pane_id))
        except Exception:
            pass
    return pane_id

def wez_send(pane_id: int, text: str, *, raw: bool = False):
    args = ["send-text", "--pane-id", str(pane_id)]
    if raw:
        args.append("--no-paste")
    wez(*args, stdin=text)

def wez_get(pane_id: int) -> str:
    return wez("get-text", "--pane-id", str(pane_id))

def wez_kill(pane_id: int):
    wez("kill-pane", "--pane-id", str(pane_id))

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

# { chat_id: { "active": pane_id|None, "sessions": { pane_id: {"name": str, "prev": str} } } }
state: dict[int, dict] = {}

def st(chat_id: int) -> dict:
    if chat_id not in state:
        state[chat_id] = {"active": None, "sessions": {}}
    return state[chat_id]

def active_pane(chat_id: int) -> int | None:
    s = state.get(chat_id)
    if not s or s["active"] is None:
        return None
    if s["active"] not in s["sessions"]:
        s["active"] = None
        return None
    return s["active"]

def create_session(chat_id: int, name: str | None = None) -> tuple[int, str]:
    s = st(chat_id)
    name = name or f"tg-{len(s['sessions'])}"
    pid = wez_spawn(name)
    s["sessions"][pid] = {"name": name, "prev": ""}
    s["active"] = pid
    return pid, name

# ---------------------------------------------------------------------------
# Output capture
# ---------------------------------------------------------------------------

def truncate(text: str) -> str:
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) > MAX_LINES:
        lines = lines[-MAX_LINES:]
    out = "\n".join(lines)
    if len(out) > MAX_CHARS:
        out = "...(truncated)\n" + out[-MAX_CHARS:]
    return out

def capture_new(chat_id: int, pane_id: int) -> str:
    """Capture pane text and return only lines that are new since last capture."""
    sess = state[chat_id]["sessions"][pane_id]
    current = wez_get(pane_id)
    prev = sess["prev"]
    sess["prev"] = current

    if not prev:
        return truncate(current)

    prev_lines = prev.splitlines()
    cur_lines = current.splitlines()

    # Fast path: if current ends with entirely new content, find the split.
    # We look for the last N lines of prev in current as an anchor.
    anchor_size = min(5, len(prev_lines))
    anchor = prev_lines[-anchor_size:] if anchor_size else []

    if anchor:
        # search backwards through current for the anchor
        for i in range(len(cur_lines) - anchor_size, -1, -1):
            if cur_lines[i:i + anchor_size] == anchor:
                new_lines = cur_lines[i + anchor_size:]
                return truncate("\n".join(new_lines)) if new_lines else "(no new output)"

    # Fallback: show last N lines of current screen
    return truncate(current)

# ---------------------------------------------------------------------------
# Key map
# ---------------------------------------------------------------------------

KEYS = {
    "ctrl+c": "\x03", "ctrl+d": "\x04", "ctrl+z": "\x1a", "ctrl+l": "\x0c",
    "ctrl+a": "\x01", "ctrl+e": "\x05", "ctrl+r": "\x12", "ctrl+p": "\x10",
    "ctrl+n": "\x0e", "ctrl+u": "\x15", "ctrl+k": "\x0b", "ctrl+w": "\x17",
    "enter": "\r", "tab": "\t", "escape": "\x1b", "esc": "\x1b",
    "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
    "backspace": "\x7f", "delete": "\x1b[3~",
    "home": "\x1b[H", "end": "\x1b[F",
    "page_up": "\x1b[5~", "page_down": "\x1b[6~",
}

# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

HELP = """Remote Terminal via Telegram

Commands:
  (any text) - execute as shell command
  /new [name] - create terminal session
  /list - list sessions
  /switch <id> - switch active session
  /screen - capture current screen
  /kill <id> - kill session
  /key <key> - send key (ctrl+c, enter, up, tab...)
  /keys - list available keys
  /raw <text> - send text without newline
  /wait <secs> - re-capture after delay
  /help - show this"""

def cmd_new(chat_id, args):
    pid, name = create_session(chat_id, args.strip() or None)
    return f"Session '{name}' created (pane {pid})"

def cmd_list(chat_id, _args):
    s = state.get(chat_id)
    if not s or not s["sessions"]:
        return "No sessions. Use /new to create one."
    lines = []
    for pid, info in s["sessions"].items():
        marker = " *" if pid == s["active"] else ""
        lines.append(f"  [{pid}] {info['name']}{marker}")
    return "Sessions:\n" + "\n".join(lines)

def cmd_switch(chat_id, args):
    s = state.get(chat_id)
    if not s:
        return "No sessions."
    try:
        pid = int(args.strip())
    except (ValueError, AttributeError):
        return "Usage: /switch <pane_id>"
    if pid not in s["sessions"]:
        return f"Unknown pane {pid}. Use /list."
    s["active"] = pid
    return f"Switched to '{s['sessions'][pid]['name']}' (pane {pid})"

def cmd_kill(chat_id, args):
    s = state.get(chat_id)
    if not s:
        return "No sessions."
    try:
        pid = int(args.strip())
    except (ValueError, AttributeError):
        return "Usage: /kill <pane_id>"
    if pid not in s["sessions"]:
        return f"Unknown pane {pid}."
    name = s["sessions"].pop(pid)["name"]
    wez_kill(pid)
    if s["active"] == pid:
        s["active"] = next(iter(s["sessions"]), None)
    return f"Killed '{name}' (pane {pid})"

def cmd_screen(chat_id, _args):
    pid = active_pane(chat_id)
    if pid is None:
        return "No active session. Use /new."
    text = wez_get(pid)
    state[chat_id]["sessions"][pid]["prev"] = text
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        lines = lines[-MAX_LINES:]
    return "\n".join(lines) or "(empty)"

def cmd_key(chat_id, args):
    pid = active_pane(chat_id)
    if pid is None:
        return "No active session. Use /new."
    key = args.strip().lower()
    if key not in KEYS:
        return f"Unknown key '{key}'. Use /keys to list."
    wez_send(pid, KEYS[key], raw=True)
    return f"Sent {key}"

def cmd_keys(_chat_id, _args):
    return "Available keys:\n" + ", ".join(sorted(KEYS))

def cmd_raw(chat_id, args):
    pid = active_pane(chat_id)
    if pid is None:
        return "No active session. Use /new."
    wez_send(pid, args, raw=True)
    time.sleep(0.5)
    return capture_new(chat_id, pid)

def cmd_wait(chat_id, args):
    pid = active_pane(chat_id)
    if pid is None:
        return "No active session. Use /new."
    try:
        delay = float(args.strip())
    except (ValueError, AttributeError):
        delay = CAPTURE_DELAY
    delay = min(delay, 30)  # cap at 30s
    time.sleep(delay)
    return capture_new(chat_id, pid)

def cmd_exec(chat_id, text):
    """Default handler: send command to active pane, return output."""
    pid = active_pane(chat_id)
    if pid is None:
        pid, name = create_session(chat_id)
        reply(chat_id, f"Auto-created session '{name}'")
        time.sleep(0.5)

    wez_send(pid, text + "\n")
    time.sleep(CAPTURE_DELAY)
    return capture_new(chat_id, pid)

COMMANDS = {
    "/start": lambda c, a: HELP,
    "/help": lambda c, a: HELP,
    "/new": cmd_new,
    "/list": cmd_list,
    "/switch": cmd_switch,
    "/kill": cmd_kill,
    "/screen": cmd_screen,
    "/key": cmd_key,
    "/keys": cmd_keys,
    "/raw": cmd_raw,
    "/wait": cmd_wait,
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def handle(chat_id: int, user_id: int, text: str):
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        reply(chat_id, f"Unauthorized. Your user ID: {user_id}")
        return

    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]  # strip @botname suffix
        args = parts[1] if len(parts) > 1 else ""
        handler = COMMANDS.get(cmd)
        if handler:
            reply(chat_id, handler(chat_id, args))
        else:
            reply(chat_id, f"Unknown command: {cmd}. Use /help")
    else:
        reply(chat_id, cmd_exec(chat_id, text))

def main():
    if not BOT_TOKEN:
        print("Set TELEGRAM_BOT_TOKEN in .env or environment.")
        sys.exit(1)

    info = tg("getMe")
    if not info.get("ok"):
        print("Failed to connect. Check your bot token.")
        sys.exit(1)

    bot = info["result"]
    print(f"Bot: @{bot['username']}")
    if ALLOWED_USER_IDS:
        print(f"Allowed users: {ALLOWED_USER_IDS}")
    else:
        print("WARNING: No ALLOWED_USER_IDS — anyone can use this bot!")
    print("Listening...")

    offset = None
    while True:
        for update in poll(offset):
            offset = update["update_id"] + 1
            msg = update.get("message")
            if not msg or "text" not in msg:
                continue
            text = msg["text"].strip()
            if not text:
                continue
            chat_id = msg["chat"]["id"]
            user_id = msg["from"]["id"]
            print(f"  [{user_id}] {text[:80]}")
            try:
                handle(chat_id, user_id, text)
            except Exception as e:
                print(f"  error: {e}")
                reply(chat_id, f"Error: {e}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
