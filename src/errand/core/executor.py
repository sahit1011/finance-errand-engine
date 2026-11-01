"""The write and verify protocols for book, cancel and reschedule. Each step can resume after a crash."""

from errand.contract import ToolError, ToolTimeout, TransientError
from errand.core import render
from errand.core.approval import consume, move, void
from errand.core.domain import ApprovalViolation, Approval, Attempt, Limits, Run, State, ToolFault
from errand.core.turn import Turn


class Executor:
    def __init__(self, store, gateway, memory, limits: Limits, checkpoint):
        self.store = store
        self.gateway = gateway
        self.memory = memory
        self.limits = limits
        self.checkpoint = checkpoint

    def execute(self, turn: Turn, run: Run) -> None:
        attempt = self.store.open_attempt(run.id)
        approval = self.store.approval(attempt.approval_id)
        try:
            result = getattr(self.gateway, approval.action)(turn, approval.id)
        except ApprovalViolation as violation:
            turn.tracer.event("approval_violation", detail=str(violation))
            turn.say("The details no longer matched what you approved, so I did not go ahead.")
            self._void_and_search(turn, run, approval, "write did not match approval")
        except ToolTimeout:
            self.checkpoint("after_write_call")
            move(self.store, turn.tracer, run, State.VERIFYING, "timeout, outcome unknown")
        except TransientError:
            self._retry_or_verify(turn, run, attempt, "transient 503")
        except ToolError as error:
            self._named_failure(turn, run, approval, attempt, error)
        except ToolFault as fault:
            turn.tracer.event("error", where="write", detail=str(fault))
            move(self.store, turn.tracer, run, State.UNKNOWN_NEEDS_CHECK, "unexpected error during write")
        else:
            self.checkpoint("after_write_call")
            if approval.action == "book":
                attempt.returned_booking_id = result["booking_id"]
                self.store.save_attempt(attempt)
            move(self.store, turn.tracer, run, State.VERIFYING, "write returned")

    def verify(self, turn: Turn, run: Run) -> bool:
        """Returns False when the turn should end with the run left as it is."""
        attempt = self.store.open_attempt(run.id)
        approval = self.store.approval(attempt.approval_id)
        if approval.action == "book":
            return self._verify_book(turn, run, approval, attempt)
        return self._verify_change(turn, run, approval, attempt)

    def _named_failure(self, turn: Turn, run: Run, approval: Approval, attempt: Attempt, error: ToolError) -> None:
        if error.code == "hold_expired":
            if attempt.retries >= self.limits.write_retries:
                move(self.store, turn.tracer, run, State.VERIFYING, "hold expired, retries used up")
                return
            attempt.retries += 1
            self.store.save_attempt(attempt)
            move(self.store, turn.tracer, run, State.HOLDING, "hold expired")
        elif error.code in ("slot_taken", "slot_conflict"):
            turn.say(f"That slot is no longer available, so nothing was changed for {render.slot_text(approval.slot)}. Here is what else is open.")
            self._void_and_search(turn, run, approval, error.code)
        else:
            raise error

    def _retry_or_verify(self, turn: Turn, run: Run, attempt: Attempt, reason: str) -> None:
        if attempt.retries >= self.limits.write_retries:
            move(self.store, turn.tracer, run, State.VERIFYING, f"{reason}, retries used up")
            return
        attempt.retries += 1
        self.store.save_attempt(attempt)
        turn.tracer.event("retry", attempt_id=attempt.id, retries=attempt.retries, reason=reason)

    def _verify_book(self, turn: Turn, run: Run, approval: Approval, attempt: Attempt) -> bool:
        status = self._read_status(turn, attempt.idempotency_key)
        if status is None:
            return self._no_answer(turn, run)
        if status["status"] == "booked":
            self._finish_book(turn, run, approval, status["booking_id"])
            return True
        if status["status"] == "failed":
            if run.stop_requested:
                self._abandon(turn, run, approval, "Stopped. Nothing was booked.")
                return True
            turn.say(f"That slot was taken, so {render.slot_text(approval.slot)} was not booked. Here is what else is open.")
            self._void_and_search(turn, run, approval, "status failed")
            return True
        if attempt.returned_booking_id and not attempt.contradiction_seen:
            attempt.contradiction_seen = True
            self.store.save_attempt(attempt)
            move(self.store, turn.tracer, run, State.UNKNOWN_NEEDS_CHECK, "book returned success but status is not_found")
            turn.say("The provider's answers disagree, so I cannot say this is booked yet. I will check again on your next message.")
            return False
        return self._not_booked(turn, run, approval, attempt)

    def _not_booked(self, turn: Turn, run: Run, approval: Approval, attempt: Attempt) -> bool:
        if run.stop_requested:
            self._abandon(turn, run, approval, "Stopped. Nothing was booked.")
            return True
        if turn.cap_scope == "run":
            self._void_and_fail(turn, run, approval, "per-run cap, nothing booked", "Nothing was booked.")
            return True
        if turn.cap_scope == "turn":
            turn.say("Nothing is booked yet. I will try again when you next reply.")
            return False
        if run.forgotten_in_flight:
            run.forgotten_in_flight = False
            turn.say("Nothing was booked, and I will not retry using a preference you asked me to forget.")
            self._void_and_search(turn, run, approval, "approved terms used a forgotten value")
            return True
        if attempt.returned_booking_id:
            turn.say("The provider confirms it has no booking for this, so nothing was booked. Here is what else is open.")
            self._void_and_search(turn, run, approval, "status stayed not_found after recheck")
            return True
        if attempt.retries < self.limits.write_retries:
            attempt.retries += 1
            self.store.save_attempt(attempt)
            turn.tracer.event("retry", attempt_id=attempt.id, retries=attempt.retries, reason="status not_found")
            move(self.store, turn.tracer, run, State.EXECUTING, "status not_found, retrying with the same key")
            return True
        turn.say("I tried several times and the provider confirms nothing was booked. Here is what else is open.")
        self._void_and_search(turn, run, approval, "retries used up, status not_found")
        return True

    def _verify_change(self, turn: Turn, run: Run, approval: Approval, attempt: Attempt) -> bool:
        bookings = self._read_bookings(turn, run.user_id)
        if bookings is None:
            return self._no_answer(turn, run)
        booking = next((b for b in bookings if b["id"] == approval.booking_id), None)
        applied = booking is not None and (
            booking["status"] == "cancelled" if approval.action == "cancel" else booking["slot"] == approval.slot
        )
        if applied:
            self._finish_change(turn, run, approval)
            return True
        if run.stop_requested:
            self._abandon(turn, run, approval, "Stopped. Your booking is unchanged.")
            return True
        if run.forgotten_in_flight:
            run.forgotten_in_flight = False
            self._void_and_fail(turn, run, approval, "forgotten value during a change", "I stopped because you asked me to forget a preference this used. Your booking is unchanged.")
            return True
        if turn.cap_scope == "turn":
            turn.say("Your booking is unchanged so far. I will try again when you next reply.")
            return False
        if turn.cap_scope is None and attempt.retries < self.limits.write_retries:
            attempt.retries += 1
            self.store.save_attempt(attempt)
            move(self.store, turn.tracer, run, State.EXECUTING, "change not applied, retrying with the same key")
            return True
        self._void_and_fail(turn, run, approval, "cancel or reschedule not applied", "I could not complete that, and your booking is unchanged.")
        return True

    def _no_answer(self, turn: Turn, run: Run) -> bool:
        move(self.store, turn.tracer, run, State.UNKNOWN_NEEDS_CHECK, "no definite answer")
        if run.stop_requested:
            turn.say("I cannot confirm the outcome yet, so your stop takes effect once it is known. I will check again on your next message.")
        else:
            turn.say("I cannot confirm the outcome yet, so I will not report success or try anything new. I will check again on your next message.")
        return False

    def _read_status(self, turn: Turn, key: str) -> dict | None:
        for _ in range(self.limits.verify_reads):
            try:
                return self.gateway.get_booking_status(turn, key)
            except ToolFault:
                continue
        return None

    def _read_bookings(self, turn: Turn, user_id: str) -> list[dict] | None:
        for _ in range(self.limits.verify_reads):
            try:
                return self.gateway.list_bookings(turn, user_id, verifying=True)
            except ToolFault:
                continue
        return None

    def _finish_book(self, turn: Turn, run: Run, approval: Approval, booking_id: str) -> None:
        text = render.booked(approval, booking_id)
        with self.store.tx():
            consume(self.store, turn.tracer, run, approval.id)
            self.store.save_booking_price(booking_id, approval.price)
            # Where it was searched, because a saved area can change while the outcome is unknown. A proposal saved before
            # options carried an area has none, and a later move then simply asks.
            self.store.save_booking_place(booking_id, run.selected.get("area"), approval.provider_name)
            run.result = {"summary": text, "booking_id": booking_id}
            run.forgotten_in_flight = False
            move(self.store, turn.tracer, run, State.DONE, "verified booked")
            turn.outcome_reply_ids.append(self.store.save_reply(run.user_id, run.id, text))
        turn.tracer.event("verified", booking_id=booking_id)
        self.checkpoint("after_save_outcome")
        turn.say(text)
        if run.stop_requested:
            turn.say("It was already booked when you asked to stop. Tell me if you would like to cancel it.")
        self.memory.infer_after_booking(run, turn)

    def _finish_change(self, turn: Turn, run: Run, approval: Approval) -> None:
        text = render.cancelled(approval) if approval.action == "cancel" else render.rescheduled(approval)
        with self.store.tx():
            consume(self.store, turn.tracer, run, approval.id)
            run.result = {"summary": text, "booking_id": approval.booking_id}
            move(self.store, turn.tracer, run, State.DONE, f"verified {approval.action}")
            turn.outcome_reply_ids.append(self.store.save_reply(run.user_id, run.id, text))
        turn.tracer.event("verified", booking_id=approval.booking_id)
        self.checkpoint("after_save_outcome")
        turn.say(text)

    # Voiding and moving happen in one transaction, so a crash cannot leave a run with no way forward.

    def _void_and_fail(self, turn: Turn, run: Run, approval: Approval, reason: str, text: str) -> None:
        with self.store.tx():
            void(self.store, turn.tracer, run, approval.id, reason)
            run.result = {"summary": text}
            move(self.store, turn.tracer, run, State.FAILED, reason)
            turn.outcome_reply_ids.append(self.store.save_reply(run.user_id, run.id, text))
        turn.say(text)

    def _void_and_search(self, turn: Turn, run: Run, approval: Approval, reason: str) -> None:
        with self.store.tx():
            void(self.store, turn.tracer, run, approval.id, reason)
            run.excluded.append(f"{approval.provider_id}|{approval.slot}")
            run.selected = None
            run.options = []
            move(self.store, turn.tracer, run, State.SEARCHING, reason)

    def _abandon(self, turn: Turn, run: Run, approval: Approval, text: str) -> None:
        with self.store.tx():
            void(self.store, turn.tracer, run, approval.id, "user stopped")
            move(self.store, turn.tracer, run, State.ABANDONED, "user stopped")
        turn.say(text)
