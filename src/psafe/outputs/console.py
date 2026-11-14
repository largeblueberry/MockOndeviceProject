import logging

from psafe.core.packets import Decision
from psafe.outputs.base import OUTPUTS, Output

log = logging.getLogger("psafe.output.console")


@OUTPUTS.register("console")
class ConsoleOutput(Output):
    """Logs Y/N. By default only when (safe, state, reason) changes, since
    send() is called on every heartbeat."""

    def __init__(self, every: bool = False):
        self.every = every
        self._last: tuple | None = None

    def send(self, decision: Decision) -> None:
        key = (decision.safe, decision.state, decision.reason)
        if self.every or key != self._last:
            log.info(
                "[%s] %-9s reason=%-22s seq=%d t=%.2f",
                decision.yn, decision.state.value, decision.reason, decision.seq, decision.decided_at,
            )
            self._last = key
