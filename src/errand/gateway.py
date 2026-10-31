"""The only path to tools. Every write is checked against its approval record immediately before it runs."""

import time

from errand.contract import ToolError
from errand.core.domain import ApprovalViolation, ToolFault
from errand.core.turn import Turn

WRITE_STATUS = "approved"


class ToolGateway:
    def __init__(self, tools, store):
        self.tools = tools
        self.store = store

    # Reads and holds.

    def search_providers(self, turn: Turn, service: str, area: str, date_range: tuple[str, str]) -> list[dict]:
        return self._call(turn, "search_providers", service, area, date_range)

    def get_provider(self, turn: Turn, provider_id: str) -> dict:
        return self._call(turn, "get_provider", provider_id)

    def list_bookings(self, turn: Turn, user_id: str, verifying: bool = False) -> list[dict]:
        return self._call(turn, "list_bookings", user_id, charge=not verifying)

    def hold_slot(self, turn: Turn, provider_id: str, slot: str) -> dict:
        return self._call(turn, "hold_slot", provider_id, slot)

    def get_booking_status(self, turn: Turn, key: str) -> dict:
        # Not charged: a write must be verifiable even after a cap is reached. The executor bounds these reads.
        return self._call(turn, "get_booking_status", key, charge=False)

    # Writes. These take only an approval id, so a caller cannot pass terms that differ from the record.

    def book(self, turn: Turn, approval_id: str) -> dict:
        approval = self._checked_approval(approval_id, "book")
        hold_id = self.store.attempt_for_approval(approval_id).hold_id
        hold = self.store.hold(hold_id)
        if hold is None or (hold["provider_id"], hold["slot"], hold["quoted_price"]) != (approval.provider_id, approval.slot, approval.price):
            raise ApprovalViolation(f"hold {hold_id} does not match approval {approval_id}")
        return self._call(turn, "book", hold_id, approval.idempotency_key)

    def cancel(self, turn: Turn, approval_id: str) -> dict:
        approval = self._checked_approval(approval_id, "cancel")
        return self._call(turn, "cancel", approval.booking_id, approval.idempotency_key)

    def reschedule(self, turn: Turn, approval_id: str) -> dict:
        approval = self._checked_approval(approval_id, "reschedule")
        return self._call(turn, "reschedule", approval.booking_id, approval.slot, approval.idempotency_key)

    def _checked_approval(self, approval_id: str, action: str):
        approval = self.store.approval(approval_id)
        if approval.status != WRITE_STATUS or approval.action != action or not approval.idempotency_key:
            raise ApprovalViolation(f"{action} blocked: approval {approval_id} has status {approval.status}, action {approval.action}")
        return approval

    def _call(self, turn: Turn, name: str, *args, charge: bool = True):
        if charge:
            turn.budget.charge_tool()
        started = time.perf_counter()
        try:
            result = getattr(self.tools, name)(*args)
        except ToolError as error:
            self._trace(turn, name, args, started, error=error.code)
            raise
        except Exception as error:
            # Boundary: anything the contract does not name becomes one named failure for the engine.
            self._trace(turn, name, args, started, error=f"fault: {error}")
            raise ToolFault(name, str(error)) from error
        self._trace(turn, name, args, started, result=result)
        return result

    @staticmethod
    def _trace(turn: Turn, name: str, args: tuple, started: float, **outcome) -> None:
        turn.tracer.event("tool_call", tool=name, args=list(args), latency_ms=round((time.perf_counter() - started) * 1000, 2), **outcome)
