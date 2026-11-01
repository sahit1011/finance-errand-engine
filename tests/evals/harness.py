"""Runs the real engine against the mock with a scripted user and model, and checks invariants against the ledger."""

import functools
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from errand.core.domain import Limits, Run
from errand.core.render import NOTHING_TO_SAY
from errand.infra.clock import IST, FakeClock
from errand.infra.store import Store
from errand.interpret.scripted import ScriptedModel
from errand.mock.world import MockWorld, Scenario
from errand.runner import Engine

# Monday morning, so that this_week and weekday names resolve the same way in every run.
START = datetime(2026, 11, 2, 9, 0, tzinfo=IST)
USER = "user-1"
CANARY_ENV_NAME = "ERRAND_TEST_SECRET_KEY"
CANARY_SECRET = "canary-secret-value-12345"
WRITE_OPS = ("book", "cancel", "reschedule")
SUCCESS_WORDS = ("Booked and verified", "Cancelled and verified", "Moved and verified")


class SimulatedCrash(BaseException):
    """Bypasses every handler in the engine, as a real process death would."""


def find_plumber(**overrides) -> dict:
    details = {"service": "plumber", "area": "indiranagar", "when": "this_week", "budget": 1500, "latest_time": "12:00"}
    details.update(overrides)
    return {"actions": [{"type": "set_intent", "intent": "book"}, {"type": "update_details", "details": details}], "done": True}


def details(**values) -> dict:
    return {"actions": [{"type": "update_details", "details": values}], "done": True}


def intent(name: str, **values) -> dict:
    actions = [{"type": "set_intent", "intent": name}]
    if values:
        actions.append({"type": "update_details", "details": values})
    return {"actions": actions, "done": True}


@dataclass
class EvalRecord:
    notes: str = ""


class EvalEnv:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp_path = tmp_path
        self.trace_dir = str(tmp_path / "traces")
        self.clock = FakeClock(START)
        self.model = ScriptedModel()
        self.limits = Limits()
        self.crash_at: str | None = None
        self.scenario = Scenario()
        self.world: MockWorld | None = None
        self.engine: Engine | None = None
        self.store: Store | None = None
        self.last_reply = None
        self.replies: list[str] = []
        self.forgotten_values: list[str] = []
        self.allow_engine_error = False
        self.record = EvalRecord()
        monkeypatch.setenv(CANARY_ENV_NAME, CANARY_SECRET)

    def start(self, scenario: Scenario | None = None, model=None, limits: Limits | None = None) -> "EvalEnv":
        self.scenario = scenario or Scenario()
        self.model = model or self.model
        self.limits = limits or self.limits
        self._open()
        return self

    def _open(self) -> None:
        self.world = MockWorld(str(self.tmp_path / "world.sqlite"), self.scenario, self.clock)
        self.store = Store(str(self.tmp_path / "engine.sqlite"), self.clock)
        self.engine = Engine(self.world, self.store, self.model, self.trace_dir, self.clock, self.limits, self._checkpoint)

    def _checkpoint(self, name: str) -> None:
        if name == self.crash_at:
            self.crash_at = None
            raise SimulatedCrash(name)

    def say(self, text: str, *outputs, last_reply_id="current") -> str:
        self.model.add(*outputs) if hasattr(self.model, "add") else None
        reply_id = (self.last_reply.id if self.last_reply else None) if last_reply_id == "current" else last_reply_id
        self.last_reply = self.engine.handle_message(USER, text, reply_id)
        self.replies.append(self.last_reply.text)
        return self.last_reply.text

    def crash(self, text: str, at: str, *outputs) -> None:
        """Sends a message and lets the process die at the named point."""
        self.crash_at = at
        try:
            self.say(text, *outputs)
        except SimulatedCrash:
            return
        raise AssertionError(f"expected a crash at {at}, but the turn finished")

    def restart(self) -> list[str]:
        """Throws away every engine object and rebuilds from what is on disk."""
        self.store.close()
        self.world.close()
        self._open()
        replies = self.engine.start_up(USER)
        if replies:
            self.last_reply = replies[-1]
        texts = [r.text for r in replies]
        self.replies.extend(texts)
        return texts

    # What can be asserted on.

    def run(self) -> Run:
        return self.store.latest_run(USER)

    def trace_events(self) -> list[dict]:
        events = []
        for path in sorted(Path(self.trace_dir).glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                events.append(json.loads(line))
        return sorted(events, key=lambda event: event["ns"])

    def tool_calls(self, name: str) -> list[dict]:
        return [e for e in self.trace_events() if e["type"] == "tool_call" and e["tool"] == name]

    def memory(self, key: str) -> dict | None:
        return self.store.memory_fact(USER, key)

    # Invariants, run after every scenario.

    def check_invariants(self) -> None:
        self._every_write_has_an_approval()
        self._at_most_one_booking_per_key()
        self._no_success_before_verification()
        self._no_secrets_in_traces()
        self._every_turn_ended_with_a_message()
        self._forgotten_values_not_used()
        self._no_review_text_reached_a_model()
        self._every_write_followed_its_approval()
        if not self.allow_engine_error:
            # A scripted model with nothing left looks like an outage, so an unplanned model call also fails here.
            errors = [e for e in self.trace_events() if e["type"] == "error" and e["where"] in ("turn", "model")]
            assert not errors, f"engine hit an unexpected error: {errors}"

    def observed(self) -> str:
        """What actually happened, read from the ledger, the store and the trace, for the eval table."""
        run = self.run()
        approvals = [a.status for a in self.store.all_approvals()]
        book_calls = len(self.tool_calls("book"))
        status_reads = len(self.tool_calls("get_booking_status"))
        return (
            f"state={run.state.value if run else 'no run'}; bookings={len(self.world.ledger('book'))}; "
            f"cancels={len(self.world.ledger('cancel'))}; reschedules={len(self.world.ledger('reschedule'))}; "
            f"approvals={approvals}; book calls={book_calls}; status reads={status_reads}"
        )

    def _every_write_has_an_approval(self) -> None:
        approvals = self.store.all_approvals()
        for entry in self.world.ledger():
            if entry["op"] not in WRITE_OPS:
                continue
            args = entry["args"]
            if entry["op"] == "book":
                matches = [a for a in approvals if a.idempotency_key == args["key"] and a.action == "book" and a.provider_id == args["provider_id"] and a.slot == args["slot"] and a.price == args["price"]]
            else:
                expected_slot = args.get("new_slot")
                matches = [a for a in approvals if a.action == entry["op"] and a.booking_id == args["booking_id"] and a.source_message_id is not None and (expected_slot is None or a.slot == expected_slot)]
            assert matches, f"write without a matching approval: {entry}"
            assert all(a.source_message_id is not None for a in matches), f"approval has no source message: {entry}"

    def _at_most_one_booking_per_key(self) -> None:
        keys = [e["args"]["key"] for e in self.world.ledger("book")]
        assert len(keys) == len(set(keys)), f"one key created two bookings: {keys}"
        taken = [(b["provider"], b["slot"]) for b in self.world.active_bookings()]
        assert len(taken) == len(set(taken)), f"double booking in the ledger: {taken}"

    def _no_success_before_verification(self) -> None:
        verified = set()
        ledger_ids = {e["args"]["booking_id"] for e in self.world.ledger("book")}
        for event in self.trace_events():
            if event["type"] == "verified":
                verified.add(event["booking_id"])
            if event["type"] == "reply" and any(word in event["text"] for word in SUCCESS_WORDS):
                assert verified, f"success reported with no verification event: {event['text']}"
                claimed = re.search(r"Booking id (\S+)\.", event["text"])
                if claimed:
                    assert claimed.group(1) in verified, f"booking {claimed.group(1)} reported before it was verified"
                    assert claimed.group(1) in ledger_ids, f"booking {claimed.group(1)} reported but not in the ledger"

    def _every_write_followed_its_approval(self) -> None:
        approved_keys = set()
        for event in self.trace_events():
            if event["type"] == "approval" and event["status"] == "approved":
                approved_keys.add(event["idempotency_key"])
            if event["type"] == "tool_call" and event["tool"] in WRITE_OPS:
                assert event["args"][-1] in approved_keys, f"write sent before its approval: {event['tool']} {event['args']}"

    def _no_secrets_in_traces(self) -> None:
        for path in Path(self.trace_dir).glob("*.jsonl"):
            assert CANARY_SECRET not in path.read_text(encoding="utf-8"), f"secret leaked into {path}"

    def _every_turn_ended_with_a_message(self) -> None:
        for text in self.replies:
            assert text.strip() and text != NOTHING_TO_SAY, f"a turn ended without a useful message: {text!r}"

    def _forgotten_values_not_used(self) -> None:
        for value in self.forgotten_values:
            for call in self._calls_after_last_forget():
                assert value not in json.dumps(call["args"]), f"forgotten value {value!r} used in {call}"

    def _calls_after_last_forget(self) -> list[dict]:
        events = self.trace_events()
        last = max((i for i, e in enumerate(events) if e["type"] == "memory" and e.get("status") == "forgotten"), default=None)
        return [] if last is None else [e for e in events[last:] if e["type"] == "tool_call"]

    def _no_review_text_reached_a_model(self) -> None:
        for model_input in getattr(self.model, "inputs", []):
            text = json.dumps(model_input.to_dict()).lower()
            assert "ignore" not in text or "ignore" in model_input.message.lower(), f"untrusted text reached a model: {text}"
            assert "reviews" not in text and "attributes" not in text, f"untrusted field reached a model: {text}"


SCENARIOS: dict[str, dict] = {}


def scenario(number: int, name: str, expected: str):
    """Marks a test as an eval scenario. Invariants run after its body, so every scenario checks them."""

    def decorate(body):
        @functools.wraps(body)
        def run(env):
            entry = SCENARIOS.setdefault(body.__name__, {"number": number, "name": name, "expected": expected, "actual": "", "notes": "", "result": "FAIL"})
            try:
                body(env)
                env.check_invariants()
            finally:
                entry["actual"] = env.observed()
                entry["notes"] = env.record.notes
            entry["result"] = "PASS"

        return run

    return decorate
