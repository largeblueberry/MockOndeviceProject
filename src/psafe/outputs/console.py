import logging

from psafe.core.packets import Decision
from psafe.outputs.output_api import OUTPUTS, Output

log = logging.getLogger("psafe.output.console")


@OUTPUTS.register("console")
class ConsoleOutput(Output):
    """Y/N을 로그로 남긴다. send()는 하트비트마다 호출되므로, 기본값은
    (safe, state, reason)이 바뀔 때만 기록한다."""

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
