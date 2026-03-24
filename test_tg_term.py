"""Comprehensive tests for tg-term.

Mocks all external dependencies (Telegram API, WezTerm CLI) so no real
network or subprocess calls are made.
"""

import importlib
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Import the module (hyphenated filename requires importlib)
# ---------------------------------------------------------------------------

# Ensure the module dir is importable
_mod_dir = str(Path(__file__).parent)
if _mod_dir not in sys.path:
    sys.path.insert(0, _mod_dir)

# Set required env before import so module-level code doesn't fail
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-123")

# Patch requests and subprocess at import time to prevent real calls during module load
with patch("requests.post"):
    with patch("subprocess.run"):
        tg_term = importlib.import_module("tg-term")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_state():
    """Clear all mutable global state before each test."""
    tg_term.forum_sessions.clear()
    tg_term.dm_state.clear()
    tg_term.pane_to_thread.clear()
    tg_term._color_idx = 0
    # Reset module-level config to test defaults
    tg_term.ALLOWED_USER_IDS = set()
    tg_term.FORUM_GROUP_ID = None
    tg_term.MACHINE_NAME = "testmachine"
    yield


@pytest.fixture
def mock_subprocess():
    """Mock subprocess.run to simulate WezTerm CLI."""
    pane_ids = itertools.count(start=42)

    def fake_run(cmd, *, input=None, capture_output=True, text=True, timeout=10):
        subcmd = cmd[2] if len(cmd) > 2 else ""
        if subcmd == "spawn":
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{next(pane_ids)}\n", stderr="")
        elif subcmd == "get-text":
            return subprocess.CompletedProcess(cmd, 0, stdout="$ echo hello\nhello\n$ \n", stderr="")
        elif subcmd in ("send-text", "kill-pane", "set-tab-title"):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        elif subcmd == "list":
            data = [{"pane_id": 1, "tab_id": 0, "title": "zsh", "cwd": "file:///Users/test/cs/pr"}]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(data) + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch.object(tg_term.subprocess, "run", side_effect=fake_run) as mock:
        yield mock


@pytest.fixture
def mock_requests():
    """Mock requests.post to simulate Telegram API."""
    def fake_post(url, *, json=None, timeout=None):
        resp = MagicMock()
        if "createForumTopic" in url:
            resp.json.return_value = {"ok": True, "result": {"message_thread_id": 999}}
        elif "closeForumTopic" in url:
            resp.json.return_value = {"ok": True}
        elif "sendMessage" in url:
            resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
        elif "getMe" in url:
            resp.json.return_value = {"ok": True, "result": {"username": "testbot"}}
        elif "getUpdates" in url:
            resp.json.return_value = {"ok": True, "result": []}
        else:
            resp.json.return_value = {"ok": True}
        return resp

    with patch.object(tg_term.requests, "post", side_effect=fake_post) as mock:
        yield mock


@pytest.fixture
def mock_sleep():
    """No-op time.sleep."""
    with patch.object(tg_term.time, "sleep"):
        yield


@pytest.fixture
def mock_helpers(mock_subprocess, mock_requests, mock_sleep):
    """Convenience: all external deps mocked."""
    return {"subprocess": mock_subprocess, "requests": mock_requests}


def make_dm_ctx(chat_id=100, user_id=1):
    return tg_term.Ctx(chat_id, user_id, None, False)


def make_forum_ctx(chat_id=-100, user_id=1, thread_id=50):
    return tg_term.Ctx(chat_id, user_id, thread_id, True)


# ═══════════════════════════════════════════════════════════════════════════
# A. Session State Structure — THE BUG regression tests
# ═══════════════════════════════════════════════════════════════════════════

class TestSessionStructure:
    """Every session dict MUST have pane_id, name, prev keys."""

    def test_dm_create_has_pane_id(self, mock_helpers):
        pid, name = tg_term.dm_create(100, "test")
        sess = tg_term.dm_state[100]["sessions"][pid]
        assert "pane_id" in sess
        assert sess["pane_id"] == pid

    def test_dm_create_has_all_keys(self, mock_helpers):
        pid, _ = tg_term.dm_create(100)
        sess = tg_term.dm_state[100]["sessions"][pid]
        assert set(sess.keys()) >= {"pane_id", "name", "prev"}
        assert sess["prev"] == ""

    def test_dm_attach_has_pane_id(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_attach(ctx, "99 myname")
        sess = tg_term.dm_state[100]["sessions"][99]
        assert "pane_id" in sess
        assert sess["pane_id"] == 99

    def test_forum_create_has_pane_id(self, mock_helpers):
        tg_term.FORUM_GROUP_ID = -100
        tid, pid, name = tg_term.forum_create("test")
        sess = tg_term.forum_sessions[tid]
        assert "pane_id" in sess
        assert sess["pane_id"] == pid

    def test_forum_create_has_machine(self, mock_helpers):
        tg_term.FORUM_GROUP_ID = -100
        tid, _, _ = tg_term.forum_create("test")
        sess = tg_term.forum_sessions[tid]
        assert "machine" in sess
        assert sess["machine"] == "testmachine"

    def test_forum_attach_has_pane_id(self, mock_helpers):
        ctx = make_forum_ctx(thread_id=50)
        tg_term.fcmd_attach(ctx, "77")
        sess = tg_term.forum_sessions[50]
        assert "pane_id" in sess
        assert sess["pane_id"] == 77

    def test_capture_accesses_pane_id(self, mock_helpers):
        """The actual bug: _capture(sess) must access sess['pane_id'] without error."""
        pid, _ = tg_term.dm_create(100, "test")
        sess = tg_term.dm_state[100]["sessions"][pid]
        # This should NOT raise KeyError
        result = tg_term._capture(sess)
        assert isinstance(result, str)

    def test_dm_exec_full_flow_no_keyerror(self, mock_helpers):
        """End-to-end: sending a command in DM mode must not raise."""
        ctx = make_dm_ctx()
        result = tg_term.dcmd_exec(ctx, "echo hello")
        assert isinstance(result, str)

    def test_forum_exec_full_flow_no_keyerror(self, mock_helpers):
        """End-to-end: sending a command in forum mode must not raise."""
        tg_term.FORUM_GROUP_ID = -100
        tid, pid, _ = tg_term.forum_create("test")
        ctx = make_forum_ctx(thread_id=tid)
        result = tg_term.fcmd_exec(ctx, "echo hello")
        assert isinstance(result, str)


# ═══════════════════════════════════════════════════════════════════════════
# B. DM Command Handlers
# ═══════════════════════════════════════════════════════════════════════════

class TestDMCommands:

    def test_new_creates_session(self, mock_helpers):
        ctx = make_dm_ctx()
        result = tg_term.dcmd_new(ctx, "myterm")
        assert "myterm" in result
        assert "created" in result
        assert len(tg_term.dm_state[100]["sessions"]) == 1

    def test_new_auto_names(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "")
        sess = tg_term.dm_state[100]["sessions"]
        names = [s["name"] for s in sess.values()]
        assert names[0] == "tg-0"

    def test_new_sets_active(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "test")
        assert tg_term.dm_state[100]["active"] is not None

    def test_exec_auto_creates_on_first_message(self, mock_helpers):
        ctx = make_dm_ctx()
        result = tg_term.dcmd_exec(ctx, "ls")
        assert len(tg_term.dm_state[100]["sessions"]) == 1

    def test_exec_sends_command_with_newline(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "test")
        tg_term.dcmd_exec(ctx, "echo hi")
        # Verify send-text was called (via subprocess)
        calls = mock_helpers["subprocess"].call_args_list
        send_calls = [c for c in calls if len(c[0][0]) > 2 and c[0][0][2] == "send-text"]
        assert len(send_calls) > 0

    def test_attach_tracks_pane(self, mock_helpers):
        ctx = make_dm_ctx()
        result = tg_term.dcmd_attach(ctx, "55 myserver")
        assert "Attached" in result
        assert 55 in tg_term.dm_state[100]["sessions"]
        assert tg_term.dm_state[100]["active"] == 55

    def test_attach_already_tracked(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_attach(ctx, "55")
        result = tg_term.dcmd_attach(ctx, "55")
        assert "already tracked" in result

    def test_attach_empty_args(self, mock_helpers):
        ctx = make_dm_ctx()
        result = tg_term.dcmd_attach(ctx, "")
        assert "Usage" in result

    def test_attach_invalid_id(self, mock_helpers):
        ctx = make_dm_ctx()
        result = tg_term.dcmd_attach(ctx, "notanumber")
        assert "Usage" in result

    def test_list_shows_sessions(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "first")
        tg_term.dcmd_new(ctx, "second")
        result = tg_term.dcmd_list(ctx, "")
        assert "first" in result
        assert "second" in result
        assert "*" in result  # active marker

    def test_list_no_sessions(self, mock_helpers):
        ctx = make_dm_ctx()
        result = tg_term.dcmd_list(ctx, "")
        assert "No tracked sessions" in result

    def test_switch_changes_active(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "first")
        pid1 = tg_term.dm_state[100]["active"]
        tg_term.dcmd_new(ctx, "second")
        pid2 = tg_term.dm_state[100]["active"]
        assert pid1 != pid2
        tg_term.dcmd_switch(ctx, str(pid1))
        assert tg_term.dm_state[100]["active"] == pid1

    def test_switch_invalid_id(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "test")
        result = tg_term.dcmd_switch(ctx, "abc")
        assert "Usage" in result

    def test_switch_unknown_pane(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "test")
        result = tg_term.dcmd_switch(ctx, "9999")
        assert "Unknown" in result

    def test_switch_no_sessions(self, mock_helpers):
        ctx = make_dm_ctx()
        result = tg_term.dcmd_switch(ctx, "1")
        assert "No sessions" in result

    def test_kill_removes_session(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "test")
        pid = tg_term.dm_state[100]["active"]
        result = tg_term.dcmd_kill(ctx, str(pid))
        assert "Killed" in result
        assert pid not in tg_term.dm_state[100]["sessions"]

    def test_kill_active_switches_to_next(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "first")
        pid1 = tg_term.dm_state[100]["active"]
        tg_term.dcmd_new(ctx, "second")
        pid2 = tg_term.dm_state[100]["active"]
        tg_term.dcmd_kill(ctx, str(pid2))
        assert tg_term.dm_state[100]["active"] == pid1

    def test_kill_last_session_active_none(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "only")
        pid = tg_term.dm_state[100]["active"]
        tg_term.dcmd_kill(ctx, str(pid))
        assert tg_term.dm_state[100]["active"] is None

    def test_kill_unknown_pane(self, mock_helpers):
        ctx = make_dm_ctx()
        result = tg_term.dcmd_kill(ctx, "9999")
        assert "No sessions" in result or "Unknown" in result

    def test_screen_returns_text(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "test")
        result = tg_term.dcmd_screen(ctx, "")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_screen_no_session(self, mock_helpers):
        ctx = make_dm_ctx()
        result = tg_term.dcmd_screen(ctx, "")
        assert "No active session" in result

    def test_key_sends_ctrl_c(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "test")
        result = tg_term.dcmd_key(ctx, "ctrl+c")
        assert "Sent ctrl+c" in result

    def test_key_unknown(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "test")
        result = tg_term.dcmd_key(ctx, "ctrl+q")
        assert "Unknown key" in result

    def test_key_no_session(self, mock_helpers):
        ctx = make_dm_ctx()
        result = tg_term.dcmd_key(ctx, "ctrl+c")
        assert "No active session" in result

    def test_raw_sends_without_newline(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "test")
        tg_term.dcmd_raw(ctx, "hello")
        calls = mock_helpers["subprocess"].call_args_list
        send_calls = [c for c in calls if len(c[0][0]) > 2 and c[0][0][2] == "send-text"]
        # Last send-text should have --no-paste
        last_send = send_calls[-1]
        assert "--no-paste" in last_send[0][0]

    def test_raw_no_session(self, mock_helpers):
        ctx = make_dm_ctx()
        result = tg_term.dcmd_raw(ctx, "hello")
        assert "No active session" in result

    def test_wait_recaptures(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "test")
        result = tg_term.dcmd_wait(ctx, "1")
        assert isinstance(result, str)

    def test_wait_caps_at_30(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "test")
        tg_term.dcmd_wait(ctx, "999")
        sleep_calls = tg_term.time.sleep.call_args_list
        # The last sleep should be capped at 30
        assert sleep_calls[-1] == call(30)

    def test_wait_no_session(self, mock_helpers):
        ctx = make_dm_ctx()
        result = tg_term.dcmd_wait(ctx, "1")
        assert "No active session" in result


# ═══════════════════════════════════════════════════════════════════════════
# C. Forum Command Handlers
# ═══════════════════════════════════════════════════════════════════════════

class TestForumCommands:

    def test_new_creates_topic_and_pane(self, mock_helpers):
        tg_term.FORUM_GROUP_ID = -100
        ctx = make_forum_ctx()
        result = tg_term.fcmd_new(ctx, "myproject")
        # fcmd_new returns "" because it replies directly to the new topic
        assert result == ""
        assert len(tg_term.forum_sessions) == 1

    def test_new_empty_name(self, mock_helpers):
        ctx = make_forum_ctx()
        result = tg_term.fcmd_new(ctx, "")
        assert "Usage" in result

    def test_new_topic_name_has_machine(self, mock_helpers):
        tg_term.FORUM_GROUP_ID = -100
        tg_term.MACHINE_NAME = "MyMac"
        ctx = make_forum_ctx()
        tg_term.fcmd_new(ctx, "dev")
        # Check createForumTopic was called with correct name
        calls = mock_helpers["requests"].call_args_list
        topic_calls = [c for c in calls if "createForumTopic" in str(c)]
        assert len(topic_calls) == 1
        body = topic_calls[0][1]["json"]
        assert body["name"] == "[MyMac] dev"

    def test_exec_sends_to_pane(self, mock_helpers):
        tg_term.FORUM_GROUP_ID = -100
        tid, pid, _ = tg_term.forum_create("test")
        ctx = make_forum_ctx(thread_id=tid)
        result = tg_term.fcmd_exec(ctx, "ls -la")
        assert isinstance(result, str)

    def test_exec_no_session_in_topic(self, mock_helpers):
        ctx = make_forum_ctx(thread_id=777)
        result = tg_term.fcmd_exec(ctx, "ls")
        assert "no session" in result.lower()

    def test_exec_no_thread_id(self, mock_helpers):
        ctx = make_forum_ctx(thread_id=None)
        result = tg_term.fcmd_exec(ctx, "ls")
        assert "no session" in result.lower()

    def test_kill_removes_and_closes_topic(self, mock_helpers):
        tg_term.FORUM_GROUP_ID = -100
        tid, pid, _ = tg_term.forum_create("test")
        ctx = make_forum_ctx(thread_id=tid)
        result = tg_term.fcmd_kill(ctx, "")
        assert "Killed" in result
        assert tid not in tg_term.forum_sessions
        # Verify closeForumTopic was called
        calls = mock_helpers["requests"].call_args_list
        close_calls = [c for c in calls if "closeForumTopic" in str(c)]
        assert len(close_calls) == 1

    def test_kill_no_session(self, mock_helpers):
        ctx = make_forum_ctx(thread_id=777)
        result = tg_term.fcmd_kill(ctx, "")
        assert "No session" in result

    def test_attach_adds_pane_to_topic(self, mock_helpers):
        ctx = make_forum_ctx(thread_id=50)
        result = tg_term.fcmd_attach(ctx, "88")
        assert "Attached" in result
        assert 50 in tg_term.forum_sessions
        assert tg_term.forum_sessions[50]["pane_id"] == 88

    def test_attach_topic_already_has_pane(self, mock_helpers):
        tg_term.forum_sessions[50] = {"pane_id": 1, "name": "x", "prev": ""}
        ctx = make_forum_ctx(thread_id=50)
        result = tg_term.fcmd_attach(ctx, "88")
        assert "already has pane" in result

    def test_attach_no_thread_id(self, mock_helpers):
        ctx = make_forum_ctx(thread_id=None)
        result = tg_term.fcmd_attach(ctx, "88")
        assert "inside a topic" in result

    def test_merge_moves_pane(self, mock_helpers):
        tg_term.FORUM_GROUP_ID = -100
        tg_term.forum_register(10, 42, "source")
        ctx = make_forum_ctx(thread_id=50)
        result = tg_term.fcmd_merge(ctx, "42")
        assert "Merged" in result
        assert 10 not in tg_term.forum_sessions
        assert tg_term.forum_sessions[50]["pane_id"] == 42

    def test_merge_replaces_existing(self, mock_helpers):
        tg_term.FORUM_GROUP_ID = -100
        tg_term.forum_register(10, 42, "source")
        tg_term.forum_register(50, 99, "old")
        ctx = make_forum_ctx(thread_id=50)
        result = tg_term.fcmd_merge(ctx, "42")
        assert "Merged" in result
        assert tg_term.forum_sessions[50]["pane_id"] == 42

    def test_merge_pane_not_found(self, mock_helpers):
        ctx = make_forum_ctx(thread_id=50)
        result = tg_term.fcmd_merge(ctx, "9999")
        assert "not found" in result

    def test_screen_returns_text(self, mock_helpers):
        tg_term.FORUM_GROUP_ID = -100
        tid, _, _ = tg_term.forum_create("test")
        ctx = make_forum_ctx(thread_id=tid)
        result = tg_term.fcmd_screen(ctx, "")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_key_sends_key(self, mock_helpers):
        tg_term.forum_sessions[50] = {"pane_id": 1, "name": "test", "prev": ""}
        ctx = make_forum_ctx(thread_id=50)
        result = tg_term.fcmd_key(ctx, "enter")
        assert "Sent enter" in result


# ═══════════════════════════════════════════════════════════════════════════
# D. Message Routing
# ═══════════════════════════════════════════════════════════════════════════

class TestRouting:

    def test_unauthorized_user_rejected(self, mock_helpers):
        tg_term.ALLOWED_USER_IDS = {999}
        ctx = make_dm_ctx(user_id=1)
        with patch.object(tg_term, "reply") as mock_reply:
            tg_term.handle(ctx, "hello")
            mock_reply.assert_called_once()
            assert "Unauthorized" in mock_reply.call_args[0][1]

    def test_authorized_user_proceeds(self, mock_helpers):
        tg_term.ALLOWED_USER_IDS = {1}
        ctx = make_dm_ctx(user_id=1)
        # Should not raise, should process command
        tg_term.handle(ctx, "/help")

    def test_empty_allowlist_permits_all(self, mock_helpers):
        tg_term.ALLOWED_USER_IDS = set()
        ctx = make_dm_ctx(user_id=12345)
        # Should not raise
        tg_term.handle(ctx, "/help")

    def test_forum_routes_to_forum_commands(self, mock_helpers):
        tg_term.FORUM_GROUP_ID = -100
        ctx = make_forum_ctx()
        with patch.object(tg_term, "reply") as mock_reply:
            tg_term.handle(ctx, "/help")
            assert "Forum Mode" in mock_reply.call_args[0][1]

    def test_dm_routes_to_dm_commands(self, mock_helpers):
        ctx = make_dm_ctx()
        with patch.object(tg_term, "reply") as mock_reply:
            tg_term.handle(ctx, "/help")
            assert "DM Mode" in mock_reply.call_args[0][1]

    def test_unknown_command(self, mock_helpers):
        ctx = make_dm_ctx()
        with patch.object(tg_term, "reply") as mock_reply:
            tg_term.handle(ctx, "/nonexistent")
            assert "Unknown command" in mock_reply.call_args[0][1]

    def test_strips_bot_mention(self, mock_helpers):
        ctx = make_dm_ctx()
        with patch.object(tg_term, "reply") as mock_reply:
            tg_term.handle(ctx, "/help@mybot")
            assert "DM Mode" in mock_reply.call_args[0][1]

    def test_command_case_insensitive(self, mock_helpers):
        ctx = make_dm_ctx()
        with patch.object(tg_term, "reply") as mock_reply:
            tg_term.handle(ctx, "/HELP")
            assert "DM Mode" in mock_reply.call_args[0][1]

    def test_plain_text_executes_command(self, mock_helpers):
        ctx = make_dm_ctx()
        tg_term.handle(ctx, "echo hello")
        # Should have created a session
        assert len(tg_term.dm_state[100]["sessions"]) == 1

    def test_empty_handler_result_no_reply(self, mock_helpers):
        """fcmd_new returns '' — handle() must not send an empty reply."""
        tg_term.FORUM_GROUP_ID = -100
        ctx = make_forum_ctx()
        with patch.object(tg_term, "reply") as mock_reply:
            tg_term.handle(ctx, "/new testproject")
            # reply IS called (by fcmd_new directly to the new topic),
            # but handle() should not call ctx.reply with ""
            for c in mock_reply.call_args_list:
                assert c[0][1] != ""  # no empty replies


# ═══════════════════════════════════════════════════════════════════════════
# E. Output Capture & Truncation
# ═══════════════════════════════════════════════════════════════════════════

class TestCapture:

    def test_truncate_short_text(self):
        assert tg_term.truncate("hello\nworld") == "hello\nworld"

    def test_truncate_strips_blank_lines(self):
        result = tg_term.truncate("a\n\n\nb\n  \nc")
        assert result == "a\nb\nc"

    def test_truncate_max_lines(self):
        text = "\n".join(f"line{i}" for i in range(200))
        result = tg_term.truncate(text)
        lines = result.splitlines()
        assert len(lines) <= tg_term.MAX_LINES

    def test_truncate_max_chars(self):
        text = "\n".join("x" * 100 for _ in range(100))
        result = tg_term.truncate(text)
        assert len(result) <= tg_term.MAX_CHARS + 20  # +20 for truncation prefix

    def test_capture_first_call_returns_all(self, mock_helpers):
        pid, _ = tg_term.dm_create(100, "test")
        sess = tg_term.dm_state[100]["sessions"][pid]
        assert sess["prev"] == ""
        result = tg_term._capture(sess)
        assert len(result) > 0
        assert sess["prev"] != ""

    def test_capture_updates_prev(self, mock_helpers):
        pid, _ = tg_term.dm_create(100, "test")
        sess = tg_term.dm_state[100]["sessions"][pid]
        tg_term._capture(sess)
        prev_after = sess["prev"]
        assert prev_after != ""

    def test_capture_no_new_output(self, mock_helpers):
        pid, _ = tg_term.dm_create(100, "test")
        sess = tg_term.dm_state[100]["sessions"][pid]
        # First capture
        tg_term._capture(sess)
        # Second capture (same output) should show no new output
        result = tg_term._capture(sess)
        assert result == "(no new output)"


# ═══════════════════════════════════════════════════════════════════════════
# F. Telegram & WezTerm Helpers
# ═══════════════════════════════════════════════════════════════════════════

class TestTelegramHelpers:

    def test_tg_posts_to_correct_url(self, mock_requests):
        tg_term.tg("sendMessage", chat_id=1, text="hi")
        url = mock_requests.call_args[0][0]
        assert "sendMessage" in url
        assert tg_term.BOT_TOKEN in url or "test-token" in url

    def test_tg_returns_json(self, mock_requests):
        result = tg_term.tg("getMe")
        assert result["ok"] is True

    def test_tg_handles_exception(self):
        with patch.object(tg_term.requests, "post", side_effect=tg_term.requests.RequestException("fail")):
            result = tg_term.tg("getMe")
            assert result == {"ok": False}

    def test_reply_sends_message(self, mock_requests):
        tg_term.reply(123, "hello")
        assert mock_requests.called
        body = mock_requests.call_args[1]["json"]
        assert body["chat_id"] == 123
        assert body["text"] == "hello"

    def test_reply_includes_thread_id(self, mock_requests):
        tg_term.reply(123, "hello", thread_id=456)
        body = mock_requests.call_args[1]["json"]
        assert body["message_thread_id"] == 456

    def test_reply_no_thread_id(self, mock_requests):
        tg_term.reply(123, "hello")
        body = mock_requests.call_args[1]["json"]
        assert "message_thread_id" not in body

    def test_reply_empty_text_becomes_no_output(self, mock_requests):
        tg_term.reply(123, "")
        body = mock_requests.call_args[1]["json"]
        assert body["text"] == "(no output)"

    def test_reply_chunks_long_text(self, mock_requests):
        long_text = "x" * (tg_term.MAX_CHARS * 3)
        tg_term.reply(123, long_text)
        assert mock_requests.call_count >= 3

    def test_create_topic_returns_thread_id(self, mock_requests):
        tg_term.FORUM_GROUP_ID = -100
        tid = tg_term.create_topic("test")
        assert tid == 999

    def test_create_topic_returns_none_without_group(self, mock_requests):
        tg_term.FORUM_GROUP_ID = None
        tid = tg_term.create_topic("test")
        assert tid is None

    def test_next_topic_color_cycles(self):
        colors = [tg_term.next_topic_color() for _ in range(len(tg_term.TOPIC_COLORS) + 2)]
        assert colors[0] == colors[len(tg_term.TOPIC_COLORS)]


class TestWezTermHelpers:

    def test_wez_spawn_returns_int(self, mock_subprocess):
        pid = tg_term.wez_spawn("test")
        assert isinstance(pid, int)

    def test_wez_spawn_sets_tab_title(self, mock_subprocess):
        tg_term.wez_spawn("myname")
        calls = mock_subprocess.call_args_list
        title_calls = [c for c in calls if len(c[0][0]) > 2 and c[0][0][2] == "set-tab-title"]
        assert len(title_calls) == 1

    def test_wez_spawn_no_name_skips_title(self, mock_subprocess):
        tg_term.wez_spawn()
        calls = mock_subprocess.call_args_list
        title_calls = [c for c in calls if len(c[0][0]) > 2 and c[0][0][2] == "set-tab-title"]
        assert len(title_calls) == 0

    def test_wez_send_uses_pane_id(self, mock_subprocess):
        tg_term.wez_send(42, "hello")
        cmd = mock_subprocess.call_args[0][0]
        assert "--pane-id" in cmd
        assert "42" in cmd

    def test_wez_send_raw_adds_no_paste(self, mock_subprocess):
        tg_term.wez_send(42, "\x03", raw=True)
        cmd = mock_subprocess.call_args[0][0]
        assert "--no-paste" in cmd

    def test_wez_send_normal_no_no_paste(self, mock_subprocess):
        tg_term.wez_send(42, "hello")
        cmd = mock_subprocess.call_args[0][0]
        assert "--no-paste" not in cmd

    def test_wez_get_returns_text(self, mock_subprocess):
        text = tg_term.wez_get(42)
        assert isinstance(text, str)
        assert len(text) > 0

    def test_wez_list_panes_parses_json(self, mock_subprocess):
        panes = tg_term.wez_list_panes()
        assert isinstance(panes, list)
        assert len(panes) == 1
        assert panes[0]["pane_id"] == 1

    def test_wez_list_panes_empty(self, mock_subprocess):
        mock_subprocess.side_effect = None
        mock_subprocess.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        panes = tg_term.wez_list_panes()
        assert panes == []

    def test_wez_list_panes_invalid_json(self, mock_subprocess):
        mock_subprocess.side_effect = None
        mock_subprocess.return_value = subprocess.CompletedProcess([], 0, stdout="not json", stderr="")
        panes = tg_term.wez_list_panes()
        assert panes == []


# ═══════════════════════════════════════════════════════════════════════════
# G. Config & Ctx
# ═══════════════════════════════════════════════════════════════════════════

class TestCtx:

    def test_session_key_dm(self):
        ctx = make_dm_ctx(chat_id=100)
        assert ctx.session_key == 100

    def test_session_key_forum(self):
        ctx = make_forum_ctx(chat_id=-100, thread_id=50)
        assert ctx.session_key == 50

    def test_session_key_forum_no_thread(self):
        ctx = tg_term.Ctx(-100, 1, None, True)
        assert ctx.session_key == -100

    def test_ctx_reply_delegates(self, mock_requests):
        ctx = make_dm_ctx(chat_id=100)
        ctx.reply("hello")
        body = mock_requests.call_args[1]["json"]
        assert body["chat_id"] == 100

    def test_ctx_reply_with_thread(self, mock_requests):
        ctx = make_forum_ctx(chat_id=-100, thread_id=50)
        ctx.reply("hello")
        body = mock_requests.call_args[1]["json"]
        assert body["message_thread_id"] == 50


class TestSharedCommands:

    def test_panes_lists_panes(self, mock_helpers):
        ctx = make_dm_ctx()
        result = tg_term.cmd_panes(ctx, "")
        assert "[1]" in result
        assert "zsh" in result

    def test_panes_no_panes(self, mock_helpers):
        # Override to return empty
        mock_helpers["subprocess"].side_effect = None
        mock_helpers["subprocess"].return_value = subprocess.CompletedProcess(
            [], 0, stdout="", stderr=""
        )
        ctx = make_dm_ctx()
        result = tg_term.cmd_panes(ctx, "")
        assert "No WezTerm panes" in result

    def test_keys_lists_all_keys(self, mock_helpers):
        ctx = make_dm_ctx()
        result = tg_term.cmd_keys(ctx, "")
        assert "ctrl+c" in result
        assert "enter" in result

    def test_poll_returns_results(self, mock_requests):
        results = tg_term.poll(None)
        assert isinstance(results, list)

    def test_poll_with_offset(self, mock_requests):
        tg_term.poll(42)
        body = mock_requests.call_args[1]["json"]
        assert body["offset"] == 42

    def test_poll_handles_error(self, mock_sleep):
        with patch.object(tg_term.requests, "post", side_effect=tg_term.requests.RequestException):
            results = tg_term.poll(None)
            assert results == []


# ═══════════════════════════════════════════════════════════════════════════
# H. Pane Monitor & Reverse Mapping
# ═══════════════════════════════════════════════════════════════════════════

class TestPaneMonitor:

    def test_forum_register_creates_reverse_mapping(self, mock_helpers):
        tg_term.forum_register(100, 42, "test")
        assert tg_term.pane_to_thread[42] == 100
        assert tg_term.forum_sessions[100]["pane_id"] == 42

    def test_forum_unregister_removes_both(self, mock_helpers):
        tg_term.forum_register(100, 42, "test")
        tg_term.forum_unregister_pane(42)
        assert 42 not in tg_term.pane_to_thread
        assert 100 not in tg_term.forum_sessions

    def test_forum_create_registers_both(self, mock_helpers):
        tg_term.FORUM_GROUP_ID = -100
        tid, pid, name = tg_term.forum_create("test")
        assert pid in tg_term.pane_to_thread
        assert tg_term.pane_to_thread[pid] == tid

    def test_fcmd_kill_cleans_reverse_mapping(self, mock_helpers):
        tg_term.FORUM_GROUP_ID = -100
        tid, pid, _ = tg_term.forum_create("test")
        ctx = make_forum_ctx(thread_id=tid)
        tg_term.fcmd_kill(ctx, "")
        assert pid not in tg_term.pane_to_thread
        assert tid not in tg_term.forum_sessions

    def test_fcmd_merge_updates_reverse_mapping(self, mock_helpers):
        tg_term.FORUM_GROUP_ID = -100
        tg_term.forum_register(10, 42, "source")
        ctx = make_forum_ctx(thread_id=50)
        tg_term.fcmd_merge(ctx, "42")
        assert tg_term.pane_to_thread[42] == 50
        assert 10 not in tg_term.forum_sessions


# ═══════════════════════════════════════════════════════════════════════════
# I. Command Echo Stripping
# ═══════════════════════════════════════════════════════════════════════════

class TestCommandStripping:

    def test_capture_strips_echoed_command(self, mock_helpers):
        """Output should NOT contain the command the user typed."""
        sess = {"pane_id": 1, "name": "t", "prev": ""}
        # First capture (prev empty) with command echo in output
        with patch.object(tg_term, "wez_get", return_value="$ echo hello\nhello\n$ "):
            result = tg_term._capture(sess, cmd_text="echo hello")
            assert "hello" in result
            assert "echo hello" not in result

    def test_capture_without_cmd_text_keeps_all(self, mock_helpers):
        sess = {"pane_id": 1, "name": "t", "prev": ""}
        with patch.object(tg_term, "wez_get", return_value="$ echo hello\nhello\n$ "):
            result = tg_term._capture(sess, cmd_text=None)
            assert "echo hello" in result

    def test_dcmd_exec_strips_echo(self, mock_helpers):
        """End-to-end: DM exec should not echo the command."""
        ctx = make_dm_ctx()
        tg_term.dcmd_new(ctx, "test")
        pid = tg_term.dm_state[100]["active"]
        sess = tg_term.dm_state[100]["sessions"][pid]
        # Simulate first command (prev empty, so full output returned)
        sess["prev"] = ""
        with patch.object(tg_term, "wez_get", return_value="$ ls\nfile.txt\ndir/\n$ "):
            result = tg_term.dcmd_exec(ctx, "ls")
            assert "file.txt" in result
            assert "$ ls" not in result

    def test_fcmd_exec_strips_echo(self, mock_helpers):
        """End-to-end: Forum exec should not echo the command."""
        tg_term.FORUM_GROUP_ID = -100
        tid, pid, _ = tg_term.forum_create("test")
        sess = tg_term.forum_sessions[tid]
        sess["prev"] = ""
        ctx = make_forum_ctx(thread_id=tid)
        with patch.object(tg_term, "wez_get", return_value="$ pwd\n/home/user\n$ "):
            result = tg_term.fcmd_exec(ctx, "pwd")
            assert "/home/user" in result
            assert "$ pwd" not in result

    def test_stripping_only_removes_matching_lines(self, mock_helpers):
        """Lines that don't contain the command should be preserved."""
        sess = {"pane_id": 1, "name": "t", "prev": ""}
        with patch.object(tg_term, "wez_get", return_value="$ git status\nOn branch main\nnothing to commit\n$ "):
            result = tg_term._capture(sess, cmd_text="git status")
            assert "On branch main" in result
            assert "nothing to commit" in result
            assert "git status" not in result
