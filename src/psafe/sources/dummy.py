"""DummySource: 시나리오로부터 가상 프레임을 그리고 정답(ground truth)을 붙인다.

카메라나 모델 없이 파이프라인 전체(FSM, 슬롯, Output)를 돌릴 수 있게 한다.

안전에 중요한 점에서는 실제 카메라처럼 동작한다:
  * read()는 최신 프레임을 반환한다. 소비자가 느렸다면 그 사이 프레임은
    건너뛴다 (`skipped`에 집계). LatestSlot과 같은 동작이다.
  * 이미지에 센서 노이즈가 있어서 빈 장면의 두 프레임도 서로 다르다.
    노이즈가 없으면 화면 정지 감지가 "정적인 장면"과 "멈춘 카메라"를 구분할 수 없다.
  * 시나리오에 정의된 카메라 이상 (outage / blackout / freeze).

meta["gt"]    = 카메라가 볼 수 있는 것 -> DummyDetector가 이것을 보고한다
meta["world"] = 구역에 실제로 있는 것 -> 검증 / 디버깅용
"""

import cv2
import numpy as np

from psafe.core.clock import Clock
from psafe.core.packets import Detection, FramePacket
from psafe.sources.base import SOURCES, Source
from psafe.sources.scenario import Scenario, load_scenario

COLORS = {"car": (200, 120, 40), "person": (60, 200, 60)}  # BGR
DRAW_ORDER = {"car": 0}  # 차는 뒤에, 나머지는 위에 그린다
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
        self.skipped = 0       # 소비자가 느려서 버린 프레임 수
        self.outage_frames = 0  # 카메라가 만들지 않은 프레임 수
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

        # 지금까지 카메라가 만든 최신 프레임 (뒤로 가지 않는다).
        due_idx = int((self.clock.now() - self._start) * sc.fps + EPS)
        idx = max(self._next_idx, due_idx)
        self.skipped += idx - self._next_idx

        # 끊김 구간 건너뛰기: 카메라는 아무것도 만들지 않고 시간만 흐른다.
        max_skip = int(sc.duration * sc.fps) + 1  # 방어: 전 구간이 끊김인 반복 시나리오
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

        # 실제 카메라처럼 속도 조절: 이 프레임의 시각이 될 때까지 기다린다.
        self.clock.sleep(self._start + idx / sc.fps - self.clock.now())

        world = sc.objects_at(t_rel)
        image, visible = self._capture(sc.camera_at(t_rel), world)
        self._next_idx = idx + 1
        return FramePacket(
            seq=idx,
            # 읽은 시각이 아니라 촬영 시각을 쓴다: 늦게 가져온 프레임도 찍힌 시점만큼
            # 오래된 것이다. 여기서 now()를 쓰면 FSM이 오래된 프레임을 알아채지 못한다.
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
            return self._last  # 픽셀도, 보이는 객체도 완전히 같음
        if camera == "blackout":
            img = self._noisy(np.zeros((self.height, self.width, 3), np.uint8))
            return img, ()  # 렌즈 가림: 사람이 있을 수 있지만 아무것도 보이지 않는다
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
