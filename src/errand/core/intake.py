"""Decides in code what a raw user message means when it is a bare answer or command. No model is involved."""

import re
from dataclasses import dataclass
from enum import Enum


class Kind(Enum):
    YES = "yes"
    NO = "no"
    STOP = "stop"
    NUMBER = "number"
    MEMORY_VIEW = "memory_view"
    MEMORY_FORGET = "memory_forget"
    MEMORY_SET = "memory_set"
    OTHER = "other"


@dataclass(frozen=True)
class Intake:
    kind: Kind
    number: int | None = None
    key: str | None = None
    value: str | None = None


# Bare confirmations only. "yes, but make it 11" does not match, so it can never become an approval.
_YES = {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "confirm", "confirmed", "yes please", "yes confirm", "go ahead", "book it", "do it"}
_NO = {"no", "n", "nope", "no thanks", "dont", "do not"}
_STOP = {"stop", "never mind", "nevermind", "abandon", "cancel this", "forget it", "quit"}
_MEMORY_VIEW = {
    "what do you remember",
    "what do you remember about me",
    "what do you know about me",
    "show memory",
    "show my memory",
    "show my preferences",
    "view memory",
}
_MEMORY_NAMES = {
    "area": "area",
    "earliest time": "earliest_time",
    "preferred technician": "preferred_technician",
    "technician": "preferred_technician",
}
_NAMES_PATTERN = "|".join(sorted(_MEMORY_NAMES, key=len, reverse=True))
_FORGET = re.compile(rf"^forget (?:my |the )?({_NAMES_PATTERN})$")
_SET = re.compile(rf"^(?:remember|set|change|correct|update) (?:my |the )?({_NAMES_PATTERN})(?: is| to| as)? (.+)$", re.IGNORECASE)
_NUMBER = re.compile(r"^(?:option |number )?(\d+)$")


def _normalise(text: str) -> str:
    cleaned = text.strip().lower().replace("'", "")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.rstrip(".!?, ")


def classify(text: str) -> Intake:
    cleaned = _normalise(text)
    if cleaned in _YES:
        return Intake(Kind.YES)
    if cleaned in _NO:
        return Intake(Kind.NO)
    if cleaned in _STOP:
        return Intake(Kind.STOP)
    if cleaned in _MEMORY_VIEW:
        return Intake(Kind.MEMORY_VIEW)
    match = _NUMBER.match(cleaned)
    if match:
        return Intake(Kind.NUMBER, number=int(match.group(1)))
    match = _FORGET.match(cleaned)
    if match:
        return Intake(Kind.MEMORY_FORGET, key=_MEMORY_NAMES[match.group(1)])
    match = _SET.match(re.sub(r"\s+", " ", text.strip()).rstrip(".!?, "))
    if match:
        return Intake(Kind.MEMORY_SET, key=_MEMORY_NAMES[match.group(1).lower()], value=match.group(2).strip())
    return Intake(Kind.OTHER)
