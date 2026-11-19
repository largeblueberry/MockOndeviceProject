"""C의 출력 인터페이스 (DESIGN.md §7.5.3). 디스플레이 기종이 정해지지 않았으므로
'최종 판정을 받아 보여 준다'는 계약만 고정한다.

    show()   하트비트마다(200 ms) 호출된다. 바뀔 때만이 아니다. 가볍고 막히면 안 된다.
    poll()   선택. 메인 스레드에서 호출된다 (GUI용). False = 종료 요청.

표시 규칙 (모든 구현체):
    1. kind를 색과 글자로 함께 표시한다 (색만으로 구분하지 않는다)
    2. OCCUPIED면 노드별 인원을 표시한다 (합계가 아니다. 겹치는 구역에서 두 번 세어질 수 있다)
    3. UNKNOWN이면 사유를 표시한다
    4. 살아 있음 표시: show()마다 바뀌는 요소와 '마지막 갱신 N초 전'
    5. C와 다른 프로세스·장치에서 그린다면, show()가 1초 넘게 없을 때 스스로 UNKNOWN 화면으로 바꾼다

LED는 이 인터페이스를 거쳐 기존 ESP32 Output(serial, mock_mcu)으로 나간다
(DecisionBridge). ESP32 워치독이 있으므로 C가 죽어도 초록이 남지 않는다.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any

from psafe.core.clock import Clock
from psafe.core.packets import Decision, State
from psafe.core.registry import Registry
from psafe.core.verdict import Verdict, VerdictKind
from psafe.outputs.output_api import OUTPUTS, Output
from psafe.safety.aggregator import to_protocol

log = logging.getLogger("psafe.output.verdict")


class VerdictOutput(ABC):
    def open(self) -> None: ...

    @abstractmethod
    def show(self, verdict: Verdict) -> None: ...

    def poll(self) -> bool:
        return True

    def close(self) -> None: ...


VERDICT_OUTPUTS: Registry[VerdictOutput] = Registry("verdict output")


def verdict_decision(v: Verdict) -> Decision:
    """Verdict -> Decision. 기존 Output(ESP32 등)에 그대로 넘기기 위한 변환 (§7.4.6 매핑)."""
    state, safe = to_protocol(v)
    return Decision(safe, state, v.reason, -1, v.decided_at, v.decided_at)


@VERDICT_OUTPUTS.register("decision")
class DecisionBridge(VerdictOutput):
    """기존 Output 하나를 감싼다. 예: ESP32 LED

        - name: decision
          options: { output: serial, options: { port: /dev/ttyUSB0 } }
    """

    def __init__(self, clock: Clock, output: str, options: dict[str, Any] | None = None):
        self.inner: Output = OUTPUTS.create(output, {"clock": clock}, **(options or {}))

    def open(self) -> None:
        self.inner.open()

    def show(self, verdict: Verdict) -> None:
        self.inner.send(verdict_decision(verdict))

    def poll(self) -> bool:
        return self.inner.poll()

    def close(self) -> None:
        self.inner.close()


@VERDICT_OUTPUTS.register("console")
class ConsoleVerdictOutput(VerdictOutput):
    """판정을 로그로 남긴다. 기본값은 (kind, reason, 노드별 인원)이 바뀔 때만."""

    def __init__(self, every: bool = False):
        self.every = every
        self._last: tuple | None = None

    def show(self, verdict: Verdict) -> None:
        key = (verdict.kind, verdict.reason, tuple((n.node, n.alive, n.count) for n in verdict.nodes))
        if not self.every and key == self._last:
            return
        self._last = key
        log.info("%s", format_verdict(verdict))


_LABELS = {
    VerdictKind.EMPTY: "비었음 확인",
    VerdictKind.OCCUPIED: "사람 있음",
    VerdictKind.CHECKING: "확인 중",
    VerdictKind.UNKNOWN: "판단 불가",
}


def format_verdict(v: Verdict) -> str:
    """한 줄 요약. 텍스트 기반 구현체가 공유한다."""
    parts = []
    for n in v.nodes:
        if not n.alive:
            parts.append(f"{n.node}: 무응답")
        else:
            age = "-" if n.evidence_age_ms is None else f"{n.evidence_age_ms:.0f}ms"
            why = "" if n.state in (State.CLEAR, State.VERIFYING, State.OCCUPIED) else f"({n.reason}) "
            parts.append(f"{n.node}: {n.count}명 {n.state.value} {why}{age}")
    cid = "-" if v.confirm_id is None else str(v.confirm_id)
    return f"[{v.kind.value:<8}] {_LABELS[v.kind]} ({v.reason}) | {' | '.join(parts)} | confirm={cid} t={v.decided_at:.2f}"
