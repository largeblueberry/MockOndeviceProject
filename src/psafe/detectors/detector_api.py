from abc import ABC, abstractmethod

from psafe.core.packets import FrameDetections, FramePacket
from psafe.core.registry import Registry


class Detector(ABC):
    """모델 추상화 (DESIGN.md §5.2).

    predict()는 모델별 처리 전체를 맡는다: 전처리 -> 추론 -> 후처리
    (NMS, bbox 정규화, 클래스 이름을 "person" 같은 내부 표준으로 변환).
    FSM은 어떤 모델이 돌았는지 알지 못한다.
    """

    name: str = "base"

    def load(self) -> None: ...

    def warmup(self) -> None: ...

    @abstractmethod
    def predict(self, pkt: FramePacket) -> FrameDetections: ...

    def close(self) -> None: ...


DETECTORS: Registry[Detector] = Registry("model")
