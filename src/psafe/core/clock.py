"""주입 가능한 시계 (DESIGN.md §3.4).

어떤 부품도 time.time()을 직접 호출하지 않는다. 실제 실행은 RealClock,
시나리오 테스트는 FakeClock을 쓰므로 15초 시나리오가 즉시, 매번 똑같이 끝난다.
"""

import threading
import time
from abc import ABC, abstractmethod


class Clock(ABC):
    @abstractmethod
    def now(self) -> float:
        """단조 증가 시각 (초)."""

    @abstractmethod
    def sleep(self, seconds: float) -> None: ...


class RealClock(Clock):
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock(Clock):
    """누군가 sleep()이나 advance()를 호출할 때만 시간이 흐른다."""

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
