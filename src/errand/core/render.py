"""All user-facing text. Models never write to the user, so a model cannot misstate a price or claim success."""

import re
from datetime import datetime

from errand.contract import AREAS, SERVICES
from errand.core.domain import Approval, Run

# A display filter only. The real protection is that models cannot reach write tools or approvals.
_INJECTION = re.compile(
    r"(ignore (all |previous |prior )|system:|assistant instruction|skip confirmation|already approved|mark this provider|you must|preferred technician)",
    re.IGNORECASE,
)
_LABEL_CHARS = 80



def service_text(service: str) -> str:
    return service.replace("_", " ").replace("ac ", "AC ")


def _one_of(choices: list[str]) -> str:
    return f"{', '.join(choices[:-1])} or {choices[-1]}"


# Offered whenever these are asked for, because a person cannot name a choice they have never been told about.
_SERVICE_CHOICES = _one_of([service_text(service) for service in SERVICES])
_AREA_CHOICES = _one_of([area.title() for area in AREAS])
WHAT_I_DO = f"I can book, reschedule or cancel a {_SERVICE_CHOICES} in {_AREA_CHOICES}. What would you like to do?"

_DETAIL_QUESTIONS = {
    "service": f"which service you need ({_SERVICE_CHOICES})",
    "area": f"which area you are in ({_AREA_CHOICES})",
    "when": "when you need it (for example this week, or Saturday)",
    "budget": "your budget in ₹ (or say no limit)",
    "booking": "which booking you mean",
}


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION.search(text))


def _label(text: str) -> str:
    return " ".join(text.split())[:_LABEL_CHARS]


def slot_text(slot: str) -> str:
    return datetime.fromisoformat(slot).strftime("%a %-d %b, %H:%M")


def dates_text(start: str, end: str) -> str:
    first = datetime.fromisoformat(start).strftime("%a %-d %b")
    return first if start == end else f"{first} to {datetime.fromisoformat(end).strftime('%a %-d %b')}"


def money(amount: int) -> str:
    return f"₹{amount:,}"


def provider_not_found(service: str, area: str) -> str:
    return f"I could not find that {service_text(service)} in {area.title()}. Try another area."


def below_budget(budget: int, service: str, area: str, cheapest: int) -> str:
    return f"Your budget of {money(budget)} is below the cheapest {service_text(service)} in {area.title()}, which is {money(cheapest)}. Change your budget, or say no limit."


def above_this_provider(budget: int, price: int) -> str:
    return f"Your budget of {money(budget)} is below this provider's price, {money(price)}. Change your budget, or say no limit."


def budget_blocks_free_slots(budget: int, start: str, end: str, cheapest_free: int) -> str:
    return f"Nothing within your budget of {money(budget)} is free for {dates_text(start, end)}. The cheapest that is free costs {money(cheapest_free)}. Change your budget, or say no limit."


def no_free_slots(start: str, end: str) -> str:
    return f"There are no free slots for {dates_text(start, end)}. Try another day."


def outside_time_limits(start: str, end: str, earliest: str | None, latest: str | None) -> str:
    limits = " and ".join(limit for limit in (f"after {earliest}" if earliest else None, f"before {latest}" if latest else None) if limit)
    return f"No free slot for {dates_text(start, end)} fits your time limit ({limits}). Change the limit, or try another day."


def ask_missing(missing: list[str]) -> str:
    return "To continue, please tell me " + " and ".join(_DETAIL_QUESTIONS[item] for item in missing) + "."


def options(run: Run) -> str:
    lines = ["Here are the options that fit:"]
    for option in run.options:
        lines.append(f"  {option['n']}. {_label(option['name'])}: {slot_text(option['slot'])}, {money(option['price'])}, rated {option['rating']}")
    lines.append("Which one would you like? Reply with its number.")
    return "\n".join(lines)


def booking_choices(run: Run) -> str:
    lines = ["You have more than one matching booking:"]
    for choice in run.booking_choices:
        lines.append(f"  {choice['n']}. {service_text(choice['service'])} on {slot_text(choice['slot'])}")
    lines.append("Which one do you mean? Reply with its number.")
    return "\n".join(lines)


def proposal(approval: Approval) -> str:
    verb = {"book": "Book", "cancel": "Cancel", "reschedule": "Move to"}[approval.action]
    price = f"{money(approval.price)} ({approval.price_label})" if approval.price_label else money(approval.price)
    return "\n".join(
        [
            "Please confirm exactly this:",
            f"  Action:   {verb}",
            f"  Provider: {_label(approval.provider_name)}",
            f"  Slot:     {slot_text(approval.slot)}",
            f"  Price:    {price}",
            "Reply yes to confirm, or no to decline.",
        ]
    )


def booked(approval: Approval, booking_id: str) -> str:
    return f"Booked and verified: {_label(approval.provider_name)}, {slot_text(approval.slot)}, {money(approval.price)}. Booking id {booking_id}."


def cancelled(approval: Approval) -> str:
    return f"Cancelled and verified: {_label(approval.provider_name)}, {slot_text(approval.slot)}."


def rescheduled(approval: Approval) -> str:
    return f"Moved and verified: {_label(approval.provider_name)} is now on {slot_text(approval.slot)}."


def provider_details(provider: dict) -> str:
    """Quotes reviews as reviews, and withholds any that look like instructions."""
    shown = [review for review in provider["reviews"] if not looks_like_injection(review)]
    withheld = len(provider["reviews"]) - len(shown)
    lines = [f"{_label(provider['name'])}: {money(provider['price'])}, rated {provider['rating']}."]
    attributes = [a for a in provider["attributes"] if not looks_like_injection(a)]
    if attributes:
        lines.append("Attributes: " + ", ".join(_label(a) for a in attributes))
    lines.extend(f'  Review: "{review}"' for review in shown)
    if withheld:
        lines.append(f"  ({withheld} review withheld because it contained instructions rather than an opinion.)")
    return "\n".join(lines)


def status(run: Run | None) -> str:
    if run is None:
        return "There is nothing waiting for your confirmation."
    if run.result:
        return f"There is nothing waiting for your confirmation. Last result: {run.result['summary']}"
    return f"There is nothing waiting for your confirmation. This errand is at the step: {run.state.value}."


def memory_view(facts: list[dict]) -> str:
    active = [f for f in facts if f["status"] == "active"]
    if not active:
        return "I am not keeping any preferences for you."
    lines = ["Here is what I keep about you:"]
    lines.extend(f"  {fact['key'].replace('_', ' ')}: {fact['value']} ({fact['provenance']})" for fact in active)
    lines.append("You can say, for example, 'change my area to koramangala' or 'forget my area'.")
    return "\n".join(lines)


# Every path should say something more useful. An invariant fails any scenario that reaches this.
NOTHING_TO_SAY = "I have nothing to add right now. Tell me what you would like to do."

MODEL_UNAVAILABLE = (
    "I could not reach my local model, so I did not act on that message. Nothing was booked or changed by it, "
    "and your details are saved. Replies like yes, no or an option number still work."
)

CAP_MESSAGES = {
    "model_calls": "I reached my limit on thinking steps",
    "tool_calls": "I reached my limit on provider calls",
    "wall_clock": "I reached my time limit",
}
