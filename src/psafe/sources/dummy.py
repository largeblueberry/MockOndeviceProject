"""DummySource: renders synthetic frames from a scenario and attaches ground truth.

Lets the whole pipeline (FSM, slots, outputs) run without a camera or model.
"""

import cv2
import numpy as np

from psafe.core.clock import Clock
from psafe.core.packets import Detection, FramePacket
from psafe.sources.base import SOURCES, Source
from psafe.sources.scenario import Scenario, load_scenario

COLORS = {"car": (200, 120, 40), "person": (60, 200, 60)}  # BGR
DRAW_ORDER = {"car": 0}  # cars behind, everything else on top


@SOURCES.register("dummy")
class DummySource(Source):
    def __init__(
        self,
        clock: Clock,
        scenario: str | Scenario,
        width: int = 640,
        height: int = 480,
        loop: bool = False,
        render: bool = True,
        source_id: str = "dummy",
    ):
        super().__init__(clock, source_id)
        self.scenario = scenario if isinstance(scenario, Scenario) else load_scenario(scenario)
        self.width, self.height = width, height
        self.loop, self.render = loop, render
        self._seq = 0
        self._start: float | None = None

    def open(self) -> None:
        self._seq = 0
        self._start = self.clock.now()

    def read(self) -> FramePacket | None:
        if self._start is None:
            self.open()
        assert self._start is not None
        sc = self.scenario
        t_rel = self._seq / sc.fps  # scenario time comes from frame index -> deterministic
        if t_rel >= sc.duration:
            if not self.loop:
                return None
            t_rel %= sc.duration

        # Pace like a real camera: wait until this frame is "due".
        self.clock.sleep(self._start + self._seq / sc.fps - self.clock.now())

        objects = sc.objects_at(t_rel)
        pkt = FramePacket(
            seq=self._seq,
            timestamp=self.clock.now(),
            source_id=self.source_id,
            image=self._draw(objects),
            meta={"gt": objects, "scenario_t": t_rel},
        )
        self._seq += 1
        return pkt

    def _draw(self, objects: tuple[Detection, ...]) -> np.ndarray:
        img = np.full((self.height, self.width, 3), 90, np.uint8)
        if not self.render:
            return img
        for d in sorted(objects, key=lambda d: DRAW_ORDER.get(d.cls, 1)):
            x1, y1, x2, y2 = d.bbox
            p1 = (int(x1 * self.width), int(y1 * self.height))
            p2 = (int(x2 * self.width), int(y2 * self.height))
            color = COLORS.get(d.cls, (200, 200, 200))
            cv2.rectangle(img, p1, p2, color, -1)
            cv2.putText(img, d.cls, (p1[0] + 4, p1[1] + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
        return img
