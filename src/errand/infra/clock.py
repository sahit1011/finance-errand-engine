"""Time source, injected so expiry and wall-clock caps can be tested without sleeping."""

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(IST)


class FakeClock:
    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta) -> None:
        self._now += timedelta(**delta)
