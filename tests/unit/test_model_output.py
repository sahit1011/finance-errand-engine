"""The boundary that every model output passes through, scripted or live."""

import pytest

from errand.core.domain import InvalidModelOutput
from errand.interpret.model import parse_output


def test_valid_output_parses():
    output = parse_output({"actions": [{"type": "set_intent", "intent": "book"}], "done": True})
    assert output.done is True and output.actions[0]["intent"] == "book"


def test_json_text_parses():
    assert parse_output('{"actions": [], "done": false}').done is False


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        ["a", "list"],
        {"actions": "book"},
        {"actions": [], "user_approved": True},
        {"actions": [{"type": "book"}]},
        {"actions": [{"type": "approve", "approval_id": "approval-1"}]},
        {"actions": [{"type": "cancel", "booking_id": "b-1"}]},
        {"actions": [{"type": "hold_slot", "slot": "x"}]},
        {"actions": [{"type": "set_intent", "intent": "delete_everything"}]},
        {"actions": [{"type": "set_intent", "intent": "book", "approved": True}]},
        {"actions": [{"type": "update_details", "details": {"price": 1}}]},
        {"actions": [{"type": "select_option", "option": "1"}]},
        {"actions": [{"type": "select_option", "option": True}]},
        {"actions": [{"type": "request_read", "tool": "book"}]},
        {"actions": [{"type": "propose_memory", "key": "password", "value": "x"}]},
        {"actions": [], "done": "yes"},
    ],
)
def test_anything_outside_the_schema_is_rejected(raw):
    with pytest.raises(InvalidModelOutput):
        parse_output(raw)
