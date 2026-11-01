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


@pytest.mark.parametrize("text", ["haan", "han", "Haan ji.", "ji haan", "theek hai", "thik hai!", "bilkul", "kar do", "book kar do", "kar dijiye", "please book it", "approve", "हाँ", "हां", "हाँ जी", "हां जी", "जी हाँ", "ठीक है", "बिल्कुल", "बिलकुल", "कर दो", "हाँ।", "ठीक है।"])
def test_bare_hindi_and_plain_confirmations_are_yes(text):
    assert classify(text).kind == Kind.YES


@pytest.mark.parametrize("text", ["haan but after 11", "theek hai but cheaper", "haan lekin kal", "theek hai?", "haan?", "kya yeh theek hai", "please book it for next week", "book kar do kal", "approve the other one", "हाँ लेकिन कल"])
def test_hindi_with_anything_more_is_not_a_confirmation(text):
    assert classify(text).kind == Kind.OTHER


@pytest.mark.parametrize("text", ["nahi", "Nahin.", "nahi chahiye", "mat karo", "नहीं", "नही", "मत करो", "नहीं।"])
def test_bare_hindi_rejections_are_no(text):
    assert classify(text).kind == Kind.NO


@pytest.mark.parametrize("text", ["ruk jao", "ruko", "Rehne do.", "chhodo", "chhod do", "band karo", "रुक जाओ", "रुको", "रहने दो", "छोड़ो", "छोड़ दो", "बंद करो", "रुक जाओ।", "रहने दो।", "stop।", "\u091b\u094b\u095c\u094b", "\u091b\u094b\u0921\u093c\u094b"])
def test_common_hindi_stops_are_settled_in_code(text):
    assert classify(text).kind == Kind.STOP


@pytest.mark.parametrize("text", ["proceed", "please proceed", "yes proceed", "yes go ahead", "yes do it", "confirm it", "yes book it", "Yes, proceed."])
def test_more_plain_confirmations_are_yes(text):
    assert classify(text).kind == Kind.YES


@pytest.mark.parametrize("text", ["yes book it", "yes, book it"])
def test_more_confirmations_naming_booking_say_so(text):
    assert classify(text).names_action == "book"


@pytest.mark.parametrize("text", ["yes proceed but cheaper", "proceed with the other one", "proceed?", "yes ?", "go ahead ?", "stop ?"])
def test_anything_more_or_a_question_is_still_not_settled_in_code(text):
    assert classify(text).kind == Kind.OTHER


@pytest.mark.parametrize("text", ["what do you remember ?", "what do you remember  ?", "show memory ?", "What do you know about me ?"])
def test_a_space_before_the_question_mark_still_finds_the_memory_command(text):
    assert classify(text).kind == Kind.MEMORY_VIEW


def test_a_space_before_the_question_mark_still_finds_a_forget():
    intake = classify("forget my area ?")
    assert (intake.kind, intake.key) == (Kind.MEMORY_FORGET, "area")


@pytest.mark.parametrize("text", ["yes?", "ok?", "book it?", "Go ahead?", "confirm?"])
def test_a_question_is_not_a_confirmation(text):
    assert classify(text).kind == Kind.OTHER


@pytest.mark.parametrize("text", ["what do you remember?", "What do you remember about me?"])
def test_memory_questions_still_count_as_memory_commands(text):
    assert classify(text).kind == Kind.MEMORY_VIEW


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


@pytest.mark.parametrize("text", ["book it", "please book it", "book kar do"])
def test_a_confirmation_naming_booking_says_so(text):
    assert classify(text).names_action == "book"


@pytest.mark.parametrize("text", ["yes", "haan", "go ahead", "approve", "kar do"])
def test_a_plain_confirmation_names_no_action(text):
    assert classify(text).names_action is None


def test_forget_names_the_fact():
    intake = classify("forget my preferred technician")
    assert (intake.kind, intake.key) == (Kind.MEMORY_FORGET, "preferred_technician")


def test_set_captures_the_value():
    intake = classify("change my area to koramangala")
    assert (intake.kind, intake.key, intake.value) == (Kind.MEMORY_SET, "area", "koramangala")


def test_remember_my_area_is_captures_the_value():
    intake = classify("remember my area is indiranagar")
    assert (intake.kind, intake.key, intake.value) == (Kind.MEMORY_SET, "area", "indiranagar")
