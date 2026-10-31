"""`errand chat` through its real entry point, in a real process, with a stand-in model server."""

import json
import os
import pty
import select
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_llama_model import StandInServer

ENTRY = "from errand.cli import main; raise SystemExit(main())"
READ_TIMEOUT_SECONDS = 20
FIND_PLUMBER = {
    "actions": [
        {"type": "set_intent", "intent": "book"},
        {"type": "update_details", "details": {"service": "plumber", "area": "indiranagar", "when": "next_week", "budget": "no_limit"}},
    ],
    "done": True,
}


def answering_with(output: dict) -> dict:
    return {
        "choices": [{"message": {"content": json.dumps(output)}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        "timings": {"prompt_ms": 1.0, "predicted_ms": 1.0},
    }


@pytest.fixture
def server():
    stand_in = StandInServer()
    stand_in.body = answering_with(FIND_PLUMBER)
    yield stand_in
    stand_in.close()


def chat(cwd: Path, url: str, lines: list[str], *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", ENTRY, "chat", "--model-url", url, *flags],
        input="".join(line + "\n" for line in lines),
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=READ_TIMEOUT_SECONDS,
    )


def world_ledger(cwd: Path, session: str, op: str) -> list[dict]:
    db = sqlite3.connect(cwd / ".errand" / session / "world.sqlite")
    rows = db.execute("SELECT args FROM ledger WHERE op = ?", (op,)).fetchall()
    db.close()
    return [json.loads(row[0]) for row in rows]


def test_a_full_booking_through_chat_writes_one_booking_and_a_trace(tmp_path, server):
    result = chat(tmp_path, server.url, ["Find a plumber next week in Indiranagar", "1", "yes"], "--book-outcome", "success")
    assert result.returncode == 0, result.stderr
    assert "Please confirm exactly this" in result.stdout
    assert "Booked and verified" in result.stdout
    assert len(world_ledger(tmp_path, "default", "book")) == 1
    trace = (tmp_path / "traces" / "default" / "run-1.jsonl").read_text()
    assert '"type": "model_call"' in trace and '"tool": "book"' in trace


def test_memory_survives_a_new_process_in_the_same_session(tmp_path, server):
    chat(tmp_path, server.url, ["remember my area is whitefield"])
    result = chat(tmp_path, server.url, ["what do you remember about me"])
    assert "whitefield (stated)" in result.stdout


def test_sessions_keep_state_and_traces_apart(tmp_path, server):
    chat(tmp_path, server.url, ["remember my area is whitefield", "Find a plumber next week in Indiranagar"], "--session", "one")
    result = chat(tmp_path, server.url, ["what do you remember about me", "Find a plumber next week in Indiranagar"], "--session", "two")
    assert "whitefield" not in result.stdout
    assert (tmp_path / ".errand" / "one").is_dir() and (tmp_path / ".errand" / "two").is_dir()
    for session in ("one", "two"):
        assert '"type": "model_call"' in (tmp_path / "traces" / session / "run-1.jsonl").read_text()
    assert list((tmp_path / "traces").glob("*.jsonl")) == []


def test_commands_that_need_no_terminal_control_import_without_it(tmp_path):
    """termios exists only on macOS and Linux, and only chat needs it."""
    result = subprocess.run([sys.executable, "-c", "import sys; sys.modules['termios'] = None; import errand.cli"], capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr


def test_a_forced_failure_runs_for_real_through_chat(tmp_path, server):
    result = chat(tmp_path, server.url, ["Find a plumber next week in Indiranagar", "1", "yes"], "--book-outcome", "timeout_booked")
    assert result.returncode == 0, result.stderr
    assert "Booked and verified" in result.stdout
    assert len(world_ledger(tmp_path, "default", "book")) == 1
    events = [json.loads(line) for line in (tmp_path / "traces" / "default" / "run-1.jsonl").read_text().splitlines()]
    calls = [e for e in events if e["type"] == "tool_call"][-2:]
    assert [c["tool"] for c in calls] == ["book", "get_booking_status"]
    assert calls[0]["error"] == "timeout"


def test_a_forced_outcome_is_refused_on_an_existing_session_rather_than_silently_ignored(tmp_path, server):
    chat(tmp_path, server.url, ["remember my area is whitefield"])
    result = chat(tmp_path, server.url, ["yes"], "--book-outcome", "timeout_booked")
    assert result.returncode != 0
    assert "--book-outcome" in result.stderr and "default" in result.stderr


def test_a_host_off_this_machine_stops_the_chat_before_it_starts(tmp_path):
    result = chat(tmp_path, "http://example.com:8080", ["hello"])
    assert result.returncode != 0
    assert "this machine" in result.stderr
    assert "agent>" not in result.stdout


def test_a_model_server_that_is_down_stops_the_chat_with_the_command_to_start_it(tmp_path):
    stand_in = StandInServer()
    stand_in.close()
    result = chat(tmp_path, stand_in.url, ["hello"])
    assert result.returncode != 0
    assert "llama-server" in result.stderr


def test_a_model_that_goes_down_mid_chat_keeps_the_run_and_code_handled_replies_still_work(tmp_path, server):
    process = subprocess.Popen(
        [sys.executable, "-c", ENTRY, "chat", "--model-url", server.url, "--book-outcome", "success"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=tmp_path,
    )
    process.stdin.write("Find a plumber next week in Indiranagar\n")
    process.stdin.flush()
    for line in process.stdout:
        if "Reply with its number" in line:
            break
    else:
        pytest.fail("the chat ended before showing options")
    server.status, server.body = 500, {"error": "boom"}
    process.stdin.write("tell me something the model must read\n1\nyes\n")
    process.stdin.close()
    output = process.stdout.read()
    assert process.wait(READ_TIMEOUT_SECONDS) == 0
    process.stdout.close()
    process.stderr.close()
    assert "local model" in output
    assert "Booked and verified" in output
    assert len(world_ledger(tmp_path, "default", "book")) == 1


class Terminal:
    """Reads a pty, keeping whatever arrived past the text waited for so the next wait can still find it."""

    def __init__(self, fd: int):
        self.fd = fd
        self.unread = ""

    def read_until(self, wanted: str) -> str:
        deadline = time.monotonic() + READ_TIMEOUT_SECONDS
        while wanted not in self.unread:
            assert time.monotonic() < deadline, f"never saw {wanted!r} in {self.unread!r}"
            if select.select([self.fd], [], [], 0.1)[0]:
                self.unread += os.read(self.fd, 4096).decode()
        end = self.unread.index(wanted) + len(wanted)
        seen, self.unread = self.unread[:end], self.unread[end:]
        return seen


def test_a_yes_typed_before_the_proposal_appeared_cannot_approve_it(tmp_path, server):
    """A real terminal, with a real proposal on screen by the time the queued yes would be read."""
    leader, follower = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-c", ENTRY, "chat", "--model-url", server.url, "--book-outcome", "success"],
        stdin=follower,
        stdout=follower,
        stderr=follower,
        cwd=tmp_path,
    )
    os.close(follower)
    terminal = Terminal(leader)
    try:
        terminal.read_until("you> ")
        os.write(leader, b"Find a plumber next week in Indiranagar\n")
        terminal.read_until("Reply with its number")
        terminal.read_until("you> ")
        server.body = answering_with({"actions": [{"type": "select_option", "option": 1}], "done": True})
        server.hang.set()
        calls_before = len(server.requests)
        os.write(leader, b"the first one please\n")
        deadline = time.monotonic() + READ_TIMEOUT_SECONDS
        while len(server.requests) == calls_before:
            assert time.monotonic() < deadline, "the model was never called"
            time.sleep(0.01)
        os.write(leader, b"yes\n")
        server.hang.clear()
        server.release.set()
        seen = terminal.read_until("Reply yes to confirm")
        seen += terminal.read_until("you> ")
        assert "typed while I was working" in seen
        assert world_ledger(tmp_path, "default", "book") == []
        os.write(leader, b"yes\n")
        terminal.read_until("Booked and verified")
        assert len(world_ledger(tmp_path, "default", "book")) == 1
    finally:
        process.terminate()
        process.wait(READ_TIMEOUT_SECONDS)
        os.close(leader)
