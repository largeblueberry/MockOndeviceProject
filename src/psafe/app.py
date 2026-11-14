"""Builds pipeline components from an AppConfig. The only place that maps
config names to classes, so runners stay independent of implementations."""

from dataclasses import dataclass

from psafe.core.clock import Clock, FakeClock, RealClock
from psafe.core.config import AppConfig
from psafe.fsm import SafetyFSM
from psafe.models import DETECTORS, Detector
from psafe.outputs import OUTPUTS, Output
from psafe.sources import SOURCES, Source


@dataclass
class Components:
    clock: Clock
    source: Source
    detector: Detector
    fsm: SafetyFSM
    outputs: list[Output]


def make_clock(cfg: AppConfig) -> Clock:
    kind = cfg.runner.clock
    if kind == "auto":
        kind = "fake" if cfg.runner.mode == "sync" else "real"
    return FakeClock() if kind == "fake" else RealClock()


def build(cfg: AppConfig, clock: Clock | None = None) -> Components:
    clock = clock or make_clock(cfg)
    return Components(
        clock=clock,
        source=SOURCES.create(cfg.source.name, clock=clock, **cfg.source.options),
        detector=DETECTORS.create(cfg.model.name, **cfg.model.options),
        fsm=SafetyFSM(cfg.fsm),
        outputs=[OUTPUTS.create(o.name, **o.options) for o in cfg.outputs],
    )
