"""DummyDetector: reports the ground truth DummySource attached, plus noise
that mimics a real model on a Raspberry Pi.

    miss_rate   probability that each true object is dropped in a frame (flicker)
    false_rate  probability per frame of a phantom person
    latency_ms  inference time; advances the injected clock, so with FakeClock
                it costs no real time but frames still go stale / get skipped
    seed        noise is reproducible: same seed -> same run
"""

import random

from psafe.core.clock import Clock
from psafe.core.packets import Detection, Detections, FramePacket
from psafe.models.base import DETECTORS, Detector


@DETECTORS.register("dummy")
class DummyDetector(Detector):
    name = "dummy"

    def __init__(
        self,
        clock: Clock,
        conf: float = 0.99,
        miss_rate: float = 0.0,
        false_rate: float = 0.0,
        latency_ms: float = 0.0,
        seed: int = 0,
    ):
        for k, v in (("miss_rate", miss_rate), ("false_rate", false_rate)):
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"{k} must be in [0, 1], got {v}")
        if latency_ms < 0:
            raise ValueError("latency_ms must be >= 0")
        self.clock = clock
        self.conf = conf
        self.miss_rate, self.false_rate = miss_rate, false_rate
        self.latency_ms = latency_ms
        self.seed = seed
        self._rng = random.Random(seed)

    def load(self) -> None:
        self._rng = random.Random(self.seed)

    def predict(self, pkt: FramePacket) -> Detections:
        if "gt" not in pkt.meta:
            raise ValueError("DummyDetector needs ground truth in meta['gt'] (use the dummy source)")
        rng = self._rng
        objects = [
            Detection(d.cls, self.conf, d.bbox)
            for d in pkt.meta["gt"]
            if not (self.miss_rate and rng.random() < self.miss_rate)
        ]
        if self.false_rate and rng.random() < self.false_rate:
            x, y = rng.uniform(0.0, 0.85), rng.uniform(0.0, 0.6)
            objects.append(Detection("person", self.conf * 0.5, (x, y, x + 0.15, y + 0.4)))
        self.clock.sleep(self.latency_ms / 1000.0)
        return Detections(pkt.seq, pkt.timestamp, tuple(objects), self.name, infer_ms=self.latency_ms)
