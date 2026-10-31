"""Approval records, and the one state-move function every part of the engine uses."""

from errand.core.domain import TRANSITIONS, Approval, Attempt, IllegalTransition, Run, State


def move(store, tracer, run: Run, to: State, reason: str) -> None:
    if to not in TRANSITIONS[run.state]:
        raise IllegalTransition(f"{run.state} to {to} ({reason})")
    previous = run.state
    with store.tx():
        run.state = to
        store.record_transition(run.id, previous, to, reason)
        store.save_run(run)
    tracer.event("transition", run_id=run.id, from_state=previous.value, to_state=to.value, reason=reason)


def propose(store, tracer, run: Run, action: str, provider_id: str, provider_name: str, slot: str, price: int, price_label: str = "", booking_id: str | None = None, hold_id: str | None = None) -> Approval:
    approval = Approval(
        id=f"approval-{store.next_number('approvals')}",
        run_id=run.id,
        user_id=run.user_id,
        action=action,
        provider_id=provider_id,
        provider_name=provider_name,
        slot=slot,
        price=price,
        price_label=price_label,
        booking_id=booking_id,
        status="pending",
        idempotency_key=None,
        source_message_id=None,
        shown_reply_id=None,
    )
    store.insert_approval(approval, hold_id)
    run.pending_question = {"kind": "approval", "approval_id": approval.id}
    tracer.event("approval", approval_id=approval.id, status="pending", action=action, provider_id=provider_id, slot=slot, price=price)
    return approval


def approve(store, tracer, run: Run, approval_id: str, message_id: int) -> bool:
    """Approves, creates the key and saves the attempt in one transaction, so a crash leaves all or none of it."""
    with store.tx():
        key = f"idem-{approval_id}"
        if not store.mark_approved(approval_id, message_id, key):
            return False
        store.insert_attempt(
            Attempt(
                id=f"attempt-{approval_id}",
                approval_id=approval_id,
                idempotency_key=key,
                hold_id=store.approval_hold_id(approval_id),
                status="open",
                retries=0,
                returned_booking_id=None,
                contradiction_seen=False,
            )
        )
        run.pending_question = None
        move(store, tracer, run, State.EXECUTING, "user approved")
    tracer.event("approval", approval_id=approval_id, status="approved", source_message_id=message_id, idempotency_key=key)
    return True


def void(store, tracer, run: Run, approval_id: str, reason: str) -> None:
    with store.tx():
        store.set_approval_status(approval_id, "voided")
        attempt = store.attempt_for_approval(approval_id)
        if attempt is not None:
            attempt.status = "closed"
            store.save_attempt(attempt)
        if run.pending_question and run.pending_question.get("approval_id") == approval_id:
            run.pending_question = None
        store.save_run(run)
    tracer.event("approval", approval_id=approval_id, status="voided", reason=reason)


def void_pending(store, tracer, run: Run, reason: str) -> None:
    if run.pending_question and run.pending_question.get("kind") == "approval":
        void(store, tracer, run, run.pending_question["approval_id"], reason)


def reopen(store, tracer, run: Run, reason: str) -> None:
    """A detail changed before any write began, so anything built on the old details is thrown away."""
    with store.tx():
        void_pending(store, tracer, run, reason)
        run.options = []
        run.selected = None
        move(store, tracer, run, State.COLLECTING, reason)


def consume(store, tracer, run: Run, approval_id: str) -> None:
    with store.tx():
        store.set_approval_status(approval_id, "consumed")
        attempt = store.attempt_for_approval(approval_id)
        attempt.status = "closed"
        store.save_attempt(attempt)
    tracer.event("approval", approval_id=approval_id, status="consumed")
