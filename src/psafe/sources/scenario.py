"""Scenario files: scripted timelines for DummySource (DESIGN.md §6.1)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from psafe.core.packets import BBox, Detection

# Where an object appears when the scenario doesn't give a bbox.
DEFAULT_BBOX: dict[str, BBox] = {
    "car": (0.20, 0.35, 0.80, 0.85),
    "person": (0.40, 0.30, 0.55, 0.80),
}


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    objects: tuple[Detection, ...]


@dataclass(frozen=True)
class Expectation:
    at: float
    safe: bool


@dataclass(frozen=True)
class Scenario:
    name: str
    fps: float
    segments: tuple[Segment, ...]
    expect: tuple[Expectation, ...]

    @property
    def duration(self) -> float:
        return max((s.end for s in self.segments), default=0.0)

    def objects_at(self, t: float) -> tuple[Detection, ...]:
        for s in self.segments:
            if s.start <= t < s.end:
                return s.objects
        return ()  # gaps in the timeline = empty scene


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
        objs = tuple(_parse_object(o, where) for o in seg.get("objects", []))
        segments.append(Segment(start, end, objs))
    expect = tuple(Expectation(float(e["at"]), bool(e["safe"])) for e in data.get("expect", []))
    return Scenario(name, float(data.get("fps", 15)), tuple(segments), expect)


def load_scenario(path: str | Path) -> Scenario:
    path = Path(path)
    with open(path, encoding="utf-8") as f:
        return parse_scenario(yaml.safe_load(f) or {}, name=path.stem)
