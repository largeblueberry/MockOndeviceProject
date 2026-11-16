from abc import ABC, abstractmethod

from psafe.core.clock import Clock
from psafe.core.packets import FramePacket
from psafe.core.registry import Registry


class Source(ABC):
    """프레임을 만든다. read()는 카메라처럼 다음 프레임이 올 때까지 대기한다.

    구현체는 Clock을 주입받으며, 시각 기록과 속도 조절에 반드시 이것을
    사용한다 (time.time() 금지).
    """

    def __init__(self, clock: Clock, source_id: str):
        self.clock = clock
        self.source_id = source_id

    def open(self) -> None: ...

    @abstractmethod
    def read(self) -> FramePacket | None:
        """다음 프레임. 스트림이 끝났거나 실패하면 None."""

    def close(self) -> None: ...


SOURCES: Registry[Source] = Registry("source")
