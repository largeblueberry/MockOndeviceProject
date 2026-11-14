from abc import ABC, abstractmethod

from psafe.core.packets import Detections, FramePacket
from psafe.core.registry import Registry


class Detector(ABC):
    """Model abstraction (DESIGN.md §5.2).

    predict() owns the WHOLE model-specific path: preprocess -> infer ->
    postprocess (NMS, normalized bbox, class names mapped to the internal
    standard such as "person"). The FSM never knows which model ran.
    """

    name: str = "base"

    def load(self) -> None: ...

    def warmup(self) -> None: ...

    @abstractmethod
    def predict(self, pkt: FramePacket) -> Detections: ...

    def close(self) -> None: ...


DETECTORS: Registry[Detector] = Registry("model")
