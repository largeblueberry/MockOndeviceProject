"""다중 보드 통합 시뮬레이션 (DESIGN.md §12 M6.5). A·B·C를 한 프로세스에서 결정적으로 돌린다.

    * 시간: 공유 FakeClock 하나가 '실제 시간'이다. 노드와 C는 각자 OffsetClock을 쓴다
      -> 파이마다 시계가 다른 상황 (E-17). 시각을 보드끼리 잘못 비교하는 코드가 있으면
      오프셋을 바꿨을 때 결과가 달라진다.
    * 네트워크: LoopbackNetwork (유실·지연·중복·깨짐·단절 주입). 도착 시각은 실제 시간 기준.
    * 실행: 이벤트 큐. 노드 프레임 / 노드 하트비트 / C 하트비트를 시각 순서대로 처리한다.
      ThreadedRunner와 같은 함수(process_frame, step_confirm, expire_decision,
      send_report)를 쓰므로 판단 로직은 실제 실행과 같다. 추론 시간은 0으로 본다.
    * 정답: 각 노드 시나리오의 world(실제로 있는 사람)를 프레임마다 기록한다.

안전 불변식 (check_invariant):
    C가 EMPTY를 낸 시각 t에, 어느 노드든 [t - W, t - L] 사이에 '위험'(ROI 안 사람, 또는
    노드가 꺼져 있어 모름)이 있었으면 위반이다.
      L = 반응 지연 상한: 프레임 간격 + 노드 하트비트 + 노드 무응답 기준 + C 하트비트 + 최대 네트워크 지연
          (보고가 계속 유실되면 C는 마지막 CLEAR를 무응답 기준까지 들고 있다)
      W = 노드의 clear_time_s: 사람이 한 번 보이면 노드가 다시 CLEAR가 되기까지의 최소 시간
    사람이 계속 있으면 [t-W, t-L] 안에 위험 표본이 반드시 있으므로 잡힌다.
"""

import bisect
import heapq
import logging
import random
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Callable

from psafe.app import Components, build
from psafe.core.clock import FakeClock, OffsetClock
from psafe.core.config import AppConfig, HubAppConfig, parse_address
from psafe.core.packets import Decision, State, unsafe
from psafe.core.verdict import Verdict, VerdictKind
from psafe.hub import Hub
from psafe.link.transport import Address, LinkFault, LoopbackNetwork
from psafe.runners import expire_decision, expired_confirm, open_all, process_frame, send_report, step_confirm
from psafe.safety.confirm import NO_CONFIRM, ConfirmStatus
from psafe.safety.roi import Roi
from psafe.sources.scenario import Scenario, load_scenario

log = logging.getLogger("psafe.sim")

# 같은 시각의 이벤트 처리 순서: 프레임 -> 노드 하트비트 -> C 하트비트 -> 예약 동작
_FRAME, _NODE_HB, _HUB_HB, _ACTION = 0, 1, 2, 3


@dataclass
class _Node:
    name: str
    cfg: AppConfig
    clock: OffsetClock
    address: Address
    c: Components | None = None
    stack: ExitStack | None = None
    alive: bool = False
    gen: int = 0                       # 재시작할 때마다 증가 (옛 이벤트 무시용)
    open_at: float = 0.0               # 실제 시간 기준, Source를 연 시각
    decision: Decision | None = None
    confirm: ConfirmStatus = NO_CONFIRM
    last_decided: float = 0.0          # 노드 시계 기준


@dataclass
class SimResult:
    verdicts: list[tuple[float, Verdict]] = field(default_factory=list)      # (실제 시각, 판정)
    hazards: list[tuple[float, str, bool]] = field(default_factory=list)     # (실제 시각, 노드, 위험 여부)
    node_decisions: dict[str, list[tuple[float, Decision]]] = field(default_factory=dict)
    events: list[tuple[float, str]] = field(default_factory=list)            # 예약 동작 기록

    def kinds(self) -> list[tuple[VerdictKind, str]]:
        return [(v.kind, v.reason) for _, v in self.verdicts]

    def first(self, kind: VerdictKind, after: float = 0.0) -> float | None:
        return next((t for t, v in self.verdicts if v.kind == kind and t >= after), None)


class MultiSim:
    """
    nodes:   {이름: AppConfig}  각 설정은 dummy Source + link: 를 가져야 한다.
             link.hub는 C의 주소, link.bind는 그 노드의 주소 (LoopbackNetwork에서 쓰는 이름)
    hub:     C 설정 (hub.bind, hub.nodes[].address가 위 주소와 맞아야 한다)
    offsets: {이름: 초}  노드·C('hub' 키) 시계가 실제 시간과 다른 정도
    """

    def __init__(self, nodes: dict[str, AppConfig], hub: HubAppConfig, offsets: dict[str, float] | None = None,
                 seed: int = 0, start: float = 1000.0):
        offsets = offsets or {}
        self.clock = FakeClock(start)
        self._start = start
        self.rng = random.Random(seed)
        self.net = LoopbackNetwork(self.clock, seed=seed)
        self.result = SimResult()
        self._q: list[tuple[float, int, int, Callable[[], None]]] = []
        self._n = 0
        self.nodes: dict[str, _Node] = {}
        for name, cfg in nodes.items():
            if cfg.link is None or cfg.source.name != "dummy":
                raise ValueError(f"node {name}: needs a dummy source and a link: section")
            self.nodes[name] = _Node(name, cfg, OffsetClock(self.clock, offsets.get(name, 0.0)),
                                     parse_address(cfg.link.bind))
            self.result.node_decisions[name] = []
        self.hub_clock = OffsetClock(self.clock, offsets.get("hub", 0.0))
        self.hub_address = parse_address(hub.hub.bind)
        self.hub = Hub(hub, self.hub_clock, transport=self.net.endpoint(self.hub_address), outputs=[],
                       first_confirm_id=self.rng.randrange(2**31))
        self.hub_period = hub.hub.heartbeat_ms / 1000.0

    # ---- 예약 ---------------------------------------------------------------

    def at(self, t: float, action: Callable[["MultiSim"], None], label: str = "") -> None:
        """실제 시간 t(시작 기준 초)에 동작을 실행한다. 예: lambda s: s.net.cut(a, c)"""
        def run() -> None:
            self.result.events.append((self.t, label or getattr(action, "__name__", "action")))
            action(self)
        self._push(self.start + t, _ACTION, run)

    def kill(self, name: str) -> None:
        """강제 종료: 마지막 N도 보내지 못한다 (전원 차단과 같다)."""
        n = self.nodes[name]
        if n.alive:
            n.alive = False
            if n.stack is not None:
                n.stack.close()
                n.stack = None

    def boot(self, name: str) -> None:
        """노드를 (다시) 켠다. 새 boot id, 시나리오는 처음부터."""
        n = self.nodes[name]
        self.kill(name)
        n.gen += 1
        n.c = build(n.cfg, clock=n.clock, transport=self.net.endpoint(n.address))
        n.stack = ExitStack()
        open_all(n.c, n.stack)
        n.alive = True
        n.open_at = self.clock.now()
        n.decision, n.confirm, n.last_decided = None, NO_CONFIRM, n.clock.now()
        gen = n.gen
        fps = n.c.source.scenario.fps
        self._schedule_frame(n, gen, 0, fps)
        period = n.cfg.runner.heartbeat_ms / 1000.0
        self._push(self.clock.now() + self.rng.random() * period, _NODE_HB, lambda: self._node_hb(n, gen, period))

    @property
    def start(self) -> float:
        return self._start

    @property
    def t(self) -> float:
        """시작 기준 실제 시간 (초)."""
        return self.clock.now() - self._start

    # ---- 실행 ---------------------------------------------------------------

    def run(self, seconds: float) -> SimResult:
        self.hub.transport.open()
        for name in self.nodes:
            self.boot(name)
        self._push(self._start + self.rng.random() * self.hub_period, _HUB_HB, self._hub_hb)
        end = self._start + seconds
        while self._q and self._q[0][0] <= end:
            when, _, _, fn = heapq.heappop(self._q)
            if when > self.clock.now():
                self.clock.advance(when - self.clock.now())
            fn()
        for name in self.nodes:
            self.kill(name)
        self.hub.transport.close()
        return self.result

    def _push(self, when: float, prio: int, fn: Callable[[], None]) -> None:
        self._n += 1
        heapq.heappush(self._q, (when, prio, self._n, fn))

    # ---- 노드 ---------------------------------------------------------------

    def _schedule_frame(self, n: _Node, gen: int, k: int, fps: float) -> None:
        self._push(n.open_at + k / fps, _FRAME, lambda: self._frame(n, gen, k, fps))

    def _frame(self, n: _Node, gen: int, k: int, fps: float) -> None:
        if not n.alive or gen != n.gen:
            return
        c = n.c
        sc = c.source.scenario
        t_rel = k / fps
        if t_rel >= sc.duration - 1e-9:                     # 시나리오 끝 = 정상 종료
            self._node_shutdown(n)
            return
        self._record_truth(n, sc, t_rel)
        while c.link.poll(0):                               # 지금까지 도착한 Control 반영
            pass
        now = n.clock.now()
        if sc.camera_at(t_rel) == "outage":
            # 카메라가 프레임을 안 줌. worker의 대기 시간 초과를 흉내 낸다.
            if now - n.last_decided >= c.cfg.fsm.max_frame_age_ms / 1000.0:
                self._set(n, c.fsm.on_no_frame(now))
        else:
            pkt = c.source.read()                           # 프레임 시각과 같으므로 기다리지 않는다
            if pkt is None:
                self._node_shutdown(n)
                return
            decision, _ = process_frame(c, pkt)
            self._set(n, decision)
        self._schedule_frame(n, gen, k + 1, fps)

    def _set(self, n: _Node, d: Decision) -> None:
        n.decision, n.confirm = d, step_confirm(n.c, d)
        n.last_decided = d.decided_at
        self.result.node_decisions[n.name].append((self.t, d))

    def _node_hb(self, n: _Node, gen: int, period: float) -> None:
        if not n.alive or gen != n.gen:
            return
        c = n.c
        now = n.clock.now()
        d, confirm = n.decision, n.confirm
        if d is None:
            d = unsafe(State.INIT, "no_decision_yet", now)
        elif (stale := expire_decision(d, now, c.cfg.runner.max_decision_age_ms / 1000.0)) is not None:
            d, confirm = stale, expired_confirm(confirm)
        for o in c.outputs:
            o.send(d)
        send_report(c, d, confirm)
        self._push(self.clock.now() + period, _NODE_HB, lambda: self._node_hb(n, gen, period))

    def _node_shutdown(self, n: _Node) -> None:
        """ThreadedRunner 종료와 같이 마지막으로 N(shutdown)을 보내고 끈다."""
        c = n.c
        final = unsafe(State.FAULT, "shutdown", n.clock.now())
        for o in c.outputs:
            o.send(final)
        send_report(c, final, expired_confirm(n.confirm))
        self.kill(n.name)

    def _record_truth(self, n: _Node, sc, t_rel: float) -> None:
        roi = Roi(n.cfg.fsm.roi)
        present = any(o.cls == "person" and roi.touches(o.bbox) for o in sc.objects_at(t_rel))
        self.result.hazards.append((self.t, n.name, present))

    # ---- C ------------------------------------------------------------------

    def _hub_hb(self) -> None:
        # 꺼진 노드 구역은 '모름' = 위험으로 기록한다
        for name, n in self.nodes.items():
            if not n.alive:
                self.result.hazards.append((self.t, name, True))
        while self.hub.poll(0):
            pass
        v = self.hub.tick()
        self.result.verdicts.append((self.t, v))
        self._push(self.clock.now() + self.hub_period, _HUB_HB, self._hub_hb)


# ---------------------------------------------------------------------------
# 안전 불변식
# ---------------------------------------------------------------------------

def reaction_bound(nodes: dict[str, AppConfig], hub: HubAppConfig, max_net_delay_s: float = 0.0) -> float:
    """L: 사람이 처음 보인 뒤 C가 EMPTY를 멈추기까지 걸릴 수 있는 최대 시간 (모듈 docstring)."""
    h = hub.hub
    worst = 0.0
    for cfg in nodes.values():
        sc = cfg.source.options["scenario"]
        fps = sc.fps if isinstance(sc, Scenario) else load_scenario(sc).fps
        worst = max(worst, 1 / fps + cfg.runner.heartbeat_ms / 1000.0)
    return worst + h.node_timeout_ms / 1000.0 + h.heartbeat_ms / 1000.0 + max_net_delay_s + 0.01


def check_invariant(result: SimResult, nodes: dict[str, AppConfig], reaction_s: float) -> list[str]:
    """위반 목록. 비어 있으면 통과."""
    window = min(cfg.fsm.clear_time_s for cfg in nodes.values())
    if reaction_s >= window:
        raise ValueError(f"reaction bound {reaction_s:.2f}s must be < clear_time_s {window:.2f}s")
    hz = sorted((t, name) for t, name, bad in result.hazards if bad)
    out = []
    times = [t for t, _ in hz]
    for t, v in result.verdicts:
        if v.kind != VerdictKind.EMPTY:
            continue
        i = bisect.bisect_left(times, t - window)
        j = bisect.bisect_right(times, t - reaction_s)
        if i < j:
            s, name = hz[i]
            out.append(f"EMPTY at t={t:.2f}s but {name} had a person/unknown at t={s:.2f}s")
    return out


__all__ = ["MultiSim", "SimResult", "LinkFault", "check_invariant", "reaction_bound"]
