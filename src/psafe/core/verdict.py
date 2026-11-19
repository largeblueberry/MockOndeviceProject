"""C의 최종 판정 계약 (DESIGN.md §7.5.2). Aggregator -> VerdictOutput들.

A·B의 Decision(packets.py)과 같은 역할을 C에서 맡는다. 화면·LED 구현체는
이 타입만 알면 된다.
"""

from dataclasses import dataclass
from enum import Enum

from psafe.core.packets import State


class VerdictKind(str, Enum):
    EMPTY = "EMPTY"            # 비었음 확정 (A·B 모두 확인 완료)
    OCCUPIED = "OCCUPIED"      # 사람 있음 (어느 한쪽이라도)
    CHECKING = "CHECKING"      # 사람은 안 보이지만 아직 확정 전
    UNKNOWN = "UNKNOWN"        # 판단 불가 (무응답, 카메라 이상, 근거가 오래됨, 확인 실패)


@dataclass(frozen=True, slots=True)
class NodeView:
    """C가 본 노드 하나의 상태. 화면에 노드별로 보여 줄 내용."""
    node: str
    alive: bool                       # 무응답이 아님
    state: State | None               # 그 노드의 FSM 상태 (무응답이면 None)
    count: int                        # 그 노드가 본 사람 수 (무응답이면 0)
    reason: str                       # 그 노드의 reason, 무응답이면 "timeout"
    evidence_age_ms: float | None     # C 시계 기준 근거 나이 (§7.4.3). 모르면 None
    confirm: str | None               # 현재 confirm_id에 대한 그 노드의 답. 다른 번호에 대한 답이면 None
    transit_ms: float | None = None   # 노드 -> C 전달 시간 상한 (왕복 시간). 근거 나이에 이미 포함됨. 모르면 None


@dataclass(frozen=True, slots=True)
class Verdict:
    kind: VerdictKind
    reason: str                       # 예: confirmed, person_A, A_timeout, confirm_timeout
    nodes: tuple[NodeView, ...]       # 설정에 적힌 노드 순서
    confirm_id: int | None            # 현재 확인 번호 (A·B에게 보내는 값)
    decided_at: float                 # C의 Clock 기준

    @property
    def safe(self) -> bool:
        return self.kind == VerdictKind.EMPTY


# ---- ESP32 연결 (DESIGN.md §7.4.6 매핑) -------------------------------------
# 순수한 데이터 변환이라 여기에 둔다. outputs(브리지)와 safety가 서로 import하지 않게 하기 위해서다.

VERDICT_STATES: dict[VerdictKind, State] = {
    VerdictKind.EMPTY: State.CLEAR,
    VerdictKind.OCCUPIED: State.OCCUPIED,
    VerdictKind.CHECKING: State.VERIFYING,
    VerdictKind.UNKNOWN: State.FAULT,
}


def to_protocol(v: Verdict) -> tuple[State, bool]:
    """Verdict -> ESP32 프로토콜 v1의 (STATE, SAFE). Y는 EMPTY일 때만."""
    return VERDICT_STATES[v.kind], v.kind == VerdictKind.EMPTY
