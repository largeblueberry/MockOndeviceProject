"""DummyDetector: DummySource가 붙여 준 정답을 보고하되, 라즈베리파이에서 도는
실제 모델을 흉내 내는 노이즈를 섞는다.

    miss_rate   프레임마다 실제 객체 하나하나를 놓칠 확률 (깜빡임)
    false_rate  프레임마다 없는 사람을 검출할 확률
    latency_ms  추론 시간. 주입된 시계를 진행시키므로 FakeClock에서는 실제
                시간이 들지 않지만, 프레임이 오래되거나 건너뛰어지는 효과는 같다
    seed        노이즈 재현용: 같은 seed -> 같은 실행 결과
"""

import random

from psafe.core.clock import Clock
from psafe.core.packets import Detection, FrameDetections, FramePacket
from psafe.detectors.detector_api import DETECTORS, Detector


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

    def predict(self, pkt: FramePacket) -> FrameDetections:
        if "visible" not in pkt.meta:
            raise ValueError("DummyDetector needs ground truth in meta['visible'] (use the dummy source)")
        rng = self._rng
        objects = [
            Detection(d.cls, self.conf, d.bbox)
            for d in pkt.meta["visible"]
            if not (self.miss_rate and rng.random() < self.miss_rate)
        ]
        if self.false_rate and rng.random() < self.false_rate:
            x, y = rng.uniform(0.0, 0.85), rng.uniform(0.0, 0.6)
            objects.append(Detection("person", self.conf * 0.5, (x, y, x + 0.15, y + 0.4)))
        self.clock.sleep(self.latency_ms / 1000.0)
        return FrameDetections(pkt.seq, pkt.timestamp, tuple(objects), self.name, infer_ms=self.latency_ms)
