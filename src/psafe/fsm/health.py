"""카메라 상태 검사 (DESIGN.md §3.5).

카메라가 실제로 장면을 보지 못하는데 모델이 "비어 있음"이라고 답하는 경우를
잡는다: 렌즈 가림, 과노출, 화면 정지.
축소한 흑백 사본으로 계산하므로 1 ms보다 훨씬 적게 걸린다.
"""

import numpy as np

from psafe.core.config import HealthConfig
from psafe.core.packets import FramePacket

STEP = 4  # 가로·세로 4픽셀마다 1개만 본다


class HealthMonitor:
    def __init__(self, cfg: HealthConfig):
        self.cfg = cfg
        self._prev: np.ndarray | None = None
        self._frozen = 0
        self.last_brightness = 0.0
        self.last_diff = 0.0
        self.last_contrast = 0.0
        self._ranges: dict[str, list[float]] = {}   # 지표 -> [최소, 최대] (직전 take_ranges() 이후)

    def reset(self) -> None:
        self._prev, self._frozen = None, 0

    def _track(self, name: str, v: float) -> None:
        r = self._ranges.get(name)
        if r is None:
            self._ranges[name] = [v, v]
        else:
            r[0], r[1] = min(r[0], v), max(r[1], v)

    def take_ranges(self) -> dict[str, tuple[float, float]]:
        """직전 호출 이후의 {지표: (최소, 최대)} (밝기, 대비, 차이)를 반환하고 초기화한다.
        튜닝 로그 전용이며, 다른 스레드에서 읽으면 근삿값이다."""
        ranges, self._ranges = self._ranges, {}
        return {k: (v[0], v[1]) for k, v in ranges.items()}

    def check(self, pkt: FramePacket) -> str | None:
        """고장 사유를 반환한다. 프레임이 정상으로 보이면 None."""
        img = pkt.image
        if img is None or img.size == 0:
            return "empty_frame"
        small = img[::STEP, ::STEP].astype(np.float32).mean(axis=2)  # 간단한 흑백 변환
        self.last_brightness = float(small.mean())
        self.last_contrast = float(small.std())      # 렌즈를 가렸거나 빈 벽이면 ~0
        self._track("brightness", self.last_brightness)
        self._track("contrast", self.last_contrast)

        prev, self._prev = self._prev, small
        if prev is not None and prev.shape == small.shape:
            self.last_diff = d = float(np.abs(small - prev).mean())
            self._track("diff", d)
            self._frozen = self._frozen + 1 if self.last_diff < self.cfg.freeze_diff_threshold else 0
        if self._frozen >= self.cfg.freeze_frames:
            return "frozen_frame"

        if self.last_brightness < self.cfg.min_brightness:
            return "too_dark"
        if self.last_brightness > self.cfg.max_brightness:
            return "too_bright"
        return None
