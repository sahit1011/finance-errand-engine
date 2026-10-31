from harness import find_plumber, scenario

from errand.core.domain import State
from errand.mock.world import OUTCOME_SUCCESS, Scenario


@scenario(1, "Happy-path booking", "One booking in the ledger at the approved price, reported only after verification")
def test_happy_path_booking(env):
    env.start(Scenario(book_outcomes=[OUTCOME_SUCCESS]))
    env.say("Find a plumber for my kitchen tap this week, mornings only, in Indiranagar, under Rs 1500", find_plumber())
    assert env.run().state == State.PRESENTING
    option = env.run().options[0]
    reply = env.say("1")
    assert env.run().state == State.AWAITING_APPROVAL
    assert "Please confirm" in reply
    assert option["name"] in reply and f"₹{option['price']:,}" in reply and "Mon 2 Nov" in reply
    assert env.world.ledger("book") == []
    reply = env.say("yes")
    bookings = env.world.ledger("book")
    assert len(bookings) == 1
    assert bookings[0]["args"]["price"] == option["price"]
    assert bookings[0]["args"]["slot"] == option["slot"]
    assert "Booked and verified" in reply
    assert env.run().state == State.DONE
    assert len(env.tool_calls("get_booking_status")) == 1
    env.record.notes = f"1 booking at ₹{bookings[0]['args']['price']}; status checked before reporting success"
