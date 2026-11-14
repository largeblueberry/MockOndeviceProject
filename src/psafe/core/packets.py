"""Data contracts passed between pipeline stages (DESIGN.md §4).

These types are the agreement between modules. Changing a field here means
every stage must be checked, so keep them small and stable.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

BBox = tuple[float, float, float, float]  # (x1, y1, x2, y2), normalized to 0..1


@dataclass(frozen=True, slots=True)
class Detection:
    cls: str      # internal standard class name: "person", "car", ...
    conf: float   # 0..1
    bbox: BBox    # normalized coordinates


@dataclass(frozen=True, slots=True)
class FramePacket:
    seq: int                 # monotonically increasing frame number
    timestamp: float         # capture time from the injected Clock (seconds)
    source_id: str
    image: np.ndarray        # H x W x 3, BGR, uint8
    meta: dict[str, Any] = field(default_factory=dict)  # dummy source puts ground truth here


@dataclass(frozen=True, slots=True)
class Detections:
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
    safe: bool          # final output: True = Y, False = N
    state: State
    reason: str         # e.g. "person_in_roi", "clear_3.0s", "stale_frame"
    seq: int            # frame that produced this decision (-1 if none)
    timestamp: float    # capture time of that frame
    decided_at: float   # clock time when the decision was made

    @property
    def yn(self) -> str:
        return "Y" if self.safe else "N"


def unsafe(state: State, reason: str, now: float, seq: int = -1, timestamp: float = -1.0) -> Decision:
    """Shorthand for an N decision. Used everywhere we fail safe."""
    return Decision(False, state, reason, seq, timestamp, now)
