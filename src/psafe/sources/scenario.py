"""Scenario files: scripted timelines for DummySource (DESIGN.md §6.1).

    fps: 15
    config:                      # optional overrides merged into the base config
      model: { options: { miss_rate: 0.3 } }
    timeline:
      - { from: 0, to: 2, objects: [] }
      - { from: 2, to: 5, objects: [car, person] }
      - { from: 5, to: 7, objects: [person], camera: blackout }
    expect:
      - { at: 4.0, safe: false }

`objects` is what is really in the bay (the world). `camera` says what the
camera does meanwhile:
    ok       normal frames (default)
    outage   no frames at all (cable pulled, driver crash)
    blackout black frames (lens covered) -> model sees nothing
    freeze   last frame repeated (driver stuck) -> model sees the old scene
blackout/freeze are the dangerous ones: the world has a person, the model
says "empty". Only health checks (M3) can catch them.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from psafe.core.packets import BBox, Detection

# Where an object appears when the scenario doesn't give a bbox.
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
        return None  # gap in the timeline = empty scene, camera ok

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
