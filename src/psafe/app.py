"""AppConfig로부터 파이프라인 부품을 생성한다. 설정의 이름을 클래스로 바꾸는
유일한 곳이므로, Runner는 구현체에 의존하지 않는다."""

from dataclasses import dataclass

from psafe.core.clock import Clock, FakeClock, RealClock
from psafe.core.config import AppConfig
from psafe.safety import HealthMonitor, SafetyFSM
from psafe.detectors import DETECTORS, Detector
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
    # 실행 중에 정해지는 값. 부품은 생성자에 같은 이름의 매개변수를 선언하면 받는다.
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
