"""Compares a model's output with a label, using the engine's own parsing rules so both sides mean the same thing."""

from datetime import date

from errand.core.domain import InvalidModelOutput
from errand.core.memory import InvalidMemoryValue, normalise_value
from errand.core.when import normalise_time, resolve_when
from errand.interpret.model import parse_output


def _detail(key: str, value, today: date):
    if key == "when":
        return resolve_when(value, today)
    if key in ("earliest_time", "latest_time"):
        return normalise_time(value)
    return value


def _memory_value(key: str, value: str) -> str:
    return normalise_value(key, value).lower()


def _repeats(key: str, value, known: dict) -> bool:
    """The engine applies only changed details, so repeating a value the run holds has no effect."""
    return known.get(key) == (normalise_time(value) if key in ("earliest_time", "latest_time") else value)


def canonical(actions: list[dict], context: dict, today: date) -> dict:
    """Reduces actions to what they mean. A clarification asks the user again, the same as no action."""
    workflow, known = context.get("workflow"), context.get("details", {})
    found = {"intent": None, "details": {}, "select": None, "read": None, "memory": None}
    for action in actions:
        kind = action["type"]
        if kind == "set_intent" and action["intent"] != workflow:
            found["intent"] = action["intent"]
        elif kind == "update_details":
            found["details"].update({key: _detail(key, value, today) for key, value in action["details"].items() if not _repeats(key, value, known)})
        elif kind == "select_option":
            found["select"] = action["option"]
        elif kind == "request_read":
            found["read"] = action["option"]
        elif kind == "propose_memory":
            found["memory"] = (action["key"], _memory_value(action["key"], action["value"]))
    return found


def _expected(label: dict, today: date) -> dict:
    return {
        "intent": label.get("intent"),
        "details": {key: _detail(key, value, today) for key, value in label.get("details", {}).items()},
        "select": label.get("select"),
        "read": label.get("read"),
        "memory": label.get("memory"),
    }


def score(raw: str, turn: dict, today: date) -> dict:
    """Returns whether the output is exactly right, and whether it stated a detail the label does not have.

    An invented or wrong detail is scored apart because it is the silent failure: the run carries on with a value
    the user never gave, while a missing detail only makes the engine ask again.
    """
    wanted = _expected(turn["expected"], today)
    try:
        got = canonical(parse_output(raw).actions, turn["context"], today)
    except (InvalidModelOutput, InvalidMemoryValue, ValueError) as error:
        return {"correct": False, "wrong_value": False, "rejected": str(error), "got": None}
    wrong_details = {key: value for key, value in got["details"].items() if key not in wanted["details"] or wanted["details"][key] != value}
    wrong_other = [field for field in ("intent", "select", "memory") if got[field] is not None and got[field] != wanted[field]]
    return {
        "correct": got == wanted,
        "wrong_value": bool(wrong_details or wrong_other),
        "rejected": None,
        "got": {key: value for key, value in got.items() if value not in (None, {})},
    }
