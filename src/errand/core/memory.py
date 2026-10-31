"""Stable preferences across sessions, each marked stated or inferred."""

import re

from errand.contract import AREAS, SERVICES
from errand.core.approval import reopen
from errand.core.domain import Run, State
from errand.core.when import normalise_time

# Which run detail each memory fact fills in.
DETAIL_FOR_KEY = {"area": "area", "earliest_time": "earliest_time", "preferred_technician": "provider_id"}
_KEEP_WORDS = re.compile(r"\b(always|remember|from now on|every time)\b", re.IGNORECASE)
_MEMORY_SOURCE = "memory_stated"


class InvalidMemoryValue(ValueError):
    pass


def normalise_value(key: str, value: str) -> str:
    cleaned = value.strip()
    if key == "area":
        cleaned = cleaned.lower()
        if cleaned not in AREAS:
            raise InvalidMemoryValue(f"unknown area: {value!r}")
    if key == "earliest_time":
        try:
            return normalise_time(cleaned)
        except ValueError as error:
            raise InvalidMemoryValue(str(error)) from error
    return cleaned


def _said_by_user(key: str, value: str, text: str) -> bool:
    """A model may only suggest a value that appears in what the user actually typed."""
    if key == "earliest_time":
        hour = int(value[:2])
        return re.search(rf"\b0?({hour}|{hour % 12 or 12})\b", text) is not None
    return value.lower() in text.lower()


class Memory:
    def __init__(self, store):
        self.store = store

    def view(self, user_id: str) -> list[dict]:
        return self.store.memory_facts(user_id)

    def state(self, turn, run: Run | None, key: str, value: str, evidence: str) -> str:
        """The user said it, so it is stated. It replaces any earlier value and lifts a forget marker."""
        normalised = normalise_value(key, value)
        self.store.save_memory_fact(turn.user_id, key, normalised, "stated", "active", evidence)
        turn.tracer.event("memory", key=key, provenance="stated", status="active")
        if run is not None:
            self._update_active_run(turn, run, key, normalised)
        return normalised

    def forget(self, turn, run: Run | None, key: str) -> None:
        self.store.save_memory_fact(turn.user_id, key, None, None, "forgotten", None)
        turn.tracer.event("memory", key=key, status="forgotten")
        if run is not None:
            question = run.pending_question
            if question and question["kind"] == "memory_confirm" and question["key"] == key:
                run.pending_question = None
                self.store.save_run(run)
            self._clear_from_active_run(turn, run, key)

    def infer(self, turn, key: str, value: str, evidence: str) -> None:
        """Only from a one-off request or booking history. Never over a stated fact or a forget marker."""
        existing = self.store.memory_fact(turn.user_id, key)
        if existing is not None and (existing["status"] in ("forgotten", "rejected") or existing["provenance"] == "stated"):
            return
        if existing is not None and existing["value"] == value:
            return
        self.store.save_memory_fact(turn.user_id, key, value, "inferred", "active", evidence)
        turn.tracer.event("memory", key=key, provenance="inferred", status="active", evidence=evidence)
        turn.say(f"Noted: your {key.replace('_', ' ')} might be {value}. I will ask before using it, and you can say 'forget my {key.replace('_', ' ')}'.")

    def propose_from_model(self, turn, run: Run | None, key: str, value: str) -> None:
        """A model can only suggest. Code decides whether the user's own words make the fact stated."""
        normalised = normalise_value(key, value)
        if not _said_by_user(key, normalised, turn.text):
            raise InvalidMemoryValue(f"value is not in the user's message: {value!r}")
        if _KEEP_WORDS.search(turn.text):
            self.state(turn, run, key, value, evidence="user message")
            turn.say(f"I will remember your {key.replace('_', ' ')}: {normalised}.")
        else:
            self.infer(turn, key, normalised, "user message")

    def confirm(self, turn, key: str) -> None:
        fact = self.store.memory_fact(turn.user_id, key)
        self.store.save_memory_fact(turn.user_id, key, fact["value"], "stated", "active", "user confirmed")
        turn.tracer.event("memory", key=key, provenance="stated", status="active", evidence="user confirmed")
        turn.say(f"Okay, I have kept your {key.replace('_', ' ')}: {fact['value']}.")

    def reject(self, turn, key: str) -> None:
        self.store.save_memory_fact(turn.user_id, key, None, None, "rejected", None)
        turn.tracer.event("memory", key=key, status="rejected")
        turn.say(f"Okay, I will not keep that {key.replace('_', ' ')}.")

    def apply_to_run(self, turn, run: Run) -> bool:
        """Fills empty details from stated facts. Returns True if it asked about an inferred fact instead."""
        for fact in self.store.memory_facts(turn.user_id):
            if fact["status"] != "active" or not self._relevant(run, fact["key"], fact["value"]):
                continue
            detail = DETAIL_FOR_KEY[fact["key"]]
            if run.detail(detail) is not None:
                continue
            if fact["provenance"] == "stated":
                run.details[detail] = {"value": fact["value"], "source": _MEMORY_SOURCE}
                turn.say(f"Using your saved {fact['key'].replace('_', ' ')}: {fact['value']}.")
                turn.tracer.event("memory_used", key=fact["key"], provenance="stated")
            elif fact["key"] not in run.asked_inferred:
                run.asked_inferred.append(fact["key"])
                run.pending_question = {"kind": "memory_confirm", "key": fact["key"]}
                turn.say(f"From an earlier errand, your {fact['key'].replace('_', ' ')} might be {fact['value']}. Should I use and keep that? Reply yes or no.")
                return True
        return False

    def infer_after_booking(self, run: Run, turn) -> None:
        earliest = run.details.get("earliest_time")
        if earliest and earliest["source"] == "user_this_run":
            self.infer(turn, "earliest_time", earliest["value"], f"one-off request in {run.id}")
        if run.selected and run.detail("provider_id") is None:
            self.infer(turn, "preferred_technician", run.selected["provider_id"], f"booking history in {run.id}")

    @staticmethod
    def _relevant(run: Run, key: str, value: str) -> bool:
        if key == "preferred_technician":
            service = run.detail("service")
            if run.workflow != "book" or service is None:
                return False
            # A technician known to belong to another service is skipped. A plain name is tried, and the search reports if it finds nobody.
            lowered = value.lower()
            other_services = [s for s in SERVICES if s != service]
            return not lowered.startswith(tuple(other_services) + tuple(s.replace("_", " ") for s in other_services))
        if key == "earliest_time":
            return run.workflow in ("book", "reschedule")
        return run.workflow in ("book", "reschedule", "cancel")

    def _update_active_run(self, turn, run: Run, key: str, value: str) -> None:
        detail = DETAIL_FOR_KEY[key]
        entry = run.details.get(detail)
        if entry is None:
            if not self._relevant(run, key, value):
                return
            run.details[detail] = {"value": value, "source": _MEMORY_SOURCE}
        elif entry["source"] != _MEMORY_SOURCE:
            return
        else:
            entry["value"] = value
        self._void_if_proposed(turn, run, "memory value changed")

    def _clear_from_active_run(self, turn, run: Run, key: str) -> None:
        detail = DETAIL_FOR_KEY[key]
        if detail not in run.details:
            return
        del run.details[detail]
        if self.store.open_attempt(run.id) is not None:
            run.forgotten_in_flight = True
            self.store.save_run(run)
            return
        self._void_if_proposed(turn, run, "memory value forgotten")

    def _void_if_proposed(self, turn, run: Run, reason: str) -> None:
        if self.store.open_attempt(run.id) is not None:
            return
        if run.state in (State.PRESENTING, State.AWAITING_APPROVAL, State.HOLDING):
            reopen(self.store, turn.tracer, run, reason)
        self.store.save_run(run)
