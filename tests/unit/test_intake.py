import pytest

from errand.core.intake import Kind, classify


@pytest.mark.parametrize("text", ["yes", "Yes.", "  YES!  ", "y", "confirm", "go ahead", "yes please"])
def test_bare_confirmations_are_yes(text):
    assert classify(text).kind == Kind.YES


@pytest.mark.parametrize(
    "text",
    ["yes, but make it 11", "yes if it is cheaper", "yes book the other one", "sure why not, maybe", "the review says yes", "I said yes earlier", "ok but later"],
)
def test_anything_more_than_a_bare_confirmation_is_not_yes(text):
    assert classify(text).kind == Kind.OTHER


@pytest.mark.parametrize("text", ["no", "No.", "nope", "no thanks"])
def test_bare_rejections_are_no(text):
    assert classify(text).kind == Kind.NO


@pytest.mark.parametrize("text", ["stop", "never mind", "Never mind.", "cancel this"])
def test_stop_phrases(text):
    assert classify(text).kind == Kind.STOP


def test_cancel_alone_is_not_a_stop_because_it_may_mean_cancel_a_booking():
    assert classify("cancel my plumber").kind == Kind.OTHER


@pytest.mark.parametrize("text,number", [("1", 1), ("option 2", 2), ("3.", 3)])
def test_numbers_select_options(text, number):
    intake = classify(text)
    assert (intake.kind, intake.number) == (Kind.NUMBER, number)


def test_forget_names_the_fact():
    intake = classify("forget my preferred technician")
    assert (intake.kind, intake.key) == (Kind.MEMORY_FORGET, "preferred_technician")


def test_set_captures_the_value():
    intake = classify("change my area to koramangala")
    assert (intake.kind, intake.key, intake.value) == (Kind.MEMORY_SET, "area", "koramangala")


def test_remember_my_area_is_captures_the_value():
    intake = classify("remember my area is indiranagar")
    assert (intake.kind, intake.key, intake.value) == (Kind.MEMORY_SET, "area", "indiranagar")
