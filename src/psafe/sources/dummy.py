"""DummySource: renders synthetic frames from a scenario and attaches ground truth.

Lets the whole pipeline (FSM, slots, outputs) run without a camera or model.

Behaves like a real camera in the ways that matter for safety:
  * read() returns the LATEST frame. If the consumer was slow, frames in
    between are skipped (counted in `skipped`), like LatestSlot would do.
  * Images carry sensor noise, so two frames of an empty scene still differ.
    Without it, freeze detection could not tell "static scene" from "stuck camera".
  * camera faults (outage / blackout / freeze) from the scenario.

meta["gt"]    = what the camera can see -> DummyDetector reports this
meta["world"] = what is really in the bay -> for checking / debugging
"""

import cv2
import numpy as np

from psafe.core.clock import Clock
from psafe.core.packets import Detection, FramePacket
from psafe.sources.base import SOURCES, Source
from psafe.sources.scenario import Scenario, load_scenario

COLORS = {"car": (200, 120, 40), "person": (60, 200, 60)}  # BGR
DRAW_ORDER = {"car": 0}  # cars behind, everything else on top
EPS = 1e-9


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
        noise_sigma: float = 2.0,
        seed: int = 0,
        source_id: str = "dummy",
    ):
        super().__init__(clock, source_id)
        self.scenario = scenario if isinstance(scenario, Scenario) else load_scenario(scenario)
        self.width, self.height = width, height
        self.loop, self.render = loop, render
        self.noise_sigma = noise_sigma
        self.seed = seed
        self.skipped = 0       # frames dropped because the consumer was slow
        self.outage_frames = 0  # frames the camera never produced
        self._next_idx = 0
        self._start: float | None = None
        self._last: tuple[np.ndarray, tuple[Detection, ...]] | None = None
        self._rng = np.random.default_rng(seed)

    def open(self) -> None:
        self._next_idx = 0
        self._start = self.clock.now()
        self._last = None
        self.skipped = self.outage_frames = 0
        self._rng = np.random.default_rng(self.seed)

    def read(self) -> FramePacket | None:
        if self._start is None:
            self.open()
        assert self._start is not None
        sc = self.scenario

        # Latest frame the camera has produced by now (never go backwards).
        due_idx = int((self.clock.now() - self._start) * sc.fps + EPS)
        idx = max(self._next_idx, due_idx)
        self.skipped += idx - self._next_idx

        # Skip over outage: the camera produces nothing, time just passes.
        max_skip = int(sc.duration * sc.fps) + 1  # guard: looping scenario that is all outage
        for _ in range(max_skip + 1):
            t_rel = self._scenario_time(idx)
            if t_rel is None:
                return None
            if sc.camera_at(t_rel) != "outage":
                break
            self.outage_frames += 1
            idx += 1
        else:
            return None

        # Pace like a real camera: wait until this frame is due.
        self.clock.sleep(self._start + idx / sc.fps - self.clock.now())

        world = sc.objects_at(t_rel)
        image, visible = self._capture(sc.camera_at(t_rel), world)
        self._next_idx = idx + 1
        return FramePacket(
            seq=idx,
            # Capture time, not read time: a frame grabbed late is still as old as
            # when it was taken. Using now() here would hide staleness from the FSM.
            timestamp=self._start + idx / sc.fps,
            source_id=self.source_id,
            image=image,
            meta={"gt": visible, "world": world, "scenario_t": t_rel},
        )

    def _scenario_time(self, idx: int) -> float | None:
        t = idx / self.scenario.fps
        if t < self.scenario.duration - EPS:
            return t
        return (t % self.scenario.duration) if self.loop else None

    def _capture(self, camera: str, world: tuple[Detection, ...]) -> tuple[np.ndarray, tuple[Detection, ...]]:
        if camera == "freeze" and self._last is not None:
            return self._last  # exact same pixels and same visible objects
        if camera == "blackout":
            img = self._noisy(np.zeros((self.height, self.width, 3), np.uint8))
            return img, ()  # lens covered: a person may be there, but nothing is visible
        img = self._noisy(self._draw(world))
        self._last = (img, world)
        return img, world

    def _noisy(self, img: np.ndarray) -> np.ndarray:
        if self.noise_sigma <= 0:
            return img
        noise = self._rng.normal(0, self.noise_sigma, img.shape)
        return np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)

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
