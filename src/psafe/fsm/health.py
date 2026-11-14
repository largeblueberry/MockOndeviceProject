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
        self.last_contrast = 0.0
        self._ranges: dict[str, list[float]] = {}   # metric -> [min, max] since take_ranges()

    def reset(self) -> None:
        self._prev, self._frozen = None, 0

    def _track(self, name: str, v: float) -> None:
        r = self._ranges.get(name)
        if r is None:
            self._ranges[name] = [v, v]
        else:
            r[0], r[1] = min(r[0], v), max(r[1], v)

    def take_ranges(self) -> dict[str, tuple[float, float]]:
        """{metric: (min, max)} since the previous call (brightness, contrast, diff),
        then reset. For tuning logs only - approximate when read from another thread."""
        ranges, self._ranges = self._ranges, {}
        return {k: (v[0], v[1]) for k, v in ranges.items()}

    def check(self, pkt: FramePacket) -> str | None:
        """Returns a fault reason, or None if the frame looks healthy."""
        img = pkt.image
        if img is None or img.size == 0:
            return "empty_frame"
        small = img[::STEP, ::STEP].astype(np.float32).mean(axis=2)  # cheap grayscale
        self.last_brightness = float(small.mean())
        self.last_contrast = float(small.std())      # ~0 for a covered lens / blank wall
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
