"""Reschedule, cancel and the duplicate check."""

from harness import details, find_plumber, intent, scenario

from errand.core.domain import State
from errand.mock.world import OUTCOME_SUCCESS, Scenario

PLUMBER = "plumber-indiranagar-1"


def seeded(slot="2026-11-06T10:00", provider=PLUMBER, service="plumber", booking_id="b-1", status="confirmed") -> dict:
    return {"id": booking_id, "provider": provider, "service": service, "slot": slot, "status": status}


def booking(env, booking_id="b-1") -> dict:
    return next(b for b in env.world.list_bookings_unfaulted() if b["id"] == booking_id)


@scenario(18, "Reschedule slot_conflict", "The original booking stays, and a new slot needs new approval")
def test_reschedule_conflict(env):
    env.start(Scenario(seeded_bookings=[seeded()]))
    env.say("Move my plumber to Saturday", intent("reschedule", service="plumber", when="saturday", area="indiranagar"))
    run = env.run()
    assert run.state == State.PRESENTING
    assert {o["provider_id"] for o in run.options} == {PLUMBER}
    conflicted = run.options[0]["slot"]
    env.scenario.conflict_slots.add(conflicted)
    env.say("1")
    assert env.world.ledger("reschedule") == []
    reply = env.say("yes")
    assert "no longer available" in reply
    assert booking(env)["slot"] == "2026-11-06T10:00"
    assert env.world.ledger("reschedule") == []
    assert [a.status for a in env.store.all_approvals()] == ["voided"]
    assert all(o["slot"] != conflicted for o in env.run().options)
    next_slot = env.run().options[0]["slot"]
    env.say("1")
    reply = env.say("yes")
    assert booking(env)["slot"] == next_slot
    assert "Moved and verified" in reply
    env.record.notes = f"Conflict on {conflicted} left the booking unchanged; moved to {next_slot} only after a new approval"


@scenario(19, "Cancel with 503", "Same key retried, then verified cancelled")
def test_cancel_with_503(env):
    env.start(Scenario(seeded_bookings=[seeded()]))
    reply = env.say("Cancel my plumber on Friday", intent("cancel", service="plumber", when="friday", area="indiranagar"))
    assert env.run().state == State.AWAITING_APPROVAL
    assert "current price" in reply
    assert env.world.ledger("cancel") == []
    reply = env.say("yes")
    calls = env.tool_calls("cancel")
    assert len(calls) == 2
    assert calls[0]["error"] == "503"
    assert calls[0]["args"] == calls[1]["args"]
    assert booking(env)["status"] == "cancelled"
    assert len(env.world.ledger("cancel")) == 1
    assert "Cancelled and verified" in reply
    env.record.notes = "503 then success with the same key; list_bookings confirmed the cancellation"


def test_cancel_uses_the_price_saved_at_booking(env):
    env.start(Scenario(book_outcomes=[OUTCOME_SUCCESS]))
    env.say("Find a plumber", find_plumber())
    env.say("1")
    env.say("yes")
    booked = env.world.ledger("book")[0]["args"]
    reply = env.say("Cancel my plumber", intent("cancel", service="plumber"))
    assert env.store.all_approvals()[-1].price == booked["price"]
    assert "current price" not in reply


def test_no_to_a_cancel_keeps_the_booking(env):
    env.start(Scenario(seeded_bookings=[seeded()]))
    env.say("Cancel my plumber", intent("cancel", service="plumber", area="indiranagar"))
    reply = env.say("no")
    assert env.run().state == State.ABANDONED
    assert booking(env)["status"] == "confirmed"
    assert "stays" in reply


def test_several_matching_bookings_ask_which_one(env):
    env.start(Scenario(seeded_bookings=[seeded(), seeded("2026-11-05T10:00", booking_id="b-2")]))
    reply = env.say("Cancel my plumber", intent("cancel", service="plumber", area="indiranagar"))
    assert "more than one" in reply
    assert env.run().state == State.COLLECTING
    env.say("2")
    assert env.store.all_approvals()[0].booking_id == "b-1"


def test_no_matching_booking_ends_with_a_clear_message(env):
    env.start(Scenario())
    reply = env.say("Cancel my plumber", intent("cancel", service="plumber"))
    assert env.run().state == State.FAILED
    assert "could not find" in reply


@scenario(27, "Duplicate check", "An existing booking's provider and slot are not offered, and the same active service asks first")
def test_duplicate_check(env):
    env.start(Scenario(seeded_bookings=[seeded("2026-11-03T09:00", provider="plumber-indiranagar-7")], book_outcomes=[OUTCOME_SUCCESS]))
    reply = env.say("Find a plumber", find_plumber())
    assert "already have" in reply
    assert env.run().pending_question == {"kind": "duplicate_check"}
    assert env.tool_calls("search_providers") == []
    env.say("yes")
    assert env.run().state == State.PRESENTING
    assert all((o["provider_id"], o["slot"]) != ("plumber-indiranagar-7", "2026-11-03T09:00") for o in env.run().options)
    assert env.store.all_approvals() == []
    env.record.notes = "Asked before searching; after a yes, the slot already booked was not among the options"


def test_declining_a_second_booking_abandons_the_errand(env):
    env.start(Scenario(seeded_bookings=[seeded()]))
    env.say("Find a plumber", find_plumber())
    reply = env.say("no")
    assert env.run().state == State.ABANDONED
    assert env.world.ledger("hold_slot") == []
    assert "not booked" in reply


@scenario(30, "Cancel or reschedule retries used up", "list_bookings is checked, the booking is reported unchanged, and the approval is voided")
def test_cancel_retries_used_up(env):
    env.start(Scenario(seeded_bookings=[seeded()], cancel_503_calls=99))
    env.say("Cancel my plumber", intent("cancel", service="plumber", area="indiranagar"))
    reply = env.say("yes")
    assert len(env.tool_calls("cancel")) == 4
    assert booking(env)["status"] == "confirmed"
    assert env.run().state == State.FAILED
    assert [a.status for a in env.store.all_approvals()] == ["voided"]
    assert "unchanged" in reply and "verified" not in reply
    assert env.tool_calls("list_bookings")
    env.record.notes = "1 call plus 3 retries all failed; list_bookings showed no change; told the booking is unchanged"


def test_reschedule_retries_used_up_leaves_the_booking(env):
    env.start(Scenario(seeded_bookings=[seeded()], reschedule_503_calls=99))
    env.say("Move my plumber", intent("reschedule", service="plumber", when="saturday", area="indiranagar"))
    env.say("1")
    reply = env.say("yes")
    assert booking(env)["slot"] == "2026-11-06T10:00"
    assert env.run().state == State.FAILED
    assert "unchanged" in reply


def test_cancel_resumes_after_a_crash_without_a_second_effect(env):
    env.start(Scenario(seeded_bookings=[seeded()]))
    env.say("Cancel my plumber", intent("cancel", service="plumber", area="indiranagar"))
    env.crash("yes", "after_approve")
    replies = env.restart()
    assert booking(env)["status"] == "cancelled"
    assert len(env.world.ledger("cancel")) == 1
    assert any("Cancelled and verified" in r for r in replies)


@scenario(42, "Seeded cancel with no price available", "No approval or write, the run moves to FAILED, and the booking is unchanged")
def test_cancel_with_no_price_available(env):
    env.start(Scenario(seeded_bookings=[seeded(provider="plumber-indiranagar-99")]))
    reply = env.say("Cancel my plumber", intent("cancel", service="plumber", area="indiranagar"))
    assert env.run().state == State.FAILED
    assert env.store.all_approvals() == []
    assert booking(env)["status"] == "confirmed"
    assert "cannot find a price" in reply
    env.record.notes = "Provider not in search, so no price; no approval created; booking unchanged"


def test_cancel_asks_for_the_area_when_it_needs_a_current_price(env):
    env.start(Scenario(seeded_bookings=[seeded()]))
    reply = env.say("Cancel my plumber", intent("cancel", service="plumber"))
    assert "area" in reply
    env.say("Indiranagar", details(area="indiranagar"))
    assert env.run().state == State.AWAITING_APPROVAL
