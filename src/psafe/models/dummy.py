"""DummyDetector: returns the ground truth that DummySource attached.

M2 adds noise (miss_rate / false_rate / latency_ms) to mimic a real model.
"""

from psafe.core.packets import Detection, Detections, FramePacket
from psafe.models.base import DETECTORS, Detector


@DETECTORS.register("dummy")
class DummyDetector(Detector):
    name = "dummy"

    def __init__(self, conf: float = 0.99):
        self.conf = conf

    def predict(self, pkt: FramePacket) -> Detections:
        if "gt" not in pkt.meta:
            raise ValueError("DummyDetector needs ground truth in meta['gt'] (use the dummy source)")
        objects = tuple(Detection(d.cls, self.conf, d.bbox) for d in pkt.meta["gt"])
        return Detections(pkt.seq, pkt.timestamp, objects, self.name, infer_ms=0.0)
