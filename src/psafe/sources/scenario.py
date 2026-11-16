"""시나리오 파일: DummySource용 타임라인 대본 (DESIGN.md §6.1).

    fps: 15
    config:                      # 선택. 기본 설정 위에 병합되는 덮어쓰기 값
      model: { options: { miss_rate: 0.3 } }
    timeline:
      - { from: 0, to: 2, objects: [] }
      - { from: 2, to: 5, objects: [car, person] }
      - { from: 5, to: 7, objects: [person], camera: blackout }
    expect:
      - { at: 4.0, safe: false }

`objects`는 구역에 실제로 있는 것(world)이다. `camera`는 그동안 카메라가 어떻게
동작하는지를 나타낸다:
    ok       정상 프레임 (기본값)
    outage   프레임이 아예 없음 (케이블 분리, 드라이버 오류)
    blackout 검은 프레임 (렌즈 가림) -> 모델은 아무것도 못 본다
    freeze   마지막 프레임 반복 (드라이버 멈춤) -> 모델은 예전 장면을 본다
위험한 것은 blackout/freeze다: 실제로는 사람이 있는데 모델은 "비어 있음"이라고
답한다. health 검사(M3)만이 이를 잡을 수 있다.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from psafe.core.packets import BBox, Detection

# 시나리오에 bbox가 없을 때 객체가 나타나는 기본 위치.
DEFAULT_BBOX: dict[str, BBox] = {
    "car": (0.20, 0.35, 0.80, 0.85),
    "person": (0.40, 0.30, 0.55, 0.80),
}

CAMERA_MODES = ("ok", "outage", "blackout", "freeze")


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    objects: tuple[Detection, ...]
    camera: str = "ok"


@dataclass(frozen=True)
class Expectation:
    at: float
    safe: bool


@dataclass(frozen=True)
class Scenario:
    name: str
    fps: float
    segments: tuple[Segment, ...]
    expect: tuple[Expectation, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)
    description: str = ""

    @property
    def duration(self) -> float:
        return max((s.end for s in self.segments), default=0.0)

    def segment_at(self, t: float) -> Segment | None:
        for s in self.segments:
            if s.start <= t < s.end:
                return s
        return None  # 타임라인의 빈 구간 = 빈 장면, 카메라 정상

    def objects_at(self, t: float) -> tuple[Detection, ...]:
        seg = self.segment_at(t)
        return seg.objects if seg else ()

    def camera_at(self, t: float) -> str:
        seg = self.segment_at(t)
        return seg.camera if seg else "ok"


def _parse_object(obj: Any, where: str) -> Detection:
    if isinstance(obj, str):
        if obj not in DEFAULT_BBOX:
            raise ValueError(f"{where}: no default bbox for '{obj}', give {{cls, bbox}}")
        return Detection(obj, 1.0, DEFAULT_BBOX[obj])
    if isinstance(obj, dict) and "cls" in obj:
        bbox = tuple(float(v) for v in obj.get("bbox", DEFAULT_BBOX.get(obj["cls"], ())))
        if len(bbox) != 4:
            raise ValueError(f"{where}: bbox must be [x1, y1, x2, y2]")
        return Detection(obj["cls"], 1.0, bbox)  # type: ignore[arg-type]
    raise ValueError(f"{where}: object must be a class name or {{cls, bbox}}")


def parse_scenario(data: dict[str, Any], name: str = "inline") -> Scenario:
    segments = []
    for i, seg in enumerate(data.get("timeline", [])):
        where = f"{name}.timeline[{i}]"
        start, end = float(seg["from"]), float(seg["to"])
        if end <= start:
            raise ValueError(f"{where}: 'to' must be after 'from'")
        camera = seg.get("camera", "ok")
        if camera not in CAMERA_MODES:
            raise ValueError(f"{where}: camera must be one of {CAMERA_MODES}, got {camera!r}")
        objs = tuple(_parse_object(o, where) for o in seg.get("objects", []))
        segments.append(Segment(start, end, objs, camera))
    expect = tuple(Expectation(float(e["at"]), bool(e["safe"])) for e in data.get("expect", []))
    return Scenario(
        name=name,
        fps=float(data.get("fps", 15)),
        segments=tuple(segments),
        expect=expect,
        config=data.get("config", {}) or {},
        description=data.get("description", ""),
    )


def load_scenario(path: str | Path) -> Scenario:
    path = Path(path)
    with open(path, encoding="utf-8") as f:
        return parse_scenario(yaml.safe_load(f) or {}, name=path.stem)
