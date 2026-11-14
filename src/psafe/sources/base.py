from abc import ABC, abstractmethod

from psafe.core.clock import Clock
from psafe.core.packets import FramePacket
from psafe.core.registry import Registry


class Source(ABC):
    """Produces frames. read() blocks until the next frame, like a camera.

    Implementations get the Clock injected and must use it for timestamps
    and pacing (never time.time()).
    """

    def __init__(self, clock: Clock, source_id: str):
        self.clock = clock
        self.source_id = source_id

    def open(self) -> None: ...

    @abstractmethod
    def read(self) -> FramePacket | None:
        """Next frame, or None when the stream has ended / failed."""

    def close(self) -> None: ...


SOURCES: Registry[Source] = Registry("source")
