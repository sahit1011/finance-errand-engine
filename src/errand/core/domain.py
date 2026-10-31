"""States, the transition table, limits, and the records the engine works with."""

from dataclasses import dataclass, field
from enum import StrEnum


class State(StrEnum):
    COLLECTING = "COLLECTING"
    SEARCHING = "SEARCHING"
    PRESENTING = "PRESENTING"
    HOLDING = "HOLDING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    UNKNOWN_NEEDS_CHECK = "UNKNOWN_NEEDS_CHECK"
    DONE = "DONE"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"


TERMINAL_STATES = {State.DONE, State.FAILED, State.ABANDONED}

# docs/diagrams/03-state-machine.png draws these moves, apart from the stop and per-run cap moves that
# docs/SPECS.md lists separately. A move not listed here raises IllegalTransition.
TRANSITIONS: dict[State, set[State]] = {
    State.COLLECTING: {State.SEARCHING, State.AWAITING_APPROVAL, State.FAILED, State.ABANDONED},
    State.SEARCHING: {State.PRESENTING, State.COLLECTING, State.FAILED, State.ABANDONED},
    State.PRESENTING: {State.HOLDING, State.AWAITING_APPROVAL, State.COLLECTING, State.FAILED, State.ABANDONED},
    State.HOLDING: {State.AWAITING_APPROVAL, State.EXECUTING, State.SEARCHING, State.FAILED, State.ABANDONED},
    State.AWAITING_APPROVAL: {State.EXECUTING, State.COLLECTING, State.PRESENTING, State.FAILED, State.ABANDONED},
    State.EXECUTING: {State.VERIFYING, State.UNKNOWN_NEEDS_CHECK, State.HOLDING, State.SEARCHING},
    State.VERIFYING: {State.DONE, State.EXECUTING, State.SEARCHING, State.FAILED, State.ABANDONED, State.UNKNOWN_NEEDS_CHECK},
    State.UNKNOWN_NEEDS_CHECK: {State.VERIFYING},
    State.DONE: set(),
    State.FAILED: set(),
    State.ABANDONED: set(),
}


class IllegalTransition(Exception):
    pass


class CapReached(Exception):
    def __init__(self, kind: str, scope: str):
        super().__init__(f"{scope} cap reached: {kind}")
        self.kind = kind
        self.scope = scope


class ApprovalViolation(Exception):
    """A write did not match its approval record, so the gateway blocked it."""


class ToolFault(Exception):
    """A tool failed in a way the contract does not name."""

    def __init__(self, tool: str, detail: str):
        super().__init__(f"{tool}: {detail}")
        self.tool = tool


class InvalidModelOutput(Exception):
    pass


@dataclass(frozen=True)
class Limits:
    """Proposed starting values from docs/SPECS.md, not measurements."""

    turn_model_calls: int = 5
    run_model_calls: int = 30
    turn_tool_calls: int = 10
    run_tool_calls: int = 50
    turn_seconds: float = 120
    run_seconds: float = 900
    write_retries: int = 3
    verify_reads: int = 3


@dataclass
class Run:
    id: str
    user_id: str
    state: State
    workflow: str
    # Each detail is {"value": ..., "source": "user_this_run" | "memory_stated"}.
    details: dict = field(default_factory=dict)
    options: list = field(default_factory=list)
    booking_choices: list = field(default_factory=list)
    selected: dict | None = None
    target_booking: dict | None = None
    pending_question: dict | None = None
    excluded: list = field(default_factory=list)
    duplicate_cleared: bool = False
    # A search found nothing, so the run waits for the user to change a detail rather than searching again.
    awaiting_change: bool = False
    asked_inferred: list = field(default_factory=list)
    forgotten_in_flight: bool = False
    result: dict | None = None
    model_calls: int = 0
    tool_calls: int = 0
    active_seconds: float = 0.0

    def detail(self, key: str):
        entry = self.details.get(key)
        return None if entry is None else entry["value"]


@dataclass
class Approval:
    id: str
    run_id: str
    user_id: str
    action: str
    provider_id: str
    provider_name: str
    slot: str
    price: int
    price_label: str
    booking_id: str | None
    status: str
    idempotency_key: str | None
    source_message_id: int | None
    shown_reply_id: int | None


@dataclass
class Attempt:
    id: str
    approval_id: str
    idempotency_key: str
    hold_id: str | None
    status: str
    retries: int
    returned_booking_id: str | None
    contradiction_seen: bool
