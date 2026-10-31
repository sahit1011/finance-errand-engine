"""Measured results and saved runs for the Results and Replay pages, read from the files the repo already generates."""

import json
import re
from pathlib import Path

EVALS_FILE = "eval_results.json"
ROUTING_FILE = "benchmarks/routing/results.json"
BUGS_FILE = "BUGS.md"
# Real runs through errand chat with Qwen3.5 9B, saved in the repo.
REPLAYS = (
    {"id": "timeout-after-success", "title": "Timeout after success", "summary": "book times out after it really booked. The status check finds the booking, so there is one booking and no retry. A hijack attempt in the chat does nothing.", "path": "docs/trace-timeout-after-success.jsonl"},
    {"id": "slot-taken", "title": "Slot taken after approval", "summary": "The provider gives the slot away after the user approved. The approval is voided, and a new slot needs a new approval.", "path": "docs/traces/slot-taken.jsonl"},
    {"id": "reschedule", "title": "Reschedule a booking", "summary": "A booking moves to a new slot, with its own approval, and the move is verified before it is reported.", "path": "docs/traces/reschedule.jsonl"},
    {"id": "cancel-503", "title": "Cancel with a transient failure", "summary": "The first cancel fails with a 503. The engine retries with the same key, then verifies the cancellation.", "path": "docs/traces/cancel-503.jsonl"},
    {"id": "stop-in-hindi", "title": "Stop in Hindi", "summary": "A whole sentence in Hindi is read as a stop. The proposal is voided and nothing is booked.", "path": "docs/traces/stop-in-hindi.jsonl"},
)
_BUG_FIELDS = {"what_broke": "What broke", "why": "Why", "fix": "Fix", "regression": "Regression test"}


def _read_bugs(repo: Path) -> list[dict]:
    sections = re.split(r"^## (\d+)\. (.+)$", (repo / BUGS_FILE).read_text(encoding="utf-8"), flags=re.M)[1:]
    bugs = []
    for number, title, body in zip(sections[0::3], sections[1::3], sections[2::3]):
        bug = {"number": int(number), "title": title.strip()}
        for key, label in _BUG_FIELDS.items():
            match = re.search(rf"^- \*\*{label}s?:\*\* (.+)$", body, flags=re.M)
            if match is None:
                raise ValueError(f"bug {number} in {BUGS_FILE} has no {label!r} line")
            bug[key] = match.group(1).strip()
        bugs.append(bug)
    return bugs


def load_results(repo: Path) -> dict:
    return {
        "evals": json.loads((repo / EVALS_FILE).read_text(encoding="utf-8")),
        "routing": json.loads((repo / ROUTING_FILE).read_text(encoding="utf-8")),
        "bugs": _read_bugs(repo),
    }


def list_replays() -> dict:
    return {"replays": [{key: replay[key] for key in ("id", "title", "summary")} for replay in REPLAYS]}


def load_replay(repo: Path, replay_id: str) -> dict | None:
    replay = next((r for r in REPLAYS if r["id"] == replay_id), None)
    if replay is None:
        return None
    lines = (repo / replay["path"]).read_text(encoding="utf-8").splitlines()
    events = sorted((json.loads(line) for line in lines), key=lambda event: event["ns"])
    return {"id": replay_id, "events": [{**event, "seq": index + 1} for index, event in enumerate(events)]}
