from abc import ABC, abstractmethod

from psafe.core.packets import Decision
from psafe.core.registry import Registry


class Output(ABC):
    """Consumes decisions. send() is called periodically (heartbeat), not only
    on change, so it must be cheap and must not block for long."""

    def open(self) -> None: ...

    @abstractmethod
    def send(self, decision: Decision) -> None: ...

    def close(self) -> None: ...


OUTPUTS: Registry[Output] = Registry("output")
