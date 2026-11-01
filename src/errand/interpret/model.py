"""The model interface: models turn user text into structured actions and can do nothing else.

No action type exists for hold, book, cancel, reschedule or approve, so a hijacked model has nothing to ask for.
A model may read a stop, a memory request or a worded answer, because a wrong one spends nothing. It can never approve:
the engine treats a worded yes to a proposal as a prompt to ask for a bare one.
"""

import json
from dataclasses import asdict, dataclass, field
from typing import Protocol

from errand.core.domain import InvalidModelOutput

INTENTS = ("book", "reschedule", "cancel")
DETAIL_KEYS = ("service", "area", "when", "budget", "earliest_time", "latest_time", "provider")
READ_TOOLS = ("list_bookings", "get_provider")
MEMORY_KEYS = ("area", "earliest_time", "preferred_technician")
ACTION_FIELDS = {
    "set_intent": {"intent"},
    "update_details": {"details"},
    "select_option": {"option"},
    "request_read": {"tool", "option"},
    "propose_memory": {"key", "value"},
    "need_clarification": set(),
    "stop": set(),
    "show_memory": set(),
    "forget_memory": {"key"},
    "correct_memory": {"key", "value"},
    "answer_question": {"answer"},
}


@dataclass
class ModelInput:
    message: str
    today: str
    state: str | None = None
    workflow: str | None = None
    details: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)
    # Only option number, price, rating and id reach a model: never reviews, attributes or names.
    options: list = field(default_factory=list)
    booking_choices: list = field(default_factory=list)
    pending_question: str | None = None
    read_results: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ModelReply:
    raw: object
    # Which model produced this, so a trace shows the route a router took.
    served_by: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    compute_seconds: float | None = None


class Model(Protocol):
    name: str

    def decide(self, model_input: ModelInput, timeout_seconds: float) -> ModelReply: ...


@dataclass
class ModelOutput:
    actions: list[dict]
    done: bool


def without_answer_beside_a_change(actions: list[dict]) -> list[dict]:
    """"Yes, but make it after 11" can come back as both a yes and the change. The change is what the person means."""
    return actions if len(actions) == 1 else [action for action in actions if action["type"] != "answer_question"]


def parse_output(raw: object) -> ModelOutput:
    """Validates shape at the boundary. Whether an action fits the current state is checked by the workflow."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as error:
            raise InvalidModelOutput(f"not JSON: {error}") from error
    if not isinstance(raw, dict):
        raise InvalidModelOutput(f"expected an object, got {type(raw).__name__}")
    unknown = set(raw) - {"actions", "done"}
    if unknown:
        raise InvalidModelOutput(f"unknown top-level fields: {sorted(unknown)}")
    actions = raw.get("actions")
    if not isinstance(actions, list):
        raise InvalidModelOutput("actions must be a list")
    for action in actions:
        _validate_action(action)
    done = raw.get("done", True)
    if not isinstance(done, bool):
        raise InvalidModelOutput("done must be true or false")
    return ModelOutput(actions=without_answer_beside_a_change(actions), done=done)


def _validate_action(action: object) -> None:
    if not isinstance(action, dict) or action.get("type") not in ACTION_FIELDS:
        raise InvalidModelOutput(f"unknown action: {action!r}")
    kind = action["type"]
    extra = set(action) - {"type"} - ACTION_FIELDS[kind]
    if extra:
        raise InvalidModelOutput(f"{kind} has unknown fields: {sorted(extra)}")
    if kind == "set_intent" and action.get("intent") not in INTENTS:
        raise InvalidModelOutput(f"unknown intent: {action.get('intent')!r}")
    if kind == "update_details":
        details = action.get("details")
        if not isinstance(details, dict) or set(details) - set(DETAIL_KEYS):
            raise InvalidModelOutput(f"bad details: {details!r}")
    if kind == "select_option" and not (isinstance(action.get("option"), int) and not isinstance(action.get("option"), bool)):
        raise InvalidModelOutput(f"option must be a number: {action.get('option')!r}")
    if kind == "request_read" and action.get("tool") not in READ_TOOLS:
        raise InvalidModelOutput(f"tool not readable by a model: {action.get('tool')!r}")
    if kind in ("propose_memory", "correct_memory") and (action.get("key") not in MEMORY_KEYS or not isinstance(action.get("value"), str)):
        raise InvalidModelOutput(f"bad memory proposal: {action!r}")
    if kind == "forget_memory" and action.get("key") not in MEMORY_KEYS:
        raise InvalidModelOutput(f"bad memory key: {action!r}")
    if kind == "answer_question" and action.get("answer") not in ("yes", "no"):
        raise InvalidModelOutput(f"bad answer: {action!r}")
