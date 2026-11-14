"""Camera health checks (DESIGN.md §3.5).

Catches the cases where the model would happily say "empty" because the
camera is not really looking: lens covered, over-exposed, or stuck frames.
Runs on a downsampled grayscale copy, so it costs well under 1 ms.
"""

import numpy as np

from psafe.core.config import HealthConfig
from psafe.core.packets import FramePacket

STEP = 4  # look at every 4th pixel in each direction


class HealthMonitor:
    def __init__(self, cfg: HealthConfig):
        self.cfg = cfg
        self._prev: np.ndarray | None = None
        self._frozen = 0
        self.last_brightness = 0.0
        self.last_diff = 0.0

    def reset(self) -> None:
        self._prev, self._frozen = None, 0

    def check(self, pkt: FramePacket) -> str | None:
        """Returns a fault reason, or None if the frame looks healthy."""
        img = pkt.image
        if img is None or img.size == 0:
            return "empty_frame"
        small = img[::STEP, ::STEP].astype(np.float32).mean(axis=2)  # cheap grayscale
        self.last_brightness = float(small.mean())

        prev, self._prev = self._prev, small
        if prev is not None and prev.shape == small.shape:
            self.last_diff = float(np.abs(small - prev).mean())
            self._frozen = self._frozen + 1 if self.last_diff < self.cfg.freeze_diff_threshold else 0
        if self._frozen >= self.cfg.freeze_frames:
            return "frozen_frame"

        if self.last_brightness < self.cfg.min_brightness:
            return "too_dark"
        if self.last_brightness > self.cfg.max_brightness:
            return "too_bright"
        return None
