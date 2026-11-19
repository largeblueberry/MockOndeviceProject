"""파이프라인 단계 사이에 주고받는 데이터 계약 (DESIGN.md §4).

이 타입들은 모듈 간의 약속이다. 필드를 바꾸면 모든 단계를 다시 확인해야
하므로, 작고 안정적으로 유지한다.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

BBox = tuple[float, float, float, float]  # (x1, y1, x2, y2), 0..1로 정규화


@dataclass(frozen=True, slots=True)
class Detection:
    cls: str      # 내부 표준 클래스 이름: "person", "car", ...
    conf: float   # 신뢰도 0..1
    bbox: BBox    # 정규화 좌표


@dataclass(frozen=True, slots=True)
class FramePacket:
    seq: int                 # 단조 증가하는 프레임 번호
    timestamp: float         # 주입된 Clock 기준 촬영 시각 (초)
    source_id: str
    image: np.ndarray        # H x W x 3, BGR, uint8
    meta: dict[str, Any] = field(default_factory=dict)  # dummy 소스는 여기에 정답(ground truth)을 넣는다


@dataclass(frozen=True, slots=True)
class FrameDetections:
    seq: int
    timestamp: float
    objects: tuple[Detection, ...]
    model_name: str
    infer_ms: float


class State(str, Enum):
    INIT = "INIT"
    OCCUPIED = "OCCUPIED"
    VERIFYING = "VERIFYING"
    CLEAR = "CLEAR"
    FAULT = "FAULT"


@dataclass(frozen=True, slots=True)
class Decision:
    safe: bool          # 최종 출력: True = Y, False = N
    state: State
    reason: str         # 예: "person_in_roi", "clear", "stale_frame"
    seq: int            # 이 판단을 만든 프레임 번호 (없으면 -1)
    timestamp: float    # 그 프레임의 촬영 시각
    decided_at: float   # 판단을 내린 시각 (Clock 기준)
    count: int = 0      # 이 프레임에서 ROI 안에 있던 사람 수 (OCCUPIED일 때만 > 0). 보드 간 통신용 (§7.4)

    @property
    def yn(self) -> str:
        return "Y" if self.safe else "N"


def unsafe(state: State, reason: str, now: float, seq: int = -1, timestamp: float = -1.0) -> Decision:
    """N 판단을 만드는 단축 함수. 안전 쪽으로 실패해야 하는 모든 곳에서 쓴다."""
    return Decision(False, state, reason, seq, timestamp, now)
