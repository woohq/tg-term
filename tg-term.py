#!/usr/bin/env python3
"""tg-term: Remote terminal via Telegram -> WezTerm.

Send messages from your phone to control terminal sessions remotely.

Forum mode (primary): Create a Telegram group with Topics enabled, add the
bot as admin. The bot auto-detects all WezTerm panes and creates a topic
for each one. New panes get topics automatically; closed panes get their
topics closed.

DM mode (fallback): Chat directly with the bot when no group is configured.
"""

import json as _json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# Force unbuffered stdout so logs appear immediately
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

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

FORUM_GROUP_ID: int | None = None
_fg = os.environ.get("TELEGRAM_GROUP_ID", "")
if _fg:
    FORUM_GROUP_ID = int(_fg)

MACHINE_NAME = os.environ.get("MACHINE_NAME", "") or platform.node().split(".")[0]

MAX_LINES = 50
MAX_CHARS = 4000
CAPTURE_DELAY = 1.5
POLL_TIMEOUT = 30
MONITOR_INTERVAL = 3  # seconds between pane scans

TOPIC_COLORS = [0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFF93B2, 0xFB6F5F]
_color_idx = 0

def next_topic_color() -> int:
    global _color_idx
    c = TOPIC_COLORS[_color_idx % len(TOPIC_COLORS)]
    _color_idx += 1
    return c

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

def reply(chat_id: int, text: str, thread_id: int | None = None):
    text = text or "(no output)"
    for i in range(0, len(text), MAX_CHARS):
        kwargs: dict = {"chat_id": chat_id, "text": text[i:i + MAX_CHARS]}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        tg("sendMessage", **kwargs)

def poll(offset: int | None):
    params: dict = {
        "timeout": POLL_TIMEOUT,
        "allowed_updates": ["message", "my_chat_member"],
    }
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.post(f"{API}/getUpdates", json=params, timeout=POLL_TIMEOUT + 10)
        data = r.json()
        return data.get("result", []) if data.get("ok") else []
    except requests.RequestException:
        time.sleep(3)
        return []

def create_topic(name: str) -> int | None:
    if not FORUM_GROUP_ID:
        return None
    resp = tg("createForumTopic",
              chat_id=FORUM_GROUP_ID,
              name=name,
              icon_color=next_topic_color())
    if resp.get("ok"):
        return resp["result"]["message_thread_id"]
    print(f"  failed to create topic: {resp}")
    return None

def close_topic(thread_id: int):
    if FORUM_GROUP_ID:
        tg("closeForumTopic", chat_id=FORUM_GROUP_ID, message_thread_id=thread_id)

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

def wez_list_panes() -> list[dict]:
    raw = wez("list", "--format", "json")
    if not raw:
        return []
    try:
        return _json.loads(raw)
    except _json.JSONDecodeError:
        return []

# ---------------------------------------------------------------------------
# Context object
# ---------------------------------------------------------------------------

class Ctx:
    __slots__ = ("chat_id", "user_id", "thread_id", "forum")

    def __init__(self, chat_id: int, user_id: int, thread_id: int | None, forum: bool):
        self.chat_id = chat_id
        self.user_id = user_id
        self.thread_id = thread_id
        self.forum = forum

    def reply(self, text: str):
        reply(self.chat_id, text, self.thread_id)

    @property
    def session_key(self) -> int:
        return self.thread_id if (self.forum and self.thread_id) else self.chat_id

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

# Forum: thread_id -> {"pane_id": int, "name": str, "machine": str, "prev": str}
forum_sessions: dict[int, dict] = {}
# Reverse: pane_id -> thread_id (for monitor to find topics when panes close)
pane_to_thread: dict[int, int] = {}
# DM: chat_id -> {"active": pane_id, "sessions": {pane_id: {...}}}
dm_state: dict[int, dict] = {}
# Lock for thread-safe state access
state_lock = threading.Lock()

# Persistence file for pane<->topic mappings
STATE_FILE = Path(__file__).parent / ".tg-term-state.json"

def save_state():
    """Persist pane<->topic mappings to disk (atomic write)."""
    data = {str(pid): tid for pid, tid in pane_to_thread.items()}
    names = {str(tid): s["name"] for tid, s in forum_sessions.items()}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(_json.dumps({"pane_to_thread": data, "names": names}))
    tmp.replace(STATE_FILE)  # atomic on POSIX

def load_state():
    """Restore pane<->topic mappings from disk."""
    if not STATE_FILE.exists():
        return
    try:
        data = _json.loads(STATE_FILE.read_text())
        for pid_str, tid in data.get("pane_to_thread", {}).items():
            pid = int(pid_str)
            name = data.get("names", {}).get(str(tid), f"pane-{pid}")
            forum_register(tid, pid, name)
        print(f"  restored {len(pane_to_thread)} pane mappings from state file")
    except Exception as e:
        print(f"  failed to load state: {e}")

# --- Forum helpers ---

def forum_get_pane(thread_id: int) -> int | None:
    s = forum_sessions.get(thread_id)
    return s["pane_id"] if s else None

def forum_register(thread_id: int, pane_id: int, name: str):
    """Register a pane<->topic mapping."""
    forum_sessions[thread_id] = {
        "pane_id": pane_id, "name": name, "machine": MACHINE_NAME, "prev": ""
    }
    pane_to_thread[pane_id] = thread_id

def forum_unregister_pane(pane_id: int):
    """Remove a pane and close its topic."""
    tid = pane_to_thread.pop(pane_id, None)
    if tid and tid in forum_sessions:
        del forum_sessions[tid]
        close_topic(tid)
    save_state()

def forum_create(name: str) -> tuple[int, int, str]:
    """Create topic + pane. Returns (thread_id, pane_id, name)."""
    topic_name = f"[{MACHINE_NAME}] {name}"
    thread_id = create_topic(topic_name)
    if thread_id is None:
        raise RuntimeError("Failed to create topic. Is the bot a group admin with topic permissions?")
    pane_id = wez_spawn(name)
    forum_register(thread_id, pane_id, name)
    return thread_id, pane_id, name

def forum_capture(thread_id: int, cmd_text: str | None = None) -> str:
    sess = forum_sessions[thread_id]
    return _capture(sess, cmd_text)

# --- DM helpers ---

def dm_st(chat_id: int) -> dict:
    if chat_id not in dm_state:
        dm_state[chat_id] = {"active": None, "sessions": {}}
    return dm_state[chat_id]

def dm_active_pane(chat_id: int) -> int | None:
    s = dm_state.get(chat_id)
    if not s or s["active"] is None:
        return None
    if s["active"] not in s["sessions"]:
        s["active"] = None
        return None
    return s["active"]

def dm_create(chat_id: int, name: str | None = None) -> tuple[int, str]:
    s = dm_st(chat_id)
    name = name or f"tg-{len(s['sessions'])}"
    pid = wez_spawn(name)
    s["sessions"][pid] = {"pane_id": pid, "name": name, "prev": ""}
    s["active"] = pid
    return pid, name

def dm_capture(chat_id: int, pane_id: int, cmd_text: str | None = None) -> str:
    sess = dm_state[chat_id]["sessions"][pane_id]
    return _capture(sess, cmd_text)

# --- Shared capture ---

def truncate(text: str) -> str:
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) > MAX_LINES:
        lines = lines[-MAX_LINES:]
    out = "\n".join(lines)
    if len(out) > MAX_CHARS:
        out = "...(truncated)\n" + out[-MAX_CHARS:]
    return out

def _capture(sess: dict, cmd_text: str | None = None) -> str:
    """Capture pane text and return only new lines since last capture.
    If cmd_text is provided, strip the echoed command from the output."""
    current = wez_get(sess["pane_id"])
    prev = sess["prev"]
    sess["prev"] = current

    if not prev:
        new_lines = current.splitlines()
    else:
        prev_lines = prev.splitlines()
        cur_lines = current.splitlines()
        anchor_size = min(5, len(prev_lines))
        anchor = prev_lines[-anchor_size:] if anchor_size else []

        new_lines = cur_lines  # fallback
        if anchor:
            for i in range(len(cur_lines) - anchor_size, -1, -1):
                if cur_lines[i:i + anchor_size] == anchor:
                    new_lines = cur_lines[i + anchor_size:]
                    break

    # Strip the echoed command from output
    if cmd_text:
        cmd_stripped = cmd_text.strip()
        new_lines = [l for l in new_lines if cmd_stripped not in l]

    if not new_lines:
        return "(no new output)"

    return truncate("\n".join(new_lines))

# ---------------------------------------------------------------------------
# Pane monitor (background thread)
# ---------------------------------------------------------------------------

def pane_monitor():
    """Auto-detect WezTerm panes and create/close forum topics."""
    if not FORUM_GROUP_ID:
        return

    print("  monitor: started")
    while True:
        try:
            panes = wez_list_panes()
            current_pids = {p["pane_id"] for p in panes}
            pane_info = {p["pane_id"]: p for p in panes}

            with state_lock:
                known_pids = set(pane_to_thread.keys())

                # New panes — create topics
                # Count existing dir names for dedup numbering
                used_names: dict[str, int] = {}
                for s in forum_sessions.values():
                    base = s["name"]
                    used_names[base] = used_names.get(base, 0) + 1

                for pid in current_pids - known_pids:
                    info = pane_info[pid]
                    cwd = info.get("cwd", "")
                    if cwd:
                        dirname = cwd.replace("file://", "").rstrip("/").split("/")[-1]
                    else:
                        dirname = f"pane-{pid}"
                    # Deduplicate: if name already used, add (2), (3), etc.
                    base = dirname
                    count = used_names.get(base, 0)
                    if count > 0:
                        dirname = f"{base} ({count + 1})"
                    used_names[base] = count + 1

                    topic_name = f"[{MACHINE_NAME}] {dirname}"
                    tid = create_topic(topic_name)
                    if tid:
                        forum_register(tid, pid, base)
                        save_state()
                        print(f"  monitor: pane {pid} -> '{topic_name}'")

                # Closed panes — close topics
                for pid in known_pids - current_pids:
                    tid = pane_to_thread.get(pid)
                    name = forum_sessions.get(tid, {}).get("name", "?") if tid else "?"
                    forum_unregister_pane(pid)
                    print(f"  monitor: pane {pid} closed -> topic '{name}' closed")

            # Watch for local output changes on all tracked panes
            for tid, sess in list(forum_sessions.items()):
                try:
                    current = wez_get(sess["pane_id"])
                    prev = sess["prev"]
                    if not prev:
                        # First capture — just save, don't spam the full screen
                        sess["prev"] = current
                        continue
                    if current == prev:
                        continue
                    # Output changed — compute diff and push to Telegram
                    sess["prev"] = current
                    prev_lines = prev.splitlines()
                    cur_lines = current.splitlines()
                    anchor_size = min(5, len(prev_lines))
                    anchor = prev_lines[-anchor_size:] if anchor_size else []
                    new_lines = cur_lines
                    if anchor:
                        for i in range(len(cur_lines) - anchor_size, -1, -1):
                            if cur_lines[i:i + anchor_size] == anchor:
                                new_lines = cur_lines[i + anchor_size:]
                                break
                    new_lines = [l for l in new_lines if l.strip()]
                    if new_lines:
                        text = truncate("\n".join(new_lines))
                        if text and text != "(no new output)":
                            reply(FORUM_GROUP_ID, text, tid)
                except Exception:
                    pass

        except Exception as e:
            print(f"  monitor error: {e}")

        time.sleep(MONITOR_INTERVAL)

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

HELP_FORUM = """Remote Terminal (Forum Mode)

Panes are auto-detected. Each WezTerm pane gets its own topic.
Messages in a topic go to that pane. Only output is returned.

Commands:
  (any text) - type into terminal + press enter
  /new <name> - create new pane + topic
  /panes - show all WezTerm panes
  /attach <pane_id> - attach existing pane to this topic
  /screen - capture current screen
  /kill - kill this topic's pane
  /merge <pane_id> - move another pane into this topic
  /key <key> - send key (ctrl+c, enter, up, tab...)
  /keys - list available keys
  /raw <text> - send text without pressing enter
  /wait <secs> - re-capture after delay
  /help - show this"""

HELP_DM = """Remote Terminal (DM Mode)

Commands:
  (any text) - type into terminal + press enter
  /new [name] - create terminal session
  /attach <id> [name] - attach existing pane
  /panes - show all WezTerm panes
  /list - list tracked sessions
  /switch <id> - switch active session
  /screen - capture current screen
  /kill <id> - kill session
  /key <key> - send key (ctrl+c, enter, up, tab...)
  /keys - list available keys
  /raw <text> - send text without pressing enter
  /wait <secs> - re-capture after delay
  /help - show this"""

# --- Forum commands ---

def fcmd_new(ctx: Ctx, args: str) -> str:
    name = args.strip()
    if not name:
        return "Usage: /new <name>"
    with state_lock:
        thread_id, pane_id, name = forum_create(name)
    reply(ctx.chat_id, f"Session '{name}' created (pane {pane_id})", thread_id)
    return ""

def fcmd_exec(ctx: Ctx, text: str) -> str:
    tid = ctx.thread_id
    if not tid or tid not in forum_sessions:
        return "This topic has no session. Use /attach <pane_id> or create a /new session."
    wez_send(forum_sessions[tid]["pane_id"], text + "\n")
    time.sleep(CAPTURE_DELAY)
    return forum_capture(tid, cmd_text=text)

def fcmd_screen(ctx: Ctx, _args: str) -> str:
    tid = ctx.thread_id
    if not tid or tid not in forum_sessions:
        return "No session in this topic."
    sess = forum_sessions[tid]
    text = wez_get(sess["pane_id"])
    sess["prev"] = text
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        lines = lines[-MAX_LINES:]
    return "\n".join(lines) or "(empty)"

def fcmd_kill(ctx: Ctx, _args: str) -> str:
    tid = ctx.thread_id
    if not tid or tid not in forum_sessions:
        return "No session in this topic."
    sess = forum_sessions[tid]
    pid = sess["pane_id"]
    with state_lock:
        pane_to_thread.pop(pid, None)
        forum_sessions.pop(tid, None)
    wez_kill(pid)
    close_topic(tid)
    return f"Killed '{sess['name']}' (pane {pid})"

def fcmd_attach(ctx: Ctx, args: str) -> str:
    tid = ctx.thread_id
    if not tid:
        return "Use this inside a topic."
    try:
        pane_id = int(args.strip())
    except (ValueError, AttributeError):
        return "Usage: /attach <pane_id>"
    if tid in forum_sessions:
        return f"This topic already has pane {forum_sessions[tid]['pane_id']}."
    with state_lock:
        forum_register(tid, pane_id, f"attached-{pane_id}")
    return f"Attached pane {pane_id} to this topic."

def fcmd_merge(ctx: Ctx, args: str) -> str:
    tid = ctx.thread_id
    if not tid:
        return "Use this inside a topic."
    try:
        source_pane = int(args.strip())
    except (ValueError, AttributeError):
        return "Usage: /merge <pane_id>"
    source_tid = pane_to_thread.get(source_pane)
    if source_tid is None:
        return f"Pane {source_pane} not found in any topic. Use /attach instead."
    with state_lock:
        sess = forum_sessions.pop(source_tid, None)
        pane_to_thread.pop(source_pane, None)
        if not sess:
            return f"Pane {source_pane} session not found."
        close_topic(source_tid)
        if tid in forum_sessions:
            old = forum_sessions[tid]
            wez_kill(old["pane_id"])
            pane_to_thread.pop(old["pane_id"], None)
        forum_register(tid, source_pane, sess["name"])
    return f"Merged pane {source_pane} ('{sess['name']}') into this topic."

def fcmd_key(ctx: Ctx, args: str) -> str:
    tid = ctx.thread_id
    if not tid or tid not in forum_sessions:
        return "No session in this topic."
    key = args.strip().lower()
    if key not in KEYS:
        return f"Unknown key '{key}'. Use /keys to list."
    wez_send(forum_sessions[tid]["pane_id"], KEYS[key], raw=True)
    return f"Sent {key}"

def fcmd_raw(ctx: Ctx, args: str) -> str:
    tid = ctx.thread_id
    if not tid or tid not in forum_sessions:
        return "No session in this topic."
    wez_send(forum_sessions[tid]["pane_id"], args, raw=True)
    time.sleep(0.5)
    return forum_capture(tid)

def fcmd_wait(ctx: Ctx, args: str) -> str:
    tid = ctx.thread_id
    if not tid or tid not in forum_sessions:
        return "No session in this topic."
    try:
        delay = float(args.strip())
    except (ValueError, AttributeError):
        delay = CAPTURE_DELAY
    time.sleep(min(delay, 30))
    return forum_capture(tid)

# --- DM commands ---

def dcmd_new(ctx: Ctx, args: str) -> str:
    pid, name = dm_create(ctx.chat_id, args.strip() or None)
    return f"Session '{name}' created (pane {pid})"

def dcmd_exec(ctx: Ctx, text: str) -> str:
    pid = dm_active_pane(ctx.chat_id)
    if pid is None:
        pid, name = dm_create(ctx.chat_id)
        ctx.reply(f"Auto-created session '{name}'")
        time.sleep(0.5)
    wez_send(pid, text + "\n")
    time.sleep(CAPTURE_DELAY)
    return dm_capture(ctx.chat_id, pid, cmd_text=text)

def dcmd_attach(ctx: Ctx, args: str) -> str:
    parts = args.strip().split(maxsplit=1)
    if not parts:
        return "Usage: /attach <pane_id> [name]"
    try:
        pid = int(parts[0])
    except ValueError:
        return "Usage: /attach <pane_id> [name]"
    s = dm_st(ctx.chat_id)
    if pid in s["sessions"]:
        return f"Pane {pid} already tracked."
    name = parts[1] if len(parts) > 1 else f"attached-{pid}"
    s["sessions"][pid] = {"pane_id": pid, "name": name, "prev": ""}
    s["active"] = pid
    return f"Attached to pane {pid} as '{name}'"

def dcmd_list(ctx: Ctx, _args: str) -> str:
    s = dm_state.get(ctx.chat_id)
    if not s or not s["sessions"]:
        return "No tracked sessions. Use /new or /attach."
    lines = []
    for pid, info in s["sessions"].items():
        marker = " *" if pid == s["active"] else ""
        lines.append(f"  [{pid}] {info['name']}{marker}")
    return "Tracked sessions:\n" + "\n".join(lines)

def dcmd_switch(ctx: Ctx, args: str) -> str:
    s = dm_state.get(ctx.chat_id)
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

def dcmd_kill(ctx: Ctx, args: str) -> str:
    s = dm_state.get(ctx.chat_id)
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

def dcmd_screen(ctx: Ctx, _args: str) -> str:
    pid = dm_active_pane(ctx.chat_id)
    if pid is None:
        return "No active session. Use /new."
    sess = dm_state[ctx.chat_id]["sessions"][pid]
    text = wez_get(pid)
    sess["prev"] = text
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        lines = lines[-MAX_LINES:]
    return "\n".join(lines) or "(empty)"

def dcmd_key(ctx: Ctx, args: str) -> str:
    pid = dm_active_pane(ctx.chat_id)
    if pid is None:
        return "No active session. Use /new."
    key = args.strip().lower()
    if key not in KEYS:
        return f"Unknown key '{key}'. Use /keys to list."
    wez_send(pid, KEYS[key], raw=True)
    return f"Sent {key}"

def dcmd_raw(ctx: Ctx, args: str) -> str:
    pid = dm_active_pane(ctx.chat_id)
    if pid is None:
        return "No active session. Use /new."
    wez_send(pid, args, raw=True)
    time.sleep(0.5)
    return dm_capture(ctx.chat_id, pid)

def dcmd_wait(ctx: Ctx, args: str) -> str:
    pid = dm_active_pane(ctx.chat_id)
    if pid is None:
        return "No active session. Use /new."
    try:
        delay = float(args.strip())
    except (ValueError, AttributeError):
        delay = CAPTURE_DELAY
    time.sleep(min(delay, 30))
    return dm_capture(ctx.chat_id, pid)

# --- Shared commands ---

def cmd_panes(_ctx: Ctx, _args: str) -> str:
    panes = wez_list_panes()
    if not panes:
        return "No WezTerm panes found."
    lines = []
    for p in panes:
        pid = p.get("pane_id", "?")
        title = p.get("title", "")
        cwd = p.get("cwd", "")
        if cwd:
            cwd = cwd.replace("file://", "").split("/")[-2:]
            cwd = "/".join(cwd)
        tracked = " [tracked]" if pid in pane_to_thread else ""
        lines.append(f"  [{pid}] {title}  ({cwd}){tracked}")
    return "All WezTerm panes:\n" + "\n".join(lines)

def cmd_keys(_ctx: Ctx, _args: str) -> str:
    return "Available keys:\n" + ", ".join(sorted(KEYS))

# Command tables
FORUM_COMMANDS = {
    "/start": lambda ctx, a: HELP_FORUM,
    "/help": lambda ctx, a: HELP_FORUM,
    "/new": fcmd_new,
    "/attach": fcmd_attach,
    "/panes": cmd_panes,
    "/screen": fcmd_screen,
    "/kill": fcmd_kill,
    "/merge": fcmd_merge,
    "/key": fcmd_key,
    "/keys": cmd_keys,
    "/raw": fcmd_raw,
    "/wait": fcmd_wait,
}

DM_COMMANDS = {
    "/start": lambda ctx, a: HELP_DM,
    "/help": lambda ctx, a: HELP_DM,
    "/new": dcmd_new,
    "/attach": dcmd_attach,
    "/panes": cmd_panes,
    "/list": dcmd_list,
    "/switch": dcmd_switch,
    "/kill": dcmd_kill,
    "/screen": dcmd_screen,
    "/key": dcmd_key,
    "/keys": cmd_keys,
    "/raw": dcmd_raw,
    "/wait": dcmd_wait,
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def handle(ctx: Ctx, text: str):
    if ALLOWED_USER_IDS and ctx.user_id not in ALLOWED_USER_IDS:
        ctx.reply(f"Unauthorized. Your user ID: {ctx.user_id}")
        return

    commands = FORUM_COMMANDS if ctx.forum else DM_COMMANDS
    exec_fn = fcmd_exec if ctx.forum else dcmd_exec

    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        args = parts[1] if len(parts) > 1 else ""
        handler = commands.get(cmd)
        if handler:
            result = handler(ctx, args)
            if result:
                ctx.reply(result)
        else:
            ctx.reply(f"Unknown command: {cmd}. Use /help")
    else:
        ctx.reply(exec_fn(ctx, text))

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
    if FORUM_GROUP_ID:
        print(f"Forum mode: group {FORUM_GROUP_ID}")
    else:
        print("DM mode (set TELEGRAM_GROUP_ID for forum mode)")
    if ALLOWED_USER_IDS:
        print(f"Allowed users: {ALLOWED_USER_IDS}")
    else:
        print("WARNING: No ALLOWED_USER_IDS — anyone can use this bot!")

    # Restore state and clean up stale mappings
    if FORUM_GROUP_ID:
        load_state()
        # Remove mappings for panes that no longer exist
        current_pids = {p["pane_id"] for p in wez_list_panes()}
        stale = [pid for pid in list(pane_to_thread.keys()) if pid not in current_pids]
        for pid in stale:
            forum_unregister_pane(pid)
        if stale:
            print(f"  cleaned up {len(stale)} stale pane mappings")

        monitor = threading.Thread(target=pane_monitor, daemon=True)
        monitor.start()

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
            thread_id = msg.get("message_thread_id")
            is_forum = (FORUM_GROUP_ID is not None and chat_id == FORUM_GROUP_ID)

            ctx = Ctx(chat_id, user_id, thread_id, is_forum)
            print(f"  chat={chat_id} user={user_id}{' thread=' + str(thread_id) if thread_id else ''} | {text[:80]}")
            try:
                handle(ctx, text)
            except Exception as e:
                print(f"  error: {e}")
                ctx.reply(f"Error: {e}")

if __name__ == "__main__":
    def _shutdown(signum, frame):
        print("\nSaving state...")
        with state_lock:
            save_state()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    try:
        main()
    except KeyboardInterrupt:
        with state_lock:
            save_state()
        print("\nStopped.")
