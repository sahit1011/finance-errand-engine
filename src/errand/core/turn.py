"""What one user turn carries: its budget, its trace, and the reply being built."""

from dataclasses import dataclass, field

from errand.core.domain import CapReached, Limits, Run


class Budget:
    """Hard caps on model calls, tool calls and active time, per turn and per run."""

    def __init__(self, limits: Limits, clock):
        self.limits = limits
        self.clock = clock
        self.started = clock.now()
        self.model_calls = 0
        self.tool_calls = 0
        self.run: Run | None = None

    def attach(self, run: Run) -> None:
        self.run = run

    def charge_model(self) -> None:
        self.check_time()
        if self.model_calls >= self.limits.turn_model_calls:
            raise CapReached("model_calls", "turn")
        if self.run is not None and self.run.model_calls >= self.limits.run_model_calls:
            raise CapReached("model_calls", "run")
        self.model_calls += 1
        if self.run is not None:
            self.run.model_calls += 1

    def charge_tool(self) -> None:
        self.check_time()
        if self.tool_calls >= self.limits.turn_tool_calls:
            raise CapReached("tool_calls", "turn")
        if self.run is not None and self.run.tool_calls >= self.limits.run_tool_calls:
            raise CapReached("tool_calls", "run")
        self.tool_calls += 1
        if self.run is not None:
            self.run.tool_calls += 1

    def elapsed(self) -> float:
        return (self.clock.now() - self.started).total_seconds()

    def check_time(self) -> None:
        if self.elapsed() >= self.limits.turn_seconds:
            raise CapReached("wall_clock", "turn")
        if self.run is not None and self.run.active_seconds + self.elapsed() >= self.limits.run_seconds:
            raise CapReached("wall_clock", "run")

    def remaining_seconds(self) -> float:
        remaining = self.limits.turn_seconds - self.elapsed()
        if self.run is not None:
            remaining = min(remaining, self.limits.run_seconds - self.run.active_seconds - self.elapsed())
        return max(remaining, 0.0)


@dataclass
class Turn:
    user_id: str
    text: str
    message_id: int
    budget: Budget
    tracer: object
    lines: list[str] = field(default_factory=list)
    trace_lines: list[str] = field(default_factory=list)
    # Set when this turn's reply shows an approval request, so the reply id can be recorded on it.
    shown_approval_id: str | None = None
    stop_requested: bool = False
    # When a cap has been reached this turn: "turn" or "run". Verification still runs, but no new write starts.
    cap_scope: str | None = None
    run: Run | None = None
    # Outcomes saved in the same transaction as their final state, marked delivered with this turn's reply.
    outcome_reply_ids: list[int] = field(default_factory=list)

    def say(self, line: str) -> None:
        self.lines.append(line)
        self.trace_lines.append(line)

    def say_untrusted(self, line: str) -> None:
        """Quoted review text goes to the user, but a trace keeps only that it was shown."""
        self.lines.append(line)
        self.trace_lines.append("[provider details shown to the user; review text omitted]")
