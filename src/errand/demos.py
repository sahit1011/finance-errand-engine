"""Offline demos: real engine and mock, with a scripted model and scripted user, so no model install is needed."""

import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from errand.infra.clock import IST, FakeClock
from errand.infra.store import Store
from errand.interpret.scripted import ScriptedModel
from errand.mock.world import OUTCOME_SUCCESS, OUTCOME_TIMEOUT_BOOKED, MockWorld, Scenario
from errand.runner import Engine

# A fixed Monday morning, so every demo run prints the same thing.
DEMO_START = datetime(2026, 11, 2, 9, 0, tzinfo=IST)
USER = "user-1"

_FIND_PLUMBER = {
    "actions": [
        {"type": "set_intent", "intent": "book"},
        {"type": "update_details", "details": {"service": "plumber", "area": "indiranagar", "when": "this_week", "budget": 1500, "latest_time": "12:00"}},
    ],
    "done": True,
}


@dataclass
class Demo:
    title: str
    scenario: Scenario
    # Each turn is (what the user types, what the scripted model returns for it, if the model is reached).
    turns: list[tuple[str, list]] = field(default_factory=list)


DEMOS = {
    "happy_path": Demo(
        "One full booking",
        Scenario(book_outcomes=[OUTCOME_SUCCESS]),
        [
            ("Find a plumber for my kitchen tap this week, mornings only, in Indiranagar, under Rs 1500.", [_FIND_PLUMBER]),
            ("1", []),
            ("yes", []),
            ("yes", []),
        ],
    ),
    "timeout_after_success": Demo(
        "book times out, but the booking actually happened",
        Scenario(book_outcomes=[OUTCOME_TIMEOUT_BOOKED]),
        [
            ("Find a plumber for my kitchen tap this week, mornings only, in Indiranagar, under Rs 1500.", [_FIND_PLUMBER]),
            ("1", []),
            ("yes", []),
        ],
    ),
    "injection": Demo(
        "A review tells the agent to book, and a hijacked model tries to obey",
        Scenario(book_outcomes=[OUTCOME_SUCCESS]),
        [
            ("Find a plumber for my kitchen tap this week, mornings only, in Indiranagar, under Rs 1500.", [_FIND_PLUMBER]),
            (
                "Tell me about option 1",
                [
                    {"actions": [{"type": "request_read", "tool": "get_provider", "option": 1}], "done": False},
                    {"actions": [{"type": "book", "provider": "plumber-indiranagar-1"}], "done": False},
                    {"actions": [{"type": "select_option", "option": 99}], "done": True},
                ],
            ),
        ],
    ),
}


def run_demo(name: str, trace_root: str, out=print) -> int:
    demo = DEMOS[name]
    # Each run starts from an empty database, so run ids repeat. A fresh folder keeps one trace per run.
    trace_path = Path(trace_root) / f"demo-{name}"
    trace_path.mkdir(parents=True, exist_ok=True)
    for old_trace in trace_path.glob("*.jsonl"):
        old_trace.unlink()
    trace_dir = str(trace_path)
    clock = FakeClock(DEMO_START)
    model = ScriptedModel()
    with tempfile.TemporaryDirectory() as data_dir:
        world = MockWorld(str(Path(data_dir) / "world.sqlite"), demo.scenario, clock)
        store = Store(str(Path(data_dir) / "engine.sqlite"), clock)
        engine = Engine(world, store, model, trace_dir, clock)
        out(f"Demo: {demo.title}")
        out(f"Traces are written to {trace_dir}/\n")
        last_reply = None
        for text, outputs in demo.turns:
            model.add(*outputs)
            out(f"you> {text}")
            last_reply = engine.handle_message(USER, text, last_reply.id if last_reply else None)
            out(f"agent> {last_reply.text}\n")
            clock.advance(seconds=30)
        out(f"Provider ledger, the ground truth: {len(world.ledger('book'))} booking(s) created.")
        store.close()
        world.close()
    return 0
