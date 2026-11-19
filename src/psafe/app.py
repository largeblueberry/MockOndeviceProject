"""AppConfig로부터 파이프라인 부품을 생성한다. 설정의 이름을 클래스로 바꾸는
유일한 곳이므로, Runner는 구현체에 의존하지 않는다."""

from dataclasses import dataclass

from psafe.core.clock import Clock, FakeClock, RealClock
from psafe.core.config import AppConfig, parse_address
from psafe.link.node import NodeLink
from psafe.link.transport import Transport, UdpTransport
from psafe.safety.confirm import ConfirmTracker
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
    link: NodeLink | None = None           # C와 통신 (cfg.link가 있을 때만)
    confirm: ConfirmTracker | None = None  # link와 항상 함께 있다


def make_clock(cfg: AppConfig) -> Clock:
    kind = cfg.runner.clock
    if kind == "auto":
        kind = "fake" if cfg.runner.mode == "sync" else "real"
    return FakeClock() if kind == "fake" else RealClock()


def build(cfg: AppConfig, clock: Clock | None = None, transport: Transport | None = None) -> Components:
    """transport: 테스트에서 가짜 네트워크를 넣을 때. 없으면 cfg.link.bind로 UDP를 쓴다."""
    clock = clock or make_clock(cfg)
    # 실행 중에 정해지는 값. 부품은 생성자에 같은 이름의 매개변수를 선언하면 받는다.
    inject = {"clock": clock, "roi": cfg.fsm.roi, "person_conf_low": cfg.fsm.person_conf_low}
    link = confirm = None
    if cfg.link is not None:
        lc = cfg.link
        link = NodeLink(
            lc.node, transport or UdpTransport(parse_address(lc.bind)), parse_address(lc.hub), clock,
            hub_node=lc.hub_node, timeout_s=lc.node_timeout_ms / 1000.0,
        )
        confirm = ConfirmTracker(lc.confirm_frames)
    return Components(
        cfg=cfg,
        clock=clock,
        source=SOURCES.create(cfg.source.name, inject, **cfg.source.options),
        detector=DETECTORS.create(cfg.model.name, inject, **cfg.model.options),
        health=HealthMonitor(cfg.health),
        fsm=SafetyFSM(cfg.fsm),
        outputs=[OUTPUTS.create(o.name, inject, **o.options) for o in cfg.outputs],
        link=link,
        confirm=confirm,
    )
