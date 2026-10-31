"""Runs one turn from message to reply: caps, the final error handler, and resuming after a crash."""

from dataclasses import dataclass
from typing import Callable

from errand.core import render
from errand.core.approval import move
from errand.core.domain import TERMINAL_STATES, CapReached, Limits, ModelUnavailable, State
from errand.core.executor import Executor
from errand.core.memory import Memory
from errand.core.turn import Budget, Turn
from errand.core.workflow import Workflow
from errand.gateway import ToolGateway
from errand.infra.store import Store
from errand.infra.trace import Tracer

# A turn with no message behind it, such as resuming after a crash.
NO_MESSAGE_ID = 0


@dataclass
class Reply:
    id: int
    text: str


def _no_checkpoint(name: str) -> None:
    return None


class Engine:
    def __init__(self, tools, store: Store, model, trace_dir: str, clock, limits: Limits = Limits(), checkpoint: Callable[[str], None] = _no_checkpoint):
        self.store = store
        self.clock = clock
        self.limits = limits
        self.trace_dir = trace_dir
        self.checkpoint = checkpoint
        self.gateway = ToolGateway(tools, store)
        self.memory = Memory(store)
        self.executor = Executor(store, self.gateway, self.memory, limits, checkpoint)
        self.workflow = Workflow(store, self.gateway, model, self.memory, self.executor, clock)

    def handle_message(self, user_id: str, text: str, last_reply_id: int | None) -> Reply:
        run = self.store.active_run(user_id)
        message_id = self.store.save_message(user_id, run.id if run else None, text)
        turn = self._new_turn(user_id, text, message_id, run)
        turn.tracer.event("user_message", message_id=message_id, text=text)
        self._guarded(turn, lambda: self.workflow.handle(turn, last_reply_id))
        return self._finish(turn)

    def start_up(self, user_id: str) -> list[Reply]:
        """Shows replies a crash kept from being delivered, then finishes any work left in flight."""
        replies = []
        run = self.store.active_run(user_id)
        in_flight = run is not None and self.store.open_attempt(run.id) is not None
        for row in self.store.undelivered_replies(user_id):
            self.store.mark_delivered(row["id"])
            # An active run shows its current question below, which replaces any older copy of it.
            if run is None or in_flight or row["run_id"] != run.id:
                replies.append(Reply(row["id"], row["text"]))
        if run is None:
            return replies
        turn = self._new_turn(user_id, "", NO_MESSAGE_ID, run)
        turn.tracer.event("resumed", run_id=run.id, state=run.state.value)
        self._guarded(turn, lambda: self.workflow.start_up(turn))
        if turn.lines:
            replies.append(self._finish(turn))
        return replies

    def _new_turn(self, user_id: str, text: str, message_id: int, run) -> Turn:
        tracer = Tracer(self.trace_dir, self.clock)
        budget = Budget(self.limits, self.clock)
        turn = Turn(user_id=user_id, text=text, message_id=message_id, budget=budget, tracer=tracer, run=run)
        if run is not None:
            budget.attach(run)
            tracer.bind(run.id)
        return turn

    def _guarded(self, turn: Turn, work: Callable[[], None]) -> None:
        try:
            work()
        except CapReached as cap:
            self._on_cap(turn, cap)
        except ModelUnavailable as error:
            turn.tracer.event("error", where="model", error_type=type(error).__name__, detail=str(error))
            turn.say(render.MODEL_UNAVAILABLE)
            # The next yes must count, and it only counts against a proposal in the reply the user last saw.
            if turn.run is not None and turn.run.state == State.AWAITING_APPROVAL:
                self.workflow.show_pending_approval(turn, turn.run)
        except Exception as error:
            # The one place that catches everything, because every exit must end with a message to the user.
            self._on_error(turn, error)

    def _on_cap(self, turn: Turn, cap: CapReached) -> None:
        turn.tracer.event("cap_reached", kind=cap.kind, scope=cap.scope)
        run = turn.run
        turn.say(f"{render.CAP_MESSAGES[cap.kind]} for this {cap.scope}.")
        if run is not None and self.store.open_attempt(run.id) is not None:
            self.workflow.verify_after_cap(turn, cap.scope)
        elif run is not None and cap.scope == "run":
            self.workflow.fail_for_cap(turn)
            turn.say("I have stopped this errand, and nothing was booked or changed. You can start a new one.")
        else:
            turn.say("Your details are saved. Reply to continue.")

    def _on_error(self, turn: Turn, error: Exception) -> None:
        turn.tracer.event("error", where="turn", error_type=type(error).__name__, detail=str(error))
        run = turn.run
        if run is not None and run.state in TERMINAL_STATES:
            turn.say("Something went wrong after that step, but the result above stands.")
            return
        if run is not None and self.store.open_attempt(run.id) is not None:
            if run.state in (State.EXECUTING, State.VERIFYING):
                move(self.store, turn.tracer, run, State.UNKNOWN_NEEDS_CHECK, "unexpected error")
            turn.say("Something went wrong and I cannot confirm the outcome yet. I will not report success or try anything new, and I will check again on your next message.")
            return
        turn.say("Something went wrong on my side, and nothing was booked or changed by that step. Your details are saved, so please try again.")

    def _finish(self, turn: Turn) -> Reply:
        run = turn.run
        if run is not None:
            run.active_seconds += turn.budget.elapsed()
            self.store.save_run(run)
        text = "\n".join(turn.lines) if turn.lines else render.NOTHING_TO_SAY
        with self.store.tx():
            reply_id = self.store.save_reply(turn.user_id, run.id if run else None, text)
            # This reply repeats any saved outcome, so only one of them may ever be shown after a crash.
            for outcome_id in turn.outcome_reply_ids:
                self.store.mark_delivered(outcome_id)
        if turn.shown_approval_id is not None:
            self.store.set_shown_reply(turn.shown_approval_id, reply_id)
        trace_text = "\n".join(turn.trace_lines) if turn.trace_lines else text
        turn.tracer.event("reply", reply_id=reply_id, text=trace_text, state=run.state.value if run else None, model_calls=turn.budget.model_calls, tool_calls=turn.budget.tool_calls)
        turn.tracer.flush_unbound(f"no-run-{turn.message_id}")
        self.checkpoint("before_reply")
        self.store.mark_delivered(reply_id)
        return Reply(reply_id, text)
