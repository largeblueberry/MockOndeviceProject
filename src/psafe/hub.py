"""C (허브) 실행 (DESIGN.md §7.4, §7.5). 카메라·모델 없이 A·B의 Report를 모아 판정한다.

    rx         Report 수신 -> decode -> PeerTracker            (tracker는 락으로 보호)
    heartbeat  heartbeat_ms마다: Aggregator.step -> Verdict -> 모든 VerdictOutput.show
               + 각 노드에 Control 송신 (판정에 쓴 confirm_id)
    main       감독: 스레드 생존, Ctrl+C, 출력의 poll(), 지표 로그

종료할 때는 출력에 UNKNOWN(shutdown)을, 노드에는 confirm_id 없는 Control을 보낸다.
비정상 종료로 이것이 못 나가도 ESP32 워치독(LED)과 노드의 무응답 판정이 처리한다.
"""

import logging
import secrets
import threading
from collections import OrderedDict
from contextlib import ExitStack
from dataclasses import dataclass, field

from psafe.core.clock import Clock, FakeClock, RealClock
from psafe.core.config import HubAppConfig, parse_address
from psafe.core.verdict import NodeView, Verdict, VerdictKind
from psafe.link.messages import MAX_SEQ, Control, Report, decode, encode, new_boot_id
from psafe.link.peer import PeerTracker
from psafe.link.transport import Address, Transport, UdpTransport
from psafe.outputs import VERDICT_OUTPUTS, VerdictOutput
from psafe.safety.aggregator import Aggregator

log = logging.getLogger("psafe.hub")

# 기억할 Control 송신 시각 수. 200 ms마다 1개 -> 64개 = 12.8초. 이보다 오래된 Control을 echo하면
# 왕복 시간을 모르는 것으로 본다 (= 근거 나이를 모름 -> EMPTY 불가).
SENT_HISTORY = 64


@dataclass
class HubResult:
    verdicts: list[Verdict] = field(default_factory=list)   # record=True일 때만
    heartbeats: int = 0
    error: str | None = None


class Hub:
    """C의 부품 묶음. 수신 처리와 판정 1회를 함수로 제공한다 (Runner와 시뮬레이션이 공유)."""

    def __init__(self, cfg: HubAppConfig, clock: Clock, transport: Transport | None = None,
                 outputs: list[VerdictOutput] | None = None, first_confirm_id: int | None = None):
        h = cfg.hub
        self.cfg = cfg
        self.clock = clock
        self.node = h.node
        self.boot = new_boot_id()
        self.transport = transport or UdpTransport(parse_address(h.bind))
        self.peers: dict[str, Address] = {n.name: parse_address(n.address) for n in h.nodes}
        self.tracker: PeerTracker[Report] = PeerTracker(h.node_timeout_ms / 1000.0, expected=set(self.peers))
        self.aggregator = Aggregator(
            list(self.peers), max_evidence_age_ms=h.max_evidence_age_ms,
            confirm_timeout_s=h.confirm_timeout_s, first_confirm_id=first_confirm_id,
        )
        inject = {"clock": clock}
        self.outputs = outputs if outputs is not None else [
            VERDICT_OUTPUTS.create(o.name, inject, **o.options) for o in cfg.outputs
        ]
        self._lock = threading.Lock()
        # Control seq는 부팅마다 임의 값에서 시작한다: 재시작 전 seq에 대한 echo를 새 seq로 오인하지 않게
        self._seq = secrets.randbelow(2**31)
        self._sent: OrderedDict[int, float] = OrderedDict()   # Control seq -> 보낸 시각 (C 시계)
        self.counts: dict[str, int] = {"wrong_type": 0, "no_echo": 0}

    # ---- 수신 --------------------------------------------------------------

    def poll(self, timeout: float) -> bool:
        """데이터그램 1개를 처리한다. 받은 것이 없으면 False."""
        got = self.transport.recv(timeout)
        if got is None:
            return False
        msg = decode(got[0])
        if isinstance(msg, str):
            self._count(msg)
        elif not isinstance(msg, Report):
            self._count("wrong_type")
        else:
            now = self.clock.now()
            transit = self._transit(msg, now)
            with self._lock:
                r = self.tracker.feed(msg, now, transit)
            if r not in ("ok", "restart"):          # restart는 받아들인 것이다 (tracker.counts에 따로 있음)
                self._count(r)
        return True

    # ---- 판정 + 송신 --------------------------------------------------------

    def tick(self) -> Verdict:
        """하트비트 1회: 판정 -> 출력 -> Control 송신."""
        now = self.clock.now()
        with self._lock:
            latest = {n: self.tracker.latest(n, now) for n in self.peers}
        verdict = self.aggregator.step(latest, now)
        self.show_all(verdict)
        self.send_control(verdict.confirm_id)
        return verdict

    def show_all(self, verdict: Verdict) -> None:
        for o in self.outputs:
            try:
                o.show(verdict)
            except Exception:
                log.exception("verdict output %s failed", type(o).__name__)

    def _transit(self, r: Report, now: float) -> float | None:
        """노드 -> C 전달 시간의 상한 = 왕복 시간 (E-21). 모두 C 시계의 길이로만 계산한다:
            RTT = (Report 받은 시각 - echo한 Control을 보낸 시각) - 노드가 들고 있던 시간
        한 방향 지연은 왕복 시간보다 클 수 없다. echo가 없거나 모르는 seq면 None (= 근거 나이를 모름)."""
        if r.echo_seq is None or r.echo_seq not in self._sent:
            self._count("no_echo")
            return None
        rtt = (now - self._sent[r.echo_seq]) - r.echo_hold_ms / 1000.0
        return max(0.0, rtt)

    def send_control(self, confirm_id: int | None) -> None:
        self._seq = (self._seq + 1) % (MAX_SEQ + 1)
        self._sent[self._seq] = self.clock.now()
        while len(self._sent) > SENT_HISTORY:
            self._sent.popitem(last=False)
        data = encode(Control(self.node, self.boot, self._seq, confirm_id))
        for addr in self.peers.values():
            self.transport.send(data, addr)

    def shutdown_verdict(self) -> Verdict:
        now = self.clock.now()
        views = tuple(NodeView(n, False, None, 0, "shutdown", None, None) for n in self.peers)
        return Verdict(VerdictKind.UNKNOWN, "shutdown", views, None, now)

    def _count(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1


class HubRunner:
    JOIN_TIMEOUT_S = 2.0

    def __init__(self, hub: Hub, record: bool = False, max_seconds: float | None = None):
        if isinstance(hub.clock, FakeClock):
            raise ValueError("HubRunner needs a real clock")
        self.hub = hub
        self.record = record
        self.max_seconds = max_seconds
        h = hub.cfg.hub
        self.period_s = h.heartbeat_ms / 1000.0
        self.metrics_interval_s = h.metrics_interval_s
        self.result = HubResult()
        self._stop = threading.Event()
        self._crashed: dict[str, str] = {}
        self._latest: Verdict | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> HubResult:
        hub, r = self.hub, self.result
        with ExitStack() as stack:
            for o in hub.outputs:
                o.open()
                stack.callback(o.close)
            hub.transport.open()
            stack.callback(hub.transport.close)
            threads = [
                threading.Thread(target=self._guard("rx", self._rx_loop), name="hub-rx", daemon=True),
                threading.Thread(target=self._guard("heartbeat", self._heartbeat_loop), name="heartbeat", daemon=True),
            ]
            for t in threads:
                t.start()
            log.info("hub %s (boot %s) listening on %s, nodes %s",
                     hub.node, hub.boot, hub.cfg.hub.bind, ", ".join(f"{k}={v[0]}:{v[1]}" for k, v in hub.peers.items()))
            try:
                self._supervise(threads)
            except KeyboardInterrupt:
                log.info("interrupted")
            finally:
                self._stop.set()
                for t in threads:
                    t.join(self.JOIN_TIMEOUT_S)
                    if t.is_alive():
                        log.warning("thread %s did not stop; abandoning it", t.name)
                hub.show_all(hub.shutdown_verdict())
                try:
                    hub.send_control(None)
                except Exception:
                    log.exception("final control failed")
        log.info("hub done: %d heartbeats, rx %s, rejected %s%s",
                 r.heartbeats, hub.tracker.counts, hub.counts, f", error: {r.error}" if r.error else "")
        return r

    def _guard(self, name, fn):
        def wrapped():
            try:
                fn()
            except Exception as e:
                log.exception("%s thread crashed", name)
                self._crashed[name] = f"{type(e).__name__}: {e}"
        return wrapped

    def _supervise(self, threads: list[threading.Thread]) -> None:
        hub, r = self.hub, self.result
        start = hub.clock.now()
        next_metrics = start + self.metrics_interval_s
        while not self._stop.is_set():
            if self._crashed:
                r.error = "; ".join(f"{k}: {v}" for k, v in self._crashed.items())
                log.error("stopping: %s", r.error)
                return
            if dead := [t.name for t in threads if not t.is_alive()]:
                r.error = f"thread(s) exited unexpectedly: {dead}"
                log.error("stopping: %s", r.error)
                return
            now = hub.clock.now()
            if self.max_seconds is not None and now - start >= self.max_seconds:
                return
            if now >= next_metrics:
                v = self._latest
                log.info("status: %s | rx %s | rejected %s",
                         f"{v.kind.value} ({v.reason})" if v else "-", hub.tracker.counts, hub.counts)
                next_metrics = now + self.metrics_interval_s
            keep = True
            for o in hub.outputs:
                try:
                    keep = o.poll() and keep
                except Exception:
                    log.exception("verdict output %s.poll failed", type(o).__name__)
            if not keep:
                log.info("stop requested by output")
                return
            self._stop.wait(0.03)

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            self.hub.poll(0.05)

    def _heartbeat_loop(self) -> None:
        hub, r = self.hub, self.result
        next_t = hub.clock.now()
        while not self._stop.is_set():
            v = hub.tick()
            self._latest = v
            r.heartbeats += 1
            if self.record:
                r.verdicts.append(v)
            next_t += self.period_s
            delay = next_t - hub.clock.now()
            if delay < 0:
                next_t, delay = hub.clock.now(), 0.0
            self._stop.wait(delay)


def build_hub(cfg: HubAppConfig, clock: Clock | None = None, transport: Transport | None = None) -> Hub:
    return Hub(cfg, clock or RealClock(), transport)
