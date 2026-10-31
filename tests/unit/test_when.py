from datetime import date

import pytest

from errand.core.when import normalise_time, resolve_when

MONDAY = date(2026, 11, 2)
SATURDAY = date(2026, 11, 7)


def test_this_week_runs_to_sunday():
    assert resolve_when("this_week", MONDAY) == ("2026-11-02", "2026-11-08")


def test_next_week_is_the_following_monday_to_sunday():
    assert resolve_when("next_week", MONDAY) == ("2026-11-09", "2026-11-15")


def test_a_weekday_means_the_next_one():
    assert resolve_when("saturday", MONDAY) == ("2026-11-07", "2026-11-07")


def test_a_weekday_can_be_today():
    assert resolve_when("saturday", SATURDAY) == ("2026-11-07", "2026-11-07")


def test_a_past_weekday_rolls_to_next_week():
    assert resolve_when("monday", SATURDAY) == ("2026-11-09", "2026-11-09")


def test_an_iso_date_is_accepted():
    assert resolve_when("2026-11-20", MONDAY) == ("2026-11-20", "2026-11-20")


def test_an_unknown_token_is_an_error():
    with pytest.raises(ValueError):
        resolve_when("whenever", MONDAY)


@pytest.mark.parametrize(
    "text,expected",
    [("10", "10:00"), ("10:30", "10:30"), ("10am", "10:00"), ("10 a.m.", "10:00"), ("2 p.m.", "14:00"), ("12 pm", "12:00"), ("12 am", "00:00")],
)
def test_times_are_normalised(text, expected):
    assert normalise_time(text) == expected


@pytest.mark.parametrize("text", ["25", "morning", "10:99", ""])
def test_bad_times_are_errors(text):
    with pytest.raises(ValueError):
        normalise_time(text)
