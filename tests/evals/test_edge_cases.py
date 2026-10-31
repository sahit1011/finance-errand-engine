"""Paths a code review found by hand-tracing. Each test failed before its fix."""

from datetime import datetime

import pytest
from harness import SimulatedCrash, details, find_plumber, intent
from test_slice2_safety import REQUEST, approval_statuses, to_proposal

from errand.contract import ToolError
from errand.core import approval as approval_module
from errand.core.domain import IllegalTransition, Limits, State
from errand.infra.clock import IST, FakeClock
from errand.mock.world import (
    OUTCOME_SLOT_TAKEN,
    OUTCOME_SUCCESS,
    OUTCOME_TIMEOUT_BOOKED,
    MockWorld,
    Scenario,
)


def rehold_fails(env) -> None:
    """Leaves a run in HOLDING with an approved attempt: the hold expired and the re-hold hit an unknown error."""
    env.allow_engine_error = True
    env.scenario.faults["hold_slot"] = 1
    env.clock.advance(minutes=11)
    env.say("yes")
    assert env.run().state == State.HOLDING
    assert env.store.open_attempt(env.run().id) is not None


def test_stop_during_a_rehold_never_books(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    rehold_fails(env)
    reply = env.say("stop")
    assert env.world.ledger("book") == []
    assert env.run().state == State.ABANDONED
    assert approval_statuses(env) == ["voided"]
    assert "Nothing was booked" in reply


def test_forget_during_a_rehold_does_not_retry_with_the_forgotten_value(env):
    env.start(Scenario(book_outcomes=[OUTCOME_SUCCESS]))
    env.say("remember my area is indiranagar")
    env.say("Find a plumber", intent("book", service="plumber", when="this_week", budget=3000))
    env.say("1")
    rehold_fails(env)
    env.say("forget my area")
    env.forgotten_values.append("indiranagar")
    assert env.world.ledger("book") == []
    assert approval_statuses(env) == ["voided"]


def test_a_proposal_never_shown_cannot_be_approved(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    env.store._db.execute("UPDATE approvals SET shown_reply_id = NULL")
    reply = env.say("yes", last_reply_id=None)
    assert env.world.ledger("book") == []
    assert approval_statuses(env) == ["pending"]
    assert "Please confirm" in reply


def test_a_refused_second_errand_leaves_the_active_one_untouched(env):
    to_proposal(env)
    env.say("Also cancel my electrician", intent("cancel", service="electrician"))
    run = env.run()
    assert run.detail("service") == "plumber"
    assert run.state == State.AWAITING_APPROVAL
    assert approval_statuses(env) == ["pending"]


def to_failed_first_hold(env, **config) -> None:
    env.allow_engine_error = True
    env.start(Scenario(faults={"hold_slot": 1}, **config))
    env.say(REQUEST, find_plumber())
    env.say("1")
    assert env.run().state == State.HOLDING


def test_correction_in_holding_uses_the_new_detail(env):
    to_failed_first_hold(env)
    env.say("Actually Koramangala", details(area="koramangala"))
    run = env.run()
    assert run.state == State.PRESENTING
    assert all("koramangala" in o["provider_id"] for o in run.options)
    env.say("1")
    assert "koramangala" in env.store.all_approvals()[0].provider_id


def test_memory_correction_in_holding_does_not_break_the_run(env):
    env.allow_engine_error = True
    env.start(Scenario(faults={"hold_slot": 1}))
    env.say("remember my area is indiranagar")
    env.say("Find a plumber", intent("book", service="plumber", when="this_week", budget=3000))
    env.say("1")
    assert env.run().state == State.HOLDING
    env.say("change my area to koramangala")
    run = env.run()
    assert run.state == State.PRESENTING
    assert all("koramangala" in o["provider_id"] for o in run.options)
    errors = [e for e in env.trace_events() if e["type"] == "error" and e["error_type"] != "ToolFault"]
    assert errors == []


def test_a_number_in_holding_gets_a_plain_answer(env):
    to_failed_first_hold(env)
    env.say("2")
    errors = [e for e in env.trace_events() if e["type"] == "error" and e["error_type"] != "ToolFault"]
    assert errors == []


def test_forgetting_a_fact_clears_its_pending_question(env):
    env.start(Scenario(book_outcomes=[OUTCOME_SUCCESS]))
    env.say("Find a plumber", find_plumber(earliest_time="11:00"))
    env.say("1")
    env.say("yes")
    env.say("Find a plumber", intent("book", service="plumber", area="indiranagar", when="this_week", budget=3000))
    assert env.run().pending_question["kind"] == "memory_confirm"
    assert env.run().pending_question["key"] == "earliest_time"
    env.say("forget my earliest time")
    assert (env.run().pending_question or {}).get("key") != "earliest_time"
    env.say("yes")
    fact = env.memory("earliest_time")
    assert fact["status"] == "forgotten" and fact["value"] is None


def test_a_crash_between_void_and_move_leaves_a_recoverable_run(env, monkeypatch):
    to_proposal(env, book_outcomes=[OUTCOME_SLOT_TAKEN, OUTCOME_SUCCESS])
    real = env.store.record_transition

    def crash_on_search(run_id, from_state, to_state, reason):
        if to_state == State.SEARCHING:
            raise SimulatedCrash("between void and move")
        real(run_id, from_state, to_state, reason)

    monkeypatch.setattr(env.store, "record_transition", crash_on_search)
    with pytest.raises(SimulatedCrash):
        env.say("yes")
    monkeypatch.undo()
    env.restart()
    run = env.run()
    assert run.state == State.PRESENTING
    assert approval_statuses(env) == ["voided"]
    env.say("1")
    env.say("yes")
    assert len(env.world.ledger("book")) == 1


def test_restart_rechecks_an_unknown_outcome(env):
    to_proposal(env, book_outcomes=[OUTCOME_TIMEOUT_BOOKED], faults={"get_booking_status": 3})
    env.say("yes")
    assert env.run().state == State.UNKNOWN_NEEDS_CHECK
    replies = env.restart()
    assert env.run().state == State.DONE
    assert any("Booked and verified" in r for r in replies)


def test_restart_shows_a_pending_proposal_again_and_it_can_be_approved(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    replies = env.restart()
    assert any("Please confirm" in r for r in replies)
    env.say("yes")
    assert len(env.world.ledger("book")) == 1


def test_restart_shows_pending_options_again(env):
    env.start()
    env.say(REQUEST, find_plumber())
    replies = env.restart()
    assert any("Reply with its number" in r for r in replies)


def test_a_crash_with_an_undelivered_proposal_shows_it_once(env):
    env.start()
    env.say(REQUEST, find_plumber())
    env.crash("1", "before_reply")
    replies = env.restart()
    assert len([r for r in replies if "Please confirm" in r]) == 1


def test_stop_when_status_is_failed_abandons_the_run(env):
    to_proposal(env, book_outcomes=[OUTCOME_SLOT_TAKEN], faults={"book": 1})
    env.allow_engine_error = True
    env.say("yes")
    assert env.run().state == State.UNKNOWN_NEEDS_CHECK
    # The fault left status not_found, so record the failure directly to reach this path.
    env.world._db.execute("INSERT INTO book_keys (key, status) VALUES (?, 'failed')", (env.store.open_attempt(env.run().id).idempotency_key,))
    env.say("stop")
    assert env.run().state == State.ABANDONED


def test_no_during_an_in_flight_write_is_reported_as_not_applied(env):
    to_proposal(env, book_outcomes=[OUTCOME_TIMEOUT_BOOKED], faults={"get_booking_status": 3})
    env.say("yes")
    reply = env.say("no")
    assert "Booked and verified" in reply and "not applied" in reply


def test_forget_during_a_change_does_not_retry(env):
    seeded = {"id": "b-1", "provider": "plumber-indiranagar-1", "service": "plumber", "slot": "2026-11-06T10:00", "status": "confirmed"}
    env.start(Scenario(seeded_bookings=[seeded], cancel_503_calls=99, faults={"list_bookings": 0}))
    env.say("remember my area is indiranagar")
    env.say("Cancel my plumber", intent("cancel", service="plumber"))
    env.scenario.faults["list_bookings"] = 3
    env.say("yes")
    assert env.run().state == State.UNKNOWN_NEEDS_CHECK
    calls = len(env.tool_calls("cancel"))
    env.say("forget my area")
    env.forgotten_values.append("indiranagar")
    assert len(env.tool_calls("cancel")) == calls
    assert env.run().state == State.FAILED


def test_inferring_a_fact_is_shown_to_the_user(env):
    env.start(Scenario(book_outcomes=[OUTCOME_SUCCESS]))
    env.say(REQUEST, find_plumber(earliest_time="11:00"))
    env.say("1")
    reply = env.say("yes")
    assert "might" in reply and "earliest time" in reply and "preferred technician" in reply


def test_confirming_and_declining_a_fact_are_shown_to_the_user(env):
    env.start(Scenario(book_outcomes=[OUTCOME_SUCCESS]))
    env.say(REQUEST, find_plumber(earliest_time="11:00"))
    env.say("1")
    env.say("yes")
    env.say("Find a plumber", intent("book", service="plumber", area="indiranagar", when="this_week", budget=3000))
    assert "kept" in env.say("yes").lower()
    env.say("stop")
    env.say("Find a plumber", intent("book", service="plumber", area="indiranagar", when="this_week", budget=3000))
    assert env.run().pending_question["key"] == "preferred_technician"
    assert "not keep" in env.say("no").lower()


def test_choosing_the_same_option_again_does_not_trip_over_the_users_own_hold(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    first = env.store.all_approvals()[0]
    env.say("no")
    reply = env.say("1")
    assert "no longer available" not in reply and "Please confirm" in reply
    assert env.store.all_approvals()[-1].slot == first.slot
    assert len(env.tool_calls("hold_slot")) == 1
    env.say("yes")
    assert len(env.world.ledger("book")) == 1


def test_repeating_an_option_number_keeps_the_same_proposal_terms(env):
    to_proposal(env)
    reply = env.say("1")
    assert "no longer available" not in reply and "Please confirm" in reply


def test_an_error_after_a_verified_booking_never_says_nothing_was_booked(env, monkeypatch):
    env.allow_engine_error = True
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])

    def broken(run, turn):
        raise RuntimeError("inference failed")

    monkeypatch.setattr(env.engine.memory, "infer_after_booking", broken)
    reply = env.say("yes")
    assert "Booked and verified" in reply
    assert "nothing was booked" not in reply.lower()


def test_a_stated_technician_who_is_not_listed_is_reported_and_others_shown(env):
    env.start()
    env.say("remember my technician is Ramesh")
    reply = env.say("Find a plumber", intent("book", service="plumber", area="indiranagar", when="this_week", budget=3000))
    assert env.run().state == State.PRESENTING
    assert "Ramesh" in reply and "Reply with its number" in reply


def test_a_fact_newly_stated_while_choosing_is_applied(env):
    env.start()
    env.say(REQUEST, find_plumber(latest_time="23:00"))
    env.say("remember my earliest time is 15:00")
    run = env.run()
    assert run.state == State.PRESENTING
    assert all(o["slot"][11:16] >= "15:00" for o in run.options)


def test_a_model_cannot_save_a_time_the_user_never_said(env):
    env.start()
    env.say(
        "hello",
        {"actions": [{"type": "propose_memory", "key": "earliest_time", "value": "06:00"}], "done": True},
        {"actions": [], "done": True},
    )
    assert env.memory("earliest_time") is None


def test_a_booked_slot_is_filtered_even_if_the_provider_still_lists_it(env, monkeypatch):
    seeded = {"id": "b-1", "provider": "plumber-indiranagar-7", "service": "electrician", "slot": "2026-11-02T11:00", "status": "confirmed"}
    env.start(Scenario(seeded_bookings=[seeded]))
    real = env.world.search_providers

    def stale_search(service, area, date_range):
        results = real(service, area, date_range)
        for provider in results:
            if provider["id"] == "plumber-indiranagar-7" and "2026-11-02T11:00" not in provider["free_slots"]:
                provider["free_slots"].insert(0, "2026-11-02T11:00")
        return results

    monkeypatch.setattr(env.world, "search_providers", stale_search)
    env.say(REQUEST, find_plumber())
    assert all((o["provider_id"], o["slot"]) != ("plumber-indiranagar-7", "2026-11-02T11:00") for o in env.run().options)


def test_review_text_is_not_kept_whole_in_the_reply_trace(env):
    env.start()
    env.say(REQUEST, find_plumber())
    env.say("Tell me about option 1", {"actions": [{"type": "request_read", "tool": "get_provider", "option": 1}], "done": True})
    replies = [e["text"] for e in env.trace_events() if e["type"] == "reply"]
    assert not any('Review: "' in text for text in replies)
    assert any("provider details" in text for text in replies)


def test_a_used_hold_cannot_book_again_under_a_new_key(tmp_path):
    world = MockWorld(str(tmp_path / "w.sqlite"), Scenario(book_outcomes=[OUTCOME_SUCCESS, OUTCOME_SUCCESS]), FakeClock(datetime(2026, 11, 2, 9, 0, tzinfo=IST)))
    provider = world.search_providers("plumber", "indiranagar", ("2026-11-02", "2026-11-08"))[0]
    hold = world.hold_slot(provider["id"], provider["free_slots"][0])
    world.book(hold["hold_id"], "key-1")
    with pytest.raises(ToolError) as error:
        world.book(hold["hold_id"], "key-2")
    assert error.value.code == "hold_expired"
    assert len(world.ledger("book")) == 1


def test_a_second_approval_of_the_same_record_is_refused(env):
    to_proposal(env)
    run = env.run()
    approval_id = run.pending_question["approval_id"]
    tracer = env.engine._new_turn("user-1", "", 0, run).tracer
    assert approval_module.approve(env.store, tracer, run, approval_id, 1) is True
    assert approval_module.approve(env.store, tracer, run, approval_id, 2) is False
    assert len([a for a in env.store.all_approvals() if a.status == "approved"]) == 1


def test_repeated_yes_while_the_outcome_is_unknown_never_writes_again(env):
    to_proposal(env, book_outcomes=[OUTCOME_TIMEOUT_BOOKED], faults={"get_booking_status": 6})
    env.say("yes")
    env.say("yes")
    assert env.run().state == State.UNKNOWN_NEEDS_CHECK
    assert len(env.tool_calls("book")) == 1
    env.say("yes")
    assert env.run().state == State.DONE
    assert len(env.tool_calls("book")) == 1
    assert len(env.world.ledger("book")) == 1


def test_the_per_run_time_cap_is_enforced(env):
    env.start(limits=Limits(run_seconds=100))
    env.say(REQUEST, find_plumber())
    env.clock.advance(seconds=30)

    def slow(model_input):
        env.clock.advance(seconds=101)
        return {"actions": [], "done": False}

    reply = env.say("hmm", slow)
    assert "time limit" in reply
    assert env.run().state == State.FAILED


def test_an_illegal_move_is_refused(env):
    env.start()
    env.say(REQUEST, find_plumber())
    run = env.run()
    tracer = env.engine._new_turn("user-1", "", 0, run).tracer
    with pytest.raises(IllegalTransition):
        approval_module.move(env.store, tracer, run, State.DONE, "skipping every check")

