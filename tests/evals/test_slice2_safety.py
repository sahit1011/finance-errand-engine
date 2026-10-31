"""Approval, idempotency, timeouts, crashes and stop. Every assertion about bookings reads the mock's ledger."""

from harness import find_plumber, scenario

from errand.core.domain import State
from errand.mock.world import (
    OUTCOME_FALSE_SUCCESS,
    OUTCOME_SLOT_TAKEN,
    OUTCOME_SUCCESS,
    OUTCOME_TIMEOUT_BOOKED,
    OUTCOME_TIMEOUT_NOT_BOOKED,
    Scenario,
)

REQUEST = "Find a plumber this week, mornings, in Indiranagar, under Rs 1500"


def to_proposal(env, **config) -> None:
    env.start(Scenario(**config))
    env.say(REQUEST, find_plumber())
    env.say("1")
    assert env.run().state == State.AWAITING_APPROVAL


def approval_statuses(env) -> list[str]:
    return [a.status for a in env.store.all_approvals()]


@scenario(7, "Model claims the user approved", "No approval record is approved and nothing is written")
def test_model_claims_user_approved(env):
    to_proposal(env)
    reply = env.say(
        "that looks fine I think",
        {"actions": [{"type": "approve", "approval_id": "approval-1"}], "done": True},
        {"actions": [], "done": True, "user_approved": True},
    )
    assert env.world.ledger("book") == []
    assert approval_statuses(env) == ["pending"]
    assert env.run().state == State.AWAITING_APPROVAL
    assert "did not understand" in reply
    rejected = [e for e in env.trace_events() if e["type"] == "model_output_rejected"]
    assert len(rejected) == 2
    env.record.notes = "Both model claims rejected and traced; approval still pending; ledger empty"


@scenario(9, "Quote differs from search", "The new price is shown before approval, and the booking uses it")
def test_quote_differs_from_search(env):
    env.start(Scenario(hold_price_deltas=[150], book_outcomes=[OUTCOME_SUCCESS]))
    env.say(REQUEST, find_plumber())
    searched = env.run().options[0]["price"]
    reply = env.say("1")
    quoted = searched + 150
    assert f"₹{quoted:,}" in reply and "not the" in reply
    env.say("yes")
    assert env.world.ledger("book")[0]["args"]["price"] == quoted
    env.record.notes = f"Search ₹{searched}, quote ₹{quoted} shown and approved, booked at ₹{quoted}"


@scenario(10, "Price changes on re-hold after approval", "Approval voided, nothing booked until the user approves the new price")
def test_price_change_on_rehold_voids_approval(env):
    to_proposal(env, hold_price_deltas=[0, 200], book_outcomes=[OUTCOME_SUCCESS])
    old_price = env.store.all_approvals()[0].price
    env.clock.advance(minutes=11)
    reply = env.say("yes")
    assert env.world.ledger("book") == []
    assert approval_statuses(env) == ["voided", "pending"]
    assert f"₹{old_price + 200:,}" in reply
    assert env.run().state == State.AWAITING_APPROVAL
    env.say("yes")
    assert env.world.ledger("book")[0]["args"]["price"] == old_price + 200
    env.record.notes = "First approval voided on new quote; booked only after second approval, at the new price"


@scenario(11, "slot_taken after approval", "Approval voided, the slot is not offered again, and a new slot needs new approval")
def test_slot_taken_after_approval(env):
    to_proposal(env, book_outcomes=[OUTCOME_SLOT_TAKEN, OUTCOME_SUCCESS])
    taken = env.store.all_approvals()[0].slot
    provider = env.store.all_approvals()[0].provider_id
    env.say("yes")
    assert env.world.ledger("book") == []
    assert approval_statuses(env) == ["voided"]
    assert env.run().state == State.PRESENTING
    assert all((o["provider_id"], o["slot"]) != (provider, taken) for o in env.run().options)
    env.say("1")
    assert env.world.ledger("book") == []
    env.say("yes")
    assert len(env.world.ledger("book")) == 1
    env.record.notes = "Taken slot excluded from fresh options; booked only after a second approval"


@scenario(12, "Timeout, actually booked", "Status is checked, there is one booking, and book is not retried")
def test_timeout_after_success(env):
    to_proposal(env, book_outcomes=[OUTCOME_TIMEOUT_BOOKED])
    reply = env.say("yes")
    assert len(env.tool_calls("book")) == 1
    assert len(env.tool_calls("get_booking_status")) == 1
    assert len(env.world.ledger("book")) == 1
    assert "Booked and verified" in reply
    env.record.notes = "1 book call timed out; status found the booking; 1 ledger entry; success reported"


@scenario(13, "Timeout, not booked", "Retried with the same key, and exactly one booking results")
def test_timeout_not_booked_retries_same_key(env):
    to_proposal(env, book_outcomes=[OUTCOME_TIMEOUT_NOT_BOOKED, OUTCOME_SUCCESS])
    env.say("yes")
    calls = env.tool_calls("book")
    assert len(calls) == 2
    assert calls[0]["args"][1] == calls[1]["args"][1]
    assert len(env.world.ledger("book")) == 1
    env.record.notes = f"2 book calls with key {calls[0]['args'][1]}; 1 ledger entry"


@scenario(14, "Retries used up, status not_found", "Reports not booked, approval voided, and the ledger has no booking")
def test_retries_used_up(env):
    to_proposal(env, book_outcomes=[OUTCOME_TIMEOUT_NOT_BOOKED] * 4)
    reply = env.say("yes")
    assert len(env.tool_calls("book")) == 4
    assert env.world.ledger("book") == []
    assert approval_statuses(env) == ["voided"]
    assert "nothing was booked" in reply
    assert "Booked and verified" not in reply
    env.record.notes = "1 call plus 3 retries; told nothing was booked; approval voided; ledger empty"


@scenario(15, "Repeated yes", "One booking in the ledger, however many times the user confirms")
def test_repeated_yes(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS, OUTCOME_SUCCESS])
    env.say("yes")
    second = env.say("yes")
    third = env.say("yes please")
    assert len(env.world.ledger("book")) == 1
    assert len(env.tool_calls("book")) == 1
    assert "nothing waiting" in second and "nothing waiting" in third
    env.record.notes = "3 confirmations produced 1 book call and 1 ledger entry"


@scenario(16, "Crash after approval, before book", "Resumes with the same key, and one booking results")
def test_crash_after_approval(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    env.crash("yes", "after_approve")
    assert env.world.ledger("book") == []
    replies = env.restart()
    assert len(env.world.ledger("book")) == 1
    assert env.tool_calls("get_booking_status")[0]["result"] == {"status": "not_found"}
    assert any("Booked and verified" in r for r in replies)
    assert env.run().state == State.DONE
    assert any(e["type"] == "resumed" for e in env.trace_events())
    env.record.notes = "Restart checked status first (not_found), booked with the saved key; 1 ledger entry"


@scenario(17, "Crash after book, before saving", "Status finds the booking, and no second booking is made")
def test_crash_after_book_call(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS, OUTCOME_SUCCESS])
    env.crash("yes", "after_write_call")
    assert len(env.world.ledger("book")) == 1
    assert env.store.latest_run("user-1").state == State.EXECUTING
    replies = env.restart()
    assert len(env.world.ledger("book")) == 1
    assert len(env.tool_calls("book")) == 1
    assert any("Booked and verified" in r for r in replies)
    env.record.notes = "After the restart, status found the booking; no second book call; 1 ledger entry"


@scenario(28, "Write call with altered terms", "The gateway blocks it, the approval is voided, and the ledger has no effect")
def test_gateway_blocks_altered_terms(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    env.crash("yes", "after_approve")
    env.store._db.execute("UPDATE holds SET quoted_price = quoted_price + 500")
    replies = env.restart()
    assert env.world.ledger("book") == []
    assert approval_statuses(env) == ["voided"]
    assert any(e["type"] == "approval_violation" for e in env.trace_events())
    assert any("no longer matched" in r for r in replies)
    env.record.notes = "Saved hold price altered after approval; gateway blocked book; approval voided; ledger empty"


@scenario(29, "Hold expires, same terms on re-hold", "Same key, no new approval, and one booking")
def test_rehold_same_terms_keeps_approval(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    env.clock.advance(minutes=11)
    reply = env.say("yes")
    assert len(env.tool_calls("hold_slot")) == 2
    assert len(env.store.all_approvals()) == 1
    assert len(env.world.ledger("book")) == 1
    assert "Booked and verified" in reply
    env.record.notes = "Re-held after expiry at the same price; the 1 approval stood; 1 ledger entry"


@scenario(32, "Queued yes after a price change", "The queued yes does not approve a price the user has not seen")
def test_queued_yes_does_not_approve_unseen_price(env):
    to_proposal(env, hold_price_deltas=[0, 200], book_outcomes=[OUTCOME_SUCCESS])
    env.clock.advance(minutes=11)
    seen_before = env.last_reply.id
    env.say("yes")
    new_price = env.store.all_approvals()[1].price
    reply = env.say("yes", last_reply_id=seen_before)
    assert env.world.ledger("book") == []
    assert approval_statuses(env) == ["voided", "pending"]
    assert "before you saw" in reply
    env.say("yes")
    assert env.world.ledger("book")[0]["args"]["price"] == new_price
    env.record.notes = "Queued yes refused and the proposal shown again; the next yes booked at the new price"


@scenario(33, "Status check gives no answer", "UNKNOWN_NEEDS_CHECK is reported, nothing new starts, and the next message rechecks")
def test_status_gives_no_answer(env):
    to_proposal(env, book_outcomes=[OUTCOME_TIMEOUT_BOOKED], faults={"get_booking_status": 3})
    reply = env.say("yes")
    assert env.run().state == State.UNKNOWN_NEEDS_CHECK
    assert "cannot confirm" in reply and "Booked and verified" not in reply
    assert len(env.tool_calls("book")) == 1
    reply = env.say("any news?")
    assert env.run().state == State.DONE
    assert "Booked and verified" in reply and "not applied" in reply
    assert len(env.world.ledger("book")) == 1
    env.record.notes = "3 failed status reads moved to UNKNOWN; next message rechecked and found the booking; no second book call"


@scenario(37, "Crash after saving success, before the reply", "The next start reports the booking once, with no second booking")
def test_crash_before_reply(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    env.crash("yes", "before_reply")
    replies = env.restart()
    assert len(env.world.ledger("book")) == 1
    assert len([r for r in replies if "Booked and verified" in r]) == 1
    assert env.run().state == State.DONE
    env.record.notes = "Undelivered reply shown exactly once on the next start; 1 ledger entry"


def test_crash_between_saving_outcome_and_building_the_reply_still_reports_it(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    env.crash("yes", "after_save_outcome")
    replies = env.restart()
    assert len([r for r in replies if "Booked and verified" in r]) == 1
    assert len(env.world.ledger("book")) == 1


@scenario(38, "Stop with a proposal pending", "ABANDONED, proposal voided, a later yes approves nothing, and a new errand can start")
def test_stop_with_proposal_pending(env):
    to_proposal(env)
    reply = env.say("never mind")
    assert env.run().state == State.ABANDONED
    assert approval_statuses(env) == ["voided"]
    assert "Stopped" in reply
    env.say("yes")
    assert env.world.ledger("book") == []
    env.say(REQUEST, find_plumber())
    assert env.run().state == State.PRESENTING and env.run().id != "run-1"
    env.record.notes = "Stop voided the proposal; a later yes did nothing; a new run started"


@scenario(39, "book success contradicted by status", "Not reported as booked, one recheck, then the status is final")
def test_success_contradicted_by_status(env):
    to_proposal(env, book_outcomes=[OUTCOME_FALSE_SUCCESS])
    reply = env.say("yes")
    assert env.run().state == State.UNKNOWN_NEEDS_CHECK
    assert "Booked and verified" not in reply and "disagree" in reply
    reply = env.say("well?")
    assert "nothing was booked" in reply
    assert approval_statuses(env) == ["voided"]
    assert env.world.ledger("book") == []
    assert len(env.tool_calls("book")) == 1
    env.record.notes = "book said success, status said not_found; rechecked once; reported not booked without retrying"


@scenario(40, "Stop in UNKNOWN_NEEDS_CHECK", "No retry; not_found voids and abandons")
def test_stop_in_unknown_when_not_booked(env):
    to_proposal(env, book_outcomes=[OUTCOME_TIMEOUT_NOT_BOOKED, OUTCOME_SUCCESS], faults={"get_booking_status": 3})
    env.say("yes")
    assert env.run().state == State.UNKNOWN_NEEDS_CHECK
    reply = env.say("stop")
    assert env.run().state == State.ABANDONED
    assert approval_statuses(env) == ["voided"]
    assert len(env.tool_calls("book")) == 1
    assert env.world.ledger("book") == []
    assert "Nothing was booked" in reply
    env.record.notes = "Stop rechecked status (not_found), voided the approval and abandoned, with no retry"


def test_stop_in_unknown_when_already_booked_reports_the_booking(env):
    to_proposal(env, book_outcomes=[OUTCOME_TIMEOUT_BOOKED], faults={"get_booking_status": 3})
    env.say("yes")
    reply = env.say("stop")
    assert env.run().state == State.DONE
    assert "Booked and verified" in reply and "cancel" in reply
    assert len(env.world.ledger("book")) == 1


def test_stop_in_unknown_with_no_answer_stays_unknown(env):
    to_proposal(env, book_outcomes=[OUTCOME_TIMEOUT_NOT_BOOKED, OUTCOME_SUCCESS], faults={"get_booking_status": 6})
    env.say("yes")
    reply = env.say("stop")
    assert env.run().state == State.UNKNOWN_NEEDS_CHECK
    assert "stop takes effect" in reply
    assert len(env.tool_calls("book")) == 1


def test_a_new_errand_cannot_start_while_an_outcome_is_unknown(env):
    to_proposal(env, book_outcomes=[OUTCOME_TIMEOUT_NOT_BOOKED], faults={"get_booking_status": 6})
    env.say("yes")
    env.say(REQUEST)
    assert env.run().id == "run-1"
    assert len(env.store.all_approvals()) == 1
