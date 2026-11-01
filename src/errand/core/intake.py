"""Decides in code what a raw user message means when it is a bare answer or command. No model is involved.

These lists are a fast path, not the only way in: a model reads every other wording of a command. The exception is
approval, which only this code can grant, because a wrong approval spends money and a wrong anything else does not.
"""

import re
import unicodedata
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
    # Set when a confirmation names an action, so that it cannot confirm a different one.
    names_action: str | None = None


# Bare confirmations only. "yes, but make it 11" and "yes?" do not match, so neither can become an approval.
def _canonical(text: str) -> str:
    """Hindi letters such as a nukta can be typed in more than one way, so both sides are compared in one form."""
    return unicodedata.normalize("NFC", text)


_YES = {
    "yes", "y", "yeah", "yep", "ok", "okay", "sure", "confirm", "confirmed", "yes please", "yes confirm", "go ahead", "book it", "do it",
    "please book it", "yes book it", "proceed", "please proceed", "yes proceed", "yes go ahead", "yes do it", "confirm it", "approve", "haan", "han", "haan ji", "ji haan", "theek hai", "thik hai", "bilkul", "kar do", "book kar do", "kar dijiye",
    "हाँ", "हां", "हाँ जी", "हां जी", "जी हाँ", "जी हां", "ठीक है", "बिल्कुल", "बिलकुल", "कर दो",
}
_NAMES_BOOKING = {"book it", "please book it", "yes book it", "book kar do"}
_NO = {"no", "n", "nope", "no thanks", "dont", "do not", "nahi", "nahin", "nahi chahiye", "mat karo", "नहीं", "नही", "मत करो"}
# Recognised without a model, so a stop still works while a write is in flight even if the model is down.
_STOP = {
    "stop", "never mind", "nevermind", "abandon", "cancel this", "forget it", "quit",
    "ruk jao", "ruko", "rehne do", "chhodo", "chhod do", "band karo", "रुक जाओ", "रुको", "रहने दो", "छोड़ो", "छोड़ दो", "बंद करो",
}
_YES, _NO, _STOP = ({_canonical(word) for word in words} for words in (_YES, _NO, _STOP))
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
    # A comma inside a phrase changes nothing, and a phrase still has to match in full to count.
    cleaned = re.sub(r"\s*,\s*", " ", _canonical(text).strip().lower().replace("'", ""))
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.rstrip(".!, ।")


def classify(text: str) -> Intake:
    cleaned = _normalise(text)
    if cleaned in _YES:
        return Intake(Kind.YES, names_action="book" if cleaned in _NAMES_BOOKING else None)
    if cleaned in _NO:
        return Intake(Kind.NO)
    if cleaned in _STOP:
        return Intake(Kind.STOP)
    if cleaned.rstrip("? ") in _MEMORY_VIEW:
        return Intake(Kind.MEMORY_VIEW)
    match = _NUMBER.match(cleaned)
    if match:
        return Intake(Kind.NUMBER, number=int(match.group(1)))
    match = _FORGET.match(cleaned.rstrip("? "))
    if match:
        return Intake(Kind.MEMORY_FORGET, key=_MEMORY_NAMES[match.group(1)])
    match = _SET.match(re.sub(r"\s+", " ", text.strip()).rstrip(".!?, "))
    if match:
        return Intake(Kind.MEMORY_SET, key=_MEMORY_NAMES[match.group(1).lower()], value=match.group(2).strip())
    return Intake(Kind.OTHER)
