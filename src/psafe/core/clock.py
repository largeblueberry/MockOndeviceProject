"""Injectable clocks (DESIGN.md §3.4).

No component calls time.time() directly. Real runs use RealClock; scenario
tests use FakeClock, so a 15 s scenario finishes instantly and identically.
"""

import threading
import time
from abc import ABC, abstractmethod


class Clock(ABC):
    @abstractmethod
    def now(self) -> float:
        """Monotonic time in seconds."""

    @abstractmethod
    def sleep(self, seconds: float) -> None: ...


class RealClock(Clock):
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock(Clock):
    """Time only moves when someone sleeps or calls advance()."""

    def __init__(self, start: float = 0.0):
        self._t = start
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._t

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move time backwards")
        with self._lock:
            self._t += seconds

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.advance(seconds)
