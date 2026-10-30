"""Clock adapters: the real system clock, and a movable one for tests and the demo."""

from datetime import UTC, datetime, timedelta


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: float) -> None:
        self._now += timedelta(**delta)


class OffsetClock:
    """System time plus an offset that only moves forward: lets the demo fast-forward time."""

    def __init__(self) -> None:
        self._offset = timedelta()

    def now(self) -> datetime:
        return datetime.now(UTC) + self._offset

    def advance(self, **delta: float) -> None:
        step = timedelta(**delta)
        if step < timedelta():
            raise ValueError("time only moves forward")
        self._offset += step
