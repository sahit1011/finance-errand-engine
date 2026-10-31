"""Paths a third review found by tracing what a real model can send, which earlier scripted tests never tried."""

from harness import details, find_plumber, intent, scenario
from test_edge_cases import rehold_fails
from test_slice2_safety import approval_statuses, to_proposal

from errand.core.domain import ModelUnavailable, State
from errand.core.render import NOTHING_TO_SAY
from errand.mock.world import OUTCOME_SUCCESS, OUTCOME_TIMEOUT_NOT_BOOKED, Scenario

PLUMBER = {"id": "b-p", "provider": "plumber-indiranagar-1", "service": "plumber", "slot": "2026-11-06T10:00", "status": "confirmed"}
ELECTRICIAN = {"id": "b-e", "provider": "electrician-indiranagar-1", "service": "electrician", "slot": "2026-11-05T10:00", "status": "confirmed"}
NOTHING = {"actions": [], "done": True}
ANY_PLUMBER = {"service": "plumber", "area": "indiranagar", "when": "next_week", "budget": "no_limit"}


def status_of(env, booking_id: str) -> str:
    return next(b["status"] for b in env.world.list_bookings_unfaulted() if b["id"] == booking_id)


def remember(key: str, value: str) -> dict:
    return {"actions": [{"type": "propose_memory", "key": key, "value": value}], "done": True}


# A stop must survive until the outcome is known.


def stop_while_status_is_unknown(env) -> None:
    env.start(Scenario(book_outcomes=[OUTCOME_TIMEOUT_NOT_BOOKED, OUTCOME_SUCCESS], faults={"get_booking_status": 6}))
    env.say("Find a plumber", find_plumber())
    env.say("1")
    env.say("yes")
    assert env.run().state == State.UNKNOWN_NEEDS_CHECK
    reply = env.say("stop")
    assert "takes effect once it is known" in reply


def test_a_stop_given_while_the_outcome_is_unknown_still_applies_on_the_next_message(env):
    stop_while_status_is_unknown(env)
    env.say("ok thanks")
    assert env.world.ledger("book") == []
    assert len(env.tool_calls("book")) == 1
    assert env.run().state == State.ABANDONED


def test_a_stop_given_while_the_outcome_is_unknown_survives_a_restart(env):
    stop_while_status_is_unknown(env)
    env.restart()
    assert env.world.ledger("book") == []
    assert len(env.tool_calls("book")) == 1
    assert env.run().state == State.ABANDONED


# The duplicate check belongs to the service being asked for now.


def test_correcting_the_service_during_the_duplicate_question_asks_about_the_new_service(env):
    env.start(Scenario(seeded_bookings=[PLUMBER, ELECTRICIAN]))
    env.say("Find a plumber", intent("book", **ANY_PLUMBER))
    reply = env.say("actually an electrician", details(service="electrician"))
    assert "booking for electrician" in reply
    assert env.run().pending_question == {"kind": "duplicate_check"}


def test_correcting_to_a_service_with_no_booking_clears_the_duplicate_question(env):
    env.start(Scenario(seeded_bookings=[PLUMBER]))
    env.say("Find a plumber", intent("book", **ANY_PLUMBER))
    env.say("actually a carpenter", details(service="carpenter"))
    run = env.run()
    assert run.pending_question is None and run.state == State.PRESENTING
    assert all(o["provider_id"].startswith("carpenter") for o in run.options)


def test_clearing_the_duplicate_check_for_one_service_does_not_clear_it_for_another(env):
    env.start(Scenario(seeded_bookings=[PLUMBER, ELECTRICIAN]))
    env.say("Find a plumber", intent("book", **ANY_PLUMBER))
    env.say("yes")
    assert env.run().state == State.PRESENTING
    reply = env.say("actually an electrician", details(service="electrician"))
    assert "booking for electrician" in reply


def test_choosing_then_correcting_in_one_output_still_runs_the_duplicate_check(env):
    env.start(Scenario(seeded_bookings=[ELECTRICIAN]))
    env.say("Find a plumber", intent("book", **ANY_PLUMBER))
    output = {"actions": [{"type": "select_option", "option": 1}, {"type": "update_details", "details": {"service": "electrician"}}], "done": True}
    reply = env.say("that one, no wait, an electrician", output)
    assert "booking for electrician" in reply
    assert env.world.ledger("hold_slot") == []


# A correction must change which booking is targeted.


def test_correcting_the_service_during_a_cancel_proposal_changes_the_booking(env):
    env.start(Scenario(seeded_bookings=[PLUMBER, ELECTRICIAN]))
    env.say("Cancel my plumber", intent("cancel", service="plumber", area="indiranagar"))
    assert env.store.all_approvals()[-1].booking_id == "b-p"
    output = {"actions": [{"type": "set_intent", "intent": "cancel"}, {"type": "update_details", "details": {"service": "electrician"}}], "done": True}
    env.say("no wait, cancel my electrician instead", output)
    assert env.store.all_approvals()[-1].booking_id == "b-e"
    assert approval_statuses(env)[0] == "voided"
    env.say("yes")
    assert status_of(env, "b-e") == "cancelled"
    assert status_of(env, "b-p") == "confirmed"


def test_correcting_the_day_during_a_cancel_proposal_changes_the_booking(env):
    thursday = {**PLUMBER, "id": "b-thu", "slot": "2026-11-05T10:00"}
    env.start(Scenario(seeded_bookings=[PLUMBER, thursday]))
    env.say("Cancel my plumber on Friday", intent("cancel", service="plumber", when="friday", area="indiranagar"))
    assert env.store.all_approvals()[-1].booking_id == "b-p"
    env.say("sorry, Thursday", details(when="thursday"))
    assert env.store.all_approvals()[-1].booking_id == "b-thu"


def test_correcting_the_service_during_a_reschedule_changes_the_booking(env):
    env.start(Scenario(seeded_bookings=[PLUMBER, ELECTRICIAN]))
    env.say("Move my plumber to next week", intent("reschedule", service="plumber", when="next_week", area="indiranagar"))
    assert env.run().target_booking["id"] == "b-p"
    env.say("I meant the electrician", details(service="electrician"))
    run = env.run()
    assert run.target_booking["id"] == "b-e"
    assert {o["provider_id"] for o in run.options} == {"electrician-indiranagar-1"}


# Time passes while the user is away.


def test_a_proposal_for_a_slot_that_has_passed_cannot_be_approved(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    env.clock.advance(days=7)
    reply = env.say("yes")
    assert env.world.ledger("book") == []
    assert approval_statuses(env) == ["voided"]
    assert "passed" in reply


def test_a_stale_option_number_cannot_hold_a_slot_that_has_passed(env):
    env.start(Scenario())
    env.say("Find a plumber", find_plumber())
    env.clock.advance(days=7)
    env.say("1")
    assert env.world.ledger("hold_slot") == []


def test_a_restart_does_not_show_options_that_have_passed(env):
    env.start(Scenario())
    env.say("Find a plumber", find_plumber())
    env.clock.advance(days=7)
    env.restart()
    now = env.clock.now().strftime("%Y-%m-%dT%H:%M")
    assert env.run().options and all(o["slot"] > now for o in env.run().options)


def test_a_re_hold_stops_when_the_approved_slot_has_passed(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    rehold_fails(env)
    env.clock.advance(days=7)
    env.say("ok")
    assert env.world.ledger("book") == []
    assert len(env.world.ledger("hold_slot")) == 1
    assert approval_statuses(env) == ["voided"]


def test_a_cancel_proposal_for_a_booking_now_in_the_past_does_not_cancel(env):
    env.start(Scenario(seeded_bookings=[PLUMBER]))
    env.say("Cancel my plumber", intent("cancel", service="plumber", area="indiranagar"))
    env.clock.advance(days=30)
    env.say("yes")
    assert env.world.ledger("cancel") == []
    assert status_of(env, "b-p") == "confirmed"


def test_stop_still_works_on_a_proposal_that_has_passed(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    env.clock.advance(days=7)
    env.say("stop")
    assert env.run().state == State.ABANDONED


# A model can only save what the user actually said.


def test_a_model_cannot_turn_nine_am_into_nine_pm(env):
    env.start()
    env.say("I always want visits after 9 am", remember("earliest_time", "21:00"), remember("earliest_time", "21:00"))
    assert env.memory("earliest_time") is None


def test_a_model_cannot_add_minutes_the_user_never_said(env):
    env.start()
    env.say("always after 10", remember("earliest_time", "10:30"), remember("earliest_time", "10:30"))
    assert env.memory("earliest_time") is None


def test_times_the_user_did_say_are_saved(env):
    env.start()
    env.say("always after 2 p.m.", remember("earliest_time", "14:00"))
    assert env.memory("earliest_time")["value"] == "14:00"
    env.say("always after 10", remember("earliest_time", "10:00"))
    assert env.memory("earliest_time")["value"] == "10:00"


def test_an_empty_or_partial_technician_name_is_not_saved(env):
    env.start()
    for value in ("", "a", "Ra"):
        env.say("Always use Ravi please", remember("preferred_technician", value), remember("preferred_technician", value))
        assert env.memory("preferred_technician") is None, value
    env.say("Always use Ravi please", remember("preferred_technician", "Ravi"))
    assert env.memory("preferred_technician")["value"] == "Ravi"


# Memory changes and an empty search.


def test_changing_a_saved_limit_after_nothing_matched_searches_again(env):
    env.start()
    env.say("remember my earliest time is 23:00")
    env.say("Find a plumber", intent("book", **ANY_PLUMBER))
    calls = len(env.tool_calls("search_providers"))
    assert env.run().options == []
    reply = env.say("change my earliest time to 9")
    assert len(env.tool_calls("search_providers")) == calls + 1
    assert "Here are the options" in reply


def test_forgetting_a_saved_limit_after_nothing_matched_searches_again(env):
    env.start()
    env.say("remember my earliest time is 23:00")
    env.say("Find a plumber", intent("book", **ANY_PLUMBER))
    reply = env.say("forget my earliest time")
    assert "Here are the options" in reply


# Same as last time.


def test_same_as_last_time_waits_for_the_service_instead_of_being_lost(env):
    past = {"id": "b-old", "provider": "electrician-indiranagar-2", "service": "electrician", "slot": "2026-10-20T11:00", "status": "completed"}
    env.start(Scenario(seeded_bookings=[past]))
    reply = env.say("the same one as last time", intent("book", provider="same_as_last"))
    assert "None" not in reply and "which service" in reply
    env.say("electrician in Indiranagar next week, no limit", details(service="electrician", area="indiranagar", when="next_week", budget="no_limit"))
    assert {o["provider_id"] for o in env.run().options} == {"electrician-indiranagar-2"}


def test_same_as_last_time_beats_a_saved_technician(env):
    past = {"id": "b-old", "provider": "plumber-indiranagar-2", "service": "plumber", "slot": "2026-10-20T11:00", "status": "completed"}
    env.start(Scenario(seeded_bookings=[past]))
    env.say("remember my technician is plumber-indiranagar-3")
    env.say("same plumber as last time", intent("book", provider="same_as_last", **ANY_PLUMBER))
    assert {o["provider_id"] for o in env.run().options} == {"plumber-indiranagar-2"}


# An output is applied whole or not at all.


def test_a_rejected_output_does_not_apply_the_actions_before_the_bad_one(env):
    env.start()
    env.say("Find a plumber", find_plumber())
    bad = {"actions": [{"type": "select_option", "option": 1}, {"type": "propose_memory", "key": "area", "value": "mars"}], "done": True}
    reply = env.say("the first one, and remember mars", bad, bad)
    assert "did not understand" in reply
    assert env.run().state == State.PRESENTING
    assert env.world.ledger("hold_slot") == []
    assert "Please confirm" not in reply


def test_a_rejected_output_does_not_keep_the_details_before_the_bad_one(env):
    env.start()
    env.say("Find a plumber", find_plumber())
    bad = {"actions": [{"type": "update_details", "details": {"budget": 700}}, {"type": "update_details", "details": {"when": "whenever"}}], "done": True}
    env.say("700, whenever", bad, bad)
    assert env.run().detail("budget") == 1500


def test_two_selections_in_one_output_are_rejected(env):
    env.start()
    env.say("Find a plumber", find_plumber())
    bad = {"actions": [{"type": "select_option", "option": 1}, {"type": "select_option", "option": 2}], "done": True}
    env.say("one or two", bad, bad)
    assert env.run().state == State.PRESENTING
    assert env.world.ledger("hold_slot") == []


def test_a_different_errand_in_any_position_leaves_the_run_alone(env):
    env.start()
    env.say("Find a plumber", find_plumber())
    output = {"actions": [{"type": "update_details", "details": {"budget": 700}}, {"type": "set_intent", "intent": "cancel"}], "done": True}
    reply = env.say("cancel it, budget 700", output)
    assert "still working on your book request" in reply
    assert env.run().detail("budget") == 1500


def test_details_listed_before_the_intent_still_start_the_errand(env):
    env.start()
    output = {"actions": [{"type": "update_details", "details": {"service": "plumber"}}, {"type": "set_intent", "intent": "book"}], "done": True}
    env.say("plumber, book it", output)
    assert env.run().detail("service") == "plumber"


def test_details_with_no_errand_at_all_are_rejected(env):
    env.start()
    bad = {"actions": [{"type": "update_details", "details": {"service": "plumber"}}], "done": True}
    reply = env.say("plumber", bad, bad)
    assert "did not understand" in reply and env.run() is None


# Answering a memory question.


def test_stating_a_value_replaces_the_question_about_it(env):
    env.start()
    env.say("I live in Whitefield", remember("area", "whitefield"))
    env.say("Find a plumber", intent("book", service="plumber", when="next_week", budget="no_limit"))
    assert env.run().pending_question == {"kind": "memory_confirm", "key": "area"}
    env.say("change my area to koramangala")
    assert env.run().pending_question is None
    assert env.run().detail("area") == "koramangala"
    env.say("no")
    fact = env.memory("area")
    assert (fact["value"], fact["provenance"], fact["status"]) == ("koramangala", "stated", "active")


# Smaller gaps.


def test_one_output_cannot_show_the_same_provider_several_times(env):
    env.start()
    env.say("Find a plumber", find_plumber())
    read = {"type": "request_read", "tool": "get_provider", "option": 2}
    reply = env.say("reviews of two", {"actions": [read, read, read], "done": True})
    assert len(env.tool_calls("get_provider")) == 1
    assert reply.count("Attributes") <= 1 and reply.count("withheld") == 1


def test_a_blocked_memory_proposal_still_gets_a_useful_reply(env):
    env.start()
    env.say("forget my area")
    reply = env.say("I live in Whitefield", remember("area", "whitefield"))
    assert reply != NOTHING_TO_SAY
    assert env.memory("area")["value"] is None


def _model_down(_input):
    raise ModelUnavailable("stand-in outage")


def test_an_outage_at_a_proposal_shows_it_again_so_the_next_yes_counts(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    reply = env.say("tell me more about this", _model_down)
    assert "could not reach my local model" in reply and "Please confirm" in reply
    assert env.world.ledger("book") == []
    env.say("yes")
    assert len(env.world.ledger("book")) == 1


def test_a_question_is_not_an_approval(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    for text in ("book it?", "ok?", "yes?"):
        reply = env.say(text, NOTHING)
        assert "Please confirm" in reply
    assert env.world.ledger("book") == []
    assert approval_statuses(env) == ["pending"]


# Replies in Hindi. Approval stays a code match, and a model can only ever stop an errand.


@scenario(43, "Bare Hindi approval", "Approves the exact terms once in code, with no model; a condition or a question does not approve")
def test_bare_hindi_approval(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    for text in ("haan?", "theek hai?"):
        env.say(text, NOTHING)
        assert env.world.ledger("book") == []
    env.say("haan but after 11", details(earliest_time="11:00"))
    assert env.world.ledger("book") == []
    assert approval_statuses(env) == ["voided"]
    env.say("1")
    calls_before = len(env.model.inputs)
    reply = env.say("haan")
    assert len(env.model.inputs) == calls_before
    assert len(env.world.ledger("book")) == 1
    assert "Booked and verified" in reply
    env.record.notes = "Questions and a condition did not approve; bare haan approved in code, with no model call and one booking"


@scenario(44, "Stop understood by a model", "Abandons with the proposal voided and no write; a later yes approves nothing")
def test_stop_understood_by_a_model(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    reply = env.say("I changed my mind, drop all of this", {"actions": [{"type": "stop"}], "done": True})
    assert env.run().state == State.ABANDONED
    assert approval_statuses(env) == ["voided"]
    assert "Stopped" in reply
    env.say("yes")
    assert env.world.ledger("book") == []
    env.record.notes = "The model's stop abandoned the run and voided the proposal; the later yes found nothing to approve"


def test_a_hindi_stop_needs_no_model(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    calls_before = len(env.model.inputs)
    env.say("ruk jao")
    assert len(env.model.inputs) == calls_before
    assert env.run().state == State.ABANDONED
    assert approval_statuses(env) == ["voided"]


def test_a_hindi_stop_applies_while_the_outcome_is_unknown(env):
    env.start(Scenario(book_outcomes=[OUTCOME_TIMEOUT_NOT_BOOKED, OUTCOME_SUCCESS], faults={"get_booking_status": 6}))
    env.say("Find a plumber", find_plumber())
    env.say("1")
    env.say("yes")
    assert "takes effect once it is known" in env.say("rehne do")
    env.say("ok")
    assert env.world.ledger("book") == []
    assert env.run().state == State.ABANDONED


def test_a_hindi_no_declines_the_proposal(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    env.say("nahi")
    assert approval_statuses(env) == ["voided"]
    assert env.run().state == State.PRESENTING
    assert env.world.ledger("book") == []


def test_a_stop_mixed_with_other_actions_is_rejected_whole(env):
    env.start()
    env.say("Find a plumber", find_plumber())
    bad = {"actions": [{"type": "stop"}, {"type": "update_details", "details": {"budget": 700}}], "done": True}
    reply = env.say("stop, and make it 700", bad, bad)
    assert "did not understand" in reply
    assert env.run().state == State.PRESENTING
    assert env.run().detail("budget") == 1500


def test_a_model_stop_with_no_errand_says_so(env):
    env.start()
    reply = env.say("forget it all", {"actions": [{"type": "stop"}], "done": True})
    assert "nothing in progress" in reply
    assert env.run() is None


def test_a_model_stop_during_a_reschedule_leaves_the_booking(env):
    env.start(Scenario(seeded_bookings=[PLUMBER]))
    env.say("Move my plumber", intent("reschedule", service="plumber", when="next_week", area="indiranagar"))
    env.say("1")
    env.say("drop this whole thing", {"actions": [{"type": "stop"}], "done": True})
    assert env.world.ledger("reschedule") == []
    assert env.run().state == State.ABANDONED
    assert status_of(env, "b-p") == "confirmed"


# A confirmation that names one action cannot approve a different one.


def test_words_naming_booking_cannot_approve_a_cancel(env):
    env.start(Scenario(seeded_bookings=[PLUMBER]))
    env.say("Cancel my plumber", intent("cancel", service="plumber", area="indiranagar"))
    for text in ("book it", "please book it", "book kar do"):
        reply = env.say(text)
        assert "Please confirm" in reply and "Cancel" in reply
        assert env.world.ledger("cancel") == []
        assert status_of(env, "b-p") == "confirmed"
    assert approval_statuses(env) == ["pending"]
    env.say("yes")
    assert status_of(env, "b-p") == "cancelled"


def test_words_naming_booking_cannot_approve_a_reschedule(env):
    env.start(Scenario(seeded_bookings=[PLUMBER]))
    env.say("Move my plumber", intent("reschedule", service="plumber", when="next_week", area="indiranagar"))
    env.say("1")
    env.say("book it")
    assert env.world.ledger("reschedule") == []
    assert approval_statuses(env) == ["pending"]


def test_words_naming_booking_do_approve_a_booking(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    env.say("book kar do")
    assert len(env.world.ledger("book")) == 1


def test_a_hindi_stop_with_a_danda_prevents_the_retry_while_the_outcome_is_unknown(env):
    env.start(Scenario(book_outcomes=[OUTCOME_TIMEOUT_NOT_BOOKED, OUTCOME_SUCCESS], faults={"get_booking_status": 3}))
    env.say("Find a plumber", find_plumber())
    env.say("1")
    env.say("yes")
    env.say("रुक जाओ।")
    assert env.world.ledger("book") == []
    assert env.run().state == State.ABANDONED


def test_a_model_that_keeps_going_after_a_stop_is_not_called_again(env):
    to_proposal(env, book_outcomes=[OUTCOME_SUCCESS])
    calls = len(env.model.inputs)
    reply = env.say("drop it all", {"actions": [{"type": "stop"}], "done": False}, {"actions": [{"type": "stop"}], "done": True})
    assert len(env.model.inputs) == calls + 1
    assert env.run().state == State.ABANDONED
    assert "went wrong" not in reply
