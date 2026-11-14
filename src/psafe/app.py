"""Builds pipeline components from an AppConfig. The only place that maps
config names to classes, so runners stay independent of implementations."""

from dataclasses import dataclass

from psafe.core.clock import Clock, FakeClock, RealClock
from psafe.core.config import AppConfig
from psafe.fsm import HealthMonitor, SafetyFSM
from psafe.models import DETECTORS, Detector
from psafe.outputs import OUTPUTS, Output
from psafe.sources import SOURCES, Source


@dataclass
class Components:
    cfg: AppConfig
    clock: Clock
    source: Source
    detector: Detector
    health: HealthMonitor
    fsm: SafetyFSM
    outputs: list[Output]


def make_clock(cfg: AppConfig) -> Clock:
    kind = cfg.runner.clock
    if kind == "auto":
        kind = "fake" if cfg.runner.mode == "sync" else "real"
    return FakeClock() if kind == "fake" else RealClock()


def build(cfg: AppConfig, clock: Clock | None = None) -> Components:
    clock = clock or make_clock(cfg)
    # Runtime values a component may ask for by naming a constructor parameter.
    inject = {"clock": clock, "roi": cfg.fsm.roi, "person_conf_low": cfg.fsm.person_conf_low}
    return Components(
        cfg=cfg,
        clock=clock,
        source=SOURCES.create(cfg.source.name, inject, **cfg.source.options),
        detector=DETECTORS.create(cfg.model.name, inject, **cfg.model.options),
        health=HealthMonitor(cfg.health),
        fsm=SafetyFSM(cfg.fsm),
        outputs=[OUTPUTS.create(o.name, inject, **o.options) for o in cfg.outputs],
    )
