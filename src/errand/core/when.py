"""Date and time parsing, done in code because the rules must hold whichever model is running."""

import re
from datetime import date, timedelta

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_SUNDAY = 6
WHEN_WORDS = ("today", "tomorrow", "this_week", "next_week", *_WEEKDAYS)
_TIME = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?$")


def resolve_when(token: str, today: date) -> tuple[str, str]:
    """Turns a token such as this_week or saturday into a start and end date. Raises ValueError if unknown."""
    cleaned = token.strip().lower()
    if cleaned == "today":
        return today.isoformat(), today.isoformat()
    if cleaned == "tomorrow":
        day = today + timedelta(days=1)
        return day.isoformat(), day.isoformat()
    if cleaned == "this_week":
        return today.isoformat(), (today + timedelta(days=_SUNDAY - today.weekday())).isoformat()
    if cleaned == "next_week":
        monday = today + timedelta(days=7 - today.weekday())
        return monday.isoformat(), (monday + timedelta(days=_SUNDAY)).isoformat()
    if cleaned in _WEEKDAYS:
        ahead = (_WEEKDAYS.index(cleaned) - today.weekday()) % 7
        day = today + timedelta(days=ahead)
        return day.isoformat(), day.isoformat()
    return date.fromisoformat(cleaned).isoformat(), date.fromisoformat(cleaned).isoformat()


def normalise_time(text: str) -> str:
    """Turns 10, 10:30, 10am or 2 p.m. into HH:MM. Raises ValueError if unrecognised."""
    match = _TIME.match(text.strip().lower())
    if match is None:
        raise ValueError(f"not a time: {text!r}")
    hour, minute, meridiem = int(match.group(1)), int(match.group(2) or 0), match.group(3)
    if meridiem and meridiem.startswith("p") and hour != 12:
        hour += 12
    if meridiem and meridiem.startswith("a") and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"not a time: {text!r}")
    return f"{hour:02d}:{minute:02d}"
