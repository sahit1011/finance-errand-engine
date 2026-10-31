"""The mock must behave exactly as the assignment's fixed tool contract says."""

from datetime import datetime

import pytest

from errand.contract import ToolError, ToolTimeout, TransientError
from errand.infra.clock import IST, FakeClock
from errand.mock.world import (
    OUTCOME_SLOT_TAKEN,
    OUTCOME_SUCCESS,
    OUTCOME_TIMEOUT_BOOKED,
    OUTCOME_TIMEOUT_NOT_BOOKED,
    MockWorld,
    Scenario,
)

WEEK = ("2026-11-02", "2026-11-08")


def make_world(tmp_path, name="world.sqlite", **config):
    clock = FakeClock(datetime(2026, 11, 2, 9, 0, tzinfo=IST))
    return MockWorld(str(tmp_path / name), Scenario(**config), clock), clock


def first_option(world):
    provider = world.search_providers("plumber", "indiranagar", WEEK)[0]
    return provider, provider["free_slots"][0]


def test_search_returns_three_to_eight_providers_with_contract_fields(tmp_path):
    world, _ = make_world(tmp_path)
    results = world.search_providers("plumber", "indiranagar", WEEK)
    assert 3 <= len(results) <= 8
    assert set(results[0]) >= {"id", "price", "rating", "free_slots", "attributes"}


def test_search_is_reproducible_for_a_seed(tmp_path):
    first, _ = make_world(tmp_path, "a.sqlite", seed=11)
    second, _ = make_world(tmp_path, "b.sqlite", seed=11)
    assert first.search_providers("plumber", "koramangala", WEEK) == second.search_providers("plumber", "koramangala", WEEK)


def test_every_provider_has_a_review_with_an_injection_attempt(tmp_path):
    world, _ = make_world(tmp_path)
    for provider in world.search_providers("plumber", "indiranagar", WEEK):
        reviews = world.get_provider(provider["id"])["reviews"]
        assert any("ignore" in review.lower() for review in reviews)


def test_hold_returns_ten_minute_expiry_and_quote(tmp_path):
    world, clock = make_world(tmp_path)
    provider, slot = first_option(world)
    hold = world.hold_slot(provider["id"], slot)
    assert hold["quoted_price"] == provider["price"]
    assert datetime.fromisoformat(hold["expires_at"]) - clock.now() == (datetime(2026, 11, 2, 9, 10, tzinfo=IST) - clock.now())


def test_hold_quote_can_differ_from_search_price(tmp_path):
    world, _ = make_world(tmp_path, hold_price_deltas=[150])
    provider, slot = first_option(world)
    assert world.hold_slot(provider["id"], slot)["quoted_price"] == provider["price"] + 150


def test_holding_a_taken_slot_fails(tmp_path):
    world, _ = make_world(tmp_path)
    provider, slot = first_option(world)
    world.hold_slot(provider["id"], slot)
    with pytest.raises(ToolError) as error:
        world.hold_slot(provider["id"], slot)
    assert error.value.code == "slot_unavailable"


def test_book_success_and_replay_create_one_booking(tmp_path):
    world, _ = make_world(tmp_path, book_outcomes=[OUTCOME_SUCCESS])
    provider, slot = first_option(world)
    hold = world.hold_slot(provider["id"], slot)
    first = world.book(hold["hold_id"], "key-1")
    assert world.book(hold["hold_id"], "key-1") == first
    assert len(world.ledger("book")) == 1
    assert world.get_booking_status("key-1") == {"status": "booked", "booking_id": first["booking_id"]}


def test_slot_taken_is_stored_and_reported_as_failed(tmp_path):
    world, _ = make_world(tmp_path, book_outcomes=[OUTCOME_SLOT_TAKEN])
    provider, slot = first_option(world)
    hold = world.hold_slot(provider["id"], slot)
    with pytest.raises(ToolError) as error:
        world.book(hold["hold_id"], "key-1")
    assert error.value.code == "slot_taken"
    assert world.get_booking_status("key-1") == {"status": "failed"}
    assert world.ledger("book") == []


def test_timeout_after_success_books_and_status_finds_it(tmp_path):
    world, _ = make_world(tmp_path, book_outcomes=[OUTCOME_TIMEOUT_BOOKED])
    provider, slot = first_option(world)
    hold = world.hold_slot(provider["id"], slot)
    with pytest.raises(ToolTimeout):
        world.book(hold["hold_id"], "key-1")
    assert world.get_booking_status("key-1")["status"] == "booked"
    assert len(world.ledger("book")) == 1


def test_timeout_without_booking_stores_nothing_so_a_retry_gets_a_fresh_attempt(tmp_path):
    world, _ = make_world(tmp_path, book_outcomes=[OUTCOME_TIMEOUT_NOT_BOOKED, OUTCOME_SUCCESS])
    provider, slot = first_option(world)
    hold = world.hold_slot(provider["id"], slot)
    with pytest.raises(ToolTimeout):
        world.book(hold["hold_id"], "key-1")
    assert world.get_booking_status("key-1") == {"status": "not_found"}
    assert "booking_id" in world.book(hold["hold_id"], "key-1")


def test_expired_hold_fails_and_leaves_status_not_found(tmp_path):
    world, clock = make_world(tmp_path, book_outcomes=[OUTCOME_SUCCESS])
    provider, slot = first_option(world)
    hold = world.hold_slot(provider["id"], slot)
    clock.advance(minutes=11)
    with pytest.raises(ToolError) as error:
        world.book(hold["hold_id"], "key-1")
    assert error.value.code == "hold_expired"
    assert world.get_booking_status("key-1") == {"status": "not_found"}


def test_book_rates_match_the_contract_over_many_seeded_keys(tmp_path):
    world, _ = make_world(tmp_path)
    counts = {OUTCOME_SUCCESS: 0, OUTCOME_SLOT_TAKEN: 0, OUTCOME_TIMEOUT_BOOKED: 0, OUTCOME_TIMEOUT_NOT_BOOKED: 0}
    runs = 4000
    for index in range(runs):
        counts[world._book_outcome(f"rate-{index}")] += 1
    # Tolerance of 2.5 points is about 4 standard deviations at this sample size.
    assert abs(counts[OUTCOME_SUCCESS] / runs - 0.70) < 0.025
    assert abs(counts[OUTCOME_SLOT_TAKEN] / runs - 0.15) < 0.025
    assert abs(counts[OUTCOME_TIMEOUT_BOOKED] / runs - 0.075) < 0.025
    assert abs(counts[OUTCOME_TIMEOUT_NOT_BOOKED] / runs - 0.075) < 0.025


def test_cancel_fails_with_503_once_then_succeeds(tmp_path):
    seeded = [{"id": "b-1", "provider": "plumber-indiranagar-1", "service": "plumber", "slot": "2026-11-03T09:00", "status": "confirmed"}]
    world, _ = make_world(tmp_path, seeded_bookings=seeded)
    with pytest.raises(TransientError):
        world.cancel("b-1", "key-1")
    assert world.cancel("b-1", "key-1") == {"status": "cancelled"}
    assert world.cancel("b-1", "key-1") == {"status": "cancelled"}
    assert len(world.ledger("cancel")) == 1


def test_reschedule_conflicts_on_configured_slots(tmp_path):
    seeded = [{"id": "b-1", "provider": "plumber-indiranagar-1", "service": "plumber", "slot": "2026-11-03T09:00", "status": "confirmed"}]
    world, _ = make_world(tmp_path, seeded_bookings=seeded, conflict_slots={"2026-11-07T09:00"})
    with pytest.raises(ToolError) as error:
        world.reschedule("b-1", "2026-11-07T09:00", "key-1")
    assert error.value.code == "slot_conflict"
    assert world.reschedule("b-1", "2026-11-07T10:00", "key-2")["slot"] == "2026-11-07T10:00"


def test_world_state_survives_reopening_the_database(tmp_path):
    world, clock = make_world(tmp_path, book_outcomes=[OUTCOME_TIMEOUT_BOOKED])
    provider, slot = first_option(world)
    hold = world.hold_slot(provider["id"], slot)
    with pytest.raises(ToolTimeout):
        world.book(hold["hold_id"], "key-1")
    world.close()
    reopened = MockWorld(str(tmp_path / "world.sqlite"), Scenario(book_outcomes=[OUTCOME_TIMEOUT_BOOKED]), clock)
    assert reopened.get_booking_status("key-1")["status"] == "booked"
