"""Conversation state and memory across sessions."""

from harness import details, find_plumber, intent, scenario

from errand.core.domain import State
from errand.mock.world import OUTCOME_SUCCESS, OUTCOME_TIMEOUT_BOOKED, OUTCOME_TIMEOUT_NOT_BOOKED, Scenario


def complete_booking(env, **overrides) -> None:
    env.say("Find a plumber", find_plumber(**overrides))
    env.say("1")
    env.say("yes")
    assert env.run().state == State.DONE


@scenario(2, "Missing details", "Only what is missing is asked, and known facts are not asked again")
def test_missing_details_are_asked_once(env):
    env.start()
    reply = env.say("I need a plumber", intent("book", service="plumber"))
    assert "which area" in reply and "budget" in reply and "when" in reply
    assert "which service" not in reply
    reply = env.say("Indiranagar", details(area="indiranagar"))
    assert "which area" not in reply and "which service" not in reply
    assert "budget" in reply and "when" in reply
    reply = env.say("this week", details(when="this_week"))
    assert "budget" in reply
    assert "when you need" not in reply and "which area" not in reply
    env.say("Rs 1500", details(budget=1500))
    assert env.run().state == State.PRESENTING
    env.record.notes = "3 questions shrank as details arrived; no known fact was asked again; search ran once all were known"


def test_nothing_matching_names_the_dates_searched_so_a_misread_day_can_be_seen(env):
    env.start()
    env.say("Find a plumber on Monday for Rs 1", intent("book", service="plumber", area="indiranagar", when="monday", budget=1))
    reply = env.say("hello", {"actions": [], "done": True})
    assert "Nothing matched for Mon 2 Nov" in reply
    env.say("this week then", details(when="this_week"))
    reply = env.say("hello", {"actions": [], "done": True})
    assert "Nothing matched for Mon 2 Nov to Sun 8 Nov" in reply


@scenario(3, "Correction", "One detail changes and the rest stay")
def test_correction_changes_one_detail(env):
    env.start()
    env.say("Find a plumber", find_plumber())
    before = {key: env.run().detail(key) for key in ("service", "when", "budget", "latest_time")}
    reply = env.say("Actually Koramangala", details(area="koramangala"))
    run = env.run()
    assert run.detail("area") == "koramangala"
    assert {key: run.detail(key) for key in before} == before
    assert run.state == State.PRESENTING
    assert all("koramangala" in o["provider_id"] for o in run.options)
    assert "tell me" not in reply
    env.record.notes = "Area changed to Koramangala; service, time and budget kept; nothing re-asked; new options shown"


@scenario(4, "Same electrician as last time", "The right provider is used, and the 10 a.m. limit is not saved as stated")
def test_same_electrician_as_last_time(env):
    past = {"id": "b-past", "provider": "electrician-indiranagar-2", "service": "electrician", "slot": "2026-10-20T11:00", "status": "completed"}
    env.start(Scenario(seeded_bookings=[past], book_outcomes=[OUTCOME_SUCCESS]))
    env.say(
        "Book the same electrician as last time, but not before 10 a.m.",
        intent("book", service="electrician", provider="same_as_last", earliest_time="10 a.m.", area="indiranagar", when="this_week", budget="no_limit"),
    )
    run = env.run()
    assert run.state == State.PRESENTING
    assert {o["provider_id"] for o in run.options} == {"electrician-indiranagar-2"}
    assert all(o["slot"][11:16] >= "10:00" for o in run.options)
    env.say("1")
    env.say("yes")
    fact = env.memory("earliest_time")
    assert fact is not None and fact["provenance"] == "inferred"
    assert env.world.ledger("book")[0]["args"]["provider_id"] == "electrician-indiranagar-2"
    env.record.notes = "Provider found in list_bookings; options all at or after 10:00; the limit saved only as inferred"


@scenario(8, "Yes, but make it 11", "Treated as a correction, with no write")
def test_yes_but_is_a_correction(env):
    env.start(Scenario(book_outcomes=[OUTCOME_SUCCESS]))
    env.say("Find a plumber", find_plumber())
    env.say("1")
    env.say("yes, but make it after 11", details(earliest_time="11:00"))
    assert env.world.ledger("book") == []
    assert [a.status for a in env.store.all_approvals()] == ["voided"]
    assert env.run().detail("earliest_time") == "11:00"
    assert env.run().state == State.PRESENTING
    env.record.notes = "No approval from 'yes, but'; the pending proposal was voided and options were rebuilt"


@scenario(20, "Memory correct and forget", "The old value and the forgotten value are not reused, across sessions")
def test_memory_correct_and_forget(env):
    env.start(Scenario(book_outcomes=[OUTCOME_SUCCESS, OUTCOME_SUCCESS]))
    env.say("remember my area is indiranagar")
    assert env.memory("area")["provenance"] == "stated"
    assert "indiranagar (stated)" in env.say("what do you remember about me")
    env.say("change my area to koramangala")
    env.restart()
    reply = env.say("Find a plumber", intent("book", service="plumber", when="this_week", budget=2000))
    assert "koramangala" in reply
    assert env.tool_calls("search_providers")[-1]["args"][1] == "koramangala"
    env.say("stop")
    env.say("forget my area")
    env.forgotten_values.append("koramangala")
    assert env.memory("area")["value"] is None
    assert "not keeping" in env.say("show memory")
    env.restart()
    reply = env.say("Find a plumber", intent("book", service="plumber", when="this_week", budget=2000))
    assert "which area" in reply
    assert env.run().detail("area") is None
    env.record.notes = "Corrected area used without asking; after forget, the next errand asked for the area and kept no value"


def test_correcting_a_memory_value_voids_a_proposal_that_used_it(env):
    env.start()
    env.say("remember my area is indiranagar")
    env.say("Find a plumber", intent("book", service="plumber", when="this_week", budget=3000))
    env.say("1")
    assert env.run().state == State.AWAITING_APPROVAL
    env.say("change my area to koramangala")
    assert [a.status for a in env.store.all_approvals()] == ["voided"]
    assert env.run().state == State.PRESENTING
    assert all("koramangala" in o["provider_id"] for o in env.run().options)


def test_forgetting_a_memory_value_voids_a_proposal_that_used_it(env):
    env.start()
    env.say("remember my area is indiranagar")
    env.say("Find a plumber", intent("book", service="plumber", when="this_week", budget=3000))
    env.say("1")
    reply = env.say("forget my area")
    assert [a.status for a in env.store.all_approvals()] == ["voided"]
    assert env.run().state == State.COLLECTING
    assert "which area" in reply
    env.forgotten_values.append("indiranagar")


def test_memory_command_during_a_pending_approval_shows_the_proposal_again(env):
    env.start()
    env.say("Find a plumber", find_plumber())
    env.say("1")
    reply = env.say("show memory")
    assert "Please confirm" in reply
    assert [a.status for a in env.store.all_approvals()] == ["pending"]
    env.say("yes")


def test_a_model_cannot_mark_a_fact_as_stated_without_the_users_words(env):
    env.start()
    env.say("Find a plumber in Koramangala", {"actions": [{"type": "propose_memory", "key": "area", "value": "koramangala"}], "done": True})
    assert env.memory("area")["provenance"] == "inferred"


def test_a_model_cannot_save_a_value_the_user_never_said(env):
    env.start()
    reply = env.say(
        "hello",
        {"actions": [{"type": "propose_memory", "key": "preferred_technician", "value": "plumber-indiranagar-1"}], "done": True},
        {"actions": [{"type": "propose_memory", "key": "area", "value": "whitefield"}], "done": True},
    )
    assert env.memory("preferred_technician") is None and env.memory("area") is None
    assert "did not understand" in reply


@scenario(21, "Inferred fact", "Not used until the user confirms it")
def test_inferred_fact_needs_confirmation(env):
    env.start(Scenario(book_outcomes=[OUTCOME_SUCCESS]))
    complete_booking(env, earliest_time="11:00")
    assert env.memory("earliest_time")["provenance"] == "inferred"
    calls_before = len(env.tool_calls("search_providers"))
    reply = env.say("Find a plumber", intent("book", service="plumber", area="indiranagar", when="this_week", budget=3000))
    assert "might be 11:00" in reply
    assert env.run().detail("earliest_time") is None
    assert len(env.tool_calls("search_providers")) == calls_before
    env.say("yes")
    assert env.memory("earliest_time")["provenance"] == "stated"
    assert env.run().detail("earliest_time") == "11:00"
    assert all(o["slot"][11:16] >= "11:00" for o in env.run().options)
    env.record.notes = "Asked before using the inferred 11:00; after a yes it became stated and filtered the options"


def test_declining_an_inferred_fact_means_it_is_not_used_or_asked_again(env):
    env.start(Scenario(book_outcomes=[OUTCOME_SUCCESS]))
    complete_booking(env, earliest_time="11:00")
    env.say("Find a plumber", intent("book", service="plumber", area="indiranagar", when="this_week", budget=3000))
    env.say("no")
    assert env.run().detail("earliest_time") is None
    assert env.memory("earliest_time")["status"] == "rejected"
    assert env.run().state != State.ABANDONED


@scenario(22, "Forgotten fact and booking history", "Not inferred again from booking history")
def test_forgotten_fact_is_not_reinferred(env):
    env.start(Scenario(book_outcomes=[OUTCOME_SUCCESS, OUTCOME_SUCCESS]))
    complete_booking(env)
    technician = env.memory("preferred_technician")
    assert technician["provenance"] == "inferred"
    env.say("forget my preferred technician")
    env.say("Find a carpenter", intent("book", service="carpenter", area="indiranagar", when="this_week", budget="no_limit"))
    env.say("1")
    env.say("yes")
    assert env.run().state == State.DONE
    fact = env.memory("preferred_technician")
    assert fact["status"] == "forgotten" and fact["value"] is None
    env.record.notes = "A later booking did not bring the forgotten technician back; the fact stayed forgotten with no value"


def test_stating_a_forgotten_fact_lifts_the_marker(env):
    env.start()
    env.say("forget my area")
    env.say("remember my area is whitefield")
    fact = env.memory("area")
    assert (fact["value"], fact["provenance"], fact["status"]) == ("whitefield", "stated", "active")


@scenario(34, "Yes to a memory question before any proposal", "Confirms the fact only, with no approval record or hold")
def test_yes_to_memory_question_is_not_an_approval(env):
    env.start(Scenario(book_outcomes=[OUTCOME_SUCCESS]))
    complete_booking(env, earliest_time="11:00")
    approvals_before = len(env.store.all_approvals())
    holds_before = len(env.world.ledger("hold_slot"))
    env.say("Find a plumber", intent("book", service="plumber", area="indiranagar", when="this_week", budget=3000))
    assert env.run().pending_question["kind"] == "memory_confirm"
    env.say("yes")
    assert env.memory("earliest_time")["provenance"] == "stated"
    assert len(env.store.all_approvals()) == approvals_before
    assert len(env.world.ledger("hold_slot")) == holds_before
    assert len(env.world.ledger("book")) == 1
    env.record.notes = "The yes confirmed the memory fact; no new approval, hold or booking"


@scenario(41, "Forget during an in-flight write", "No retry using the forgotten value, and a confirmed booking is still reported")
def test_forget_during_in_flight_write(env):
    env.start(Scenario(book_outcomes=[OUTCOME_TIMEOUT_NOT_BOOKED, OUTCOME_SUCCESS], faults={"get_booking_status": 3}))
    env.say("remember my area is indiranagar")
    env.say("Find a plumber", intent("book", service="plumber", when="this_week", budget=3000))
    env.say("1")
    env.say("yes")
    assert env.run().state == State.UNKNOWN_NEEDS_CHECK
    reply = env.say("forget my area")
    env.forgotten_values.append("indiranagar")
    assert env.memory("area")["value"] is None
    assert len(env.tool_calls("book")) == 1
    assert env.world.ledger("book") == []
    assert "Nothing was booked" in reply
    assert env.run().state == State.COLLECTING
    env.record.notes = "Memory changed at once; status not_found; approval voided with no retry; the errand asks for the area again"


def test_forget_during_write_that_already_booked_still_reports_it(env):
    env.start(Scenario(book_outcomes=[OUTCOME_TIMEOUT_BOOKED], faults={"get_booking_status": 3}))
    env.say("remember my area is indiranagar")
    env.say("Find a plumber", intent("book", service="plumber", when="this_week", budget=3000))
    env.say("1")
    env.say("yes")
    reply = env.say("forget my area")
    assert "Booked and verified" in reply
    assert env.run().state == State.DONE
    assert env.memory("area")["status"] == "forgotten"
