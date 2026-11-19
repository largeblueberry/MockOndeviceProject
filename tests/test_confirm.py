"""A·B 쪽 CONFIRM: Decision.count, ConfirmTracker, NodeLink, Runner 연결."""

import threading
import time

import pytest

from psafe.app import build
from psafe.core.clock import FakeClock, RealClock
from psafe.core.config import ConfigError, FsmConfig, parse_config
from psafe.core.packets import Decision, Detection, FrameDetections, State, unsafe
from psafe.link.messages import Control, Report, decode, encode
from psafe.link.node import NodeLink
from psafe.link.transport import Address, LoopbackNetwork, Transport
from psafe.runners import SyncRunner, ThreadedRunner
from psafe.safety.confirm import NO_CONFIRM, ConfirmRequest, ConfirmTracker
from psafe.safety.fsm import SafetyFSM
from psafe.sources.scenario import parse_scenario

HUB: Address = ("c", 5005)
NODE: Address = ("a", 5006)


# ---- Decision.count -------------------------------------------------------

def test_fsm_counts_people_in_roi():
    fsm = SafetyFSM(FsmConfig(roi=[[0, 0], [0.5, 0], [0.5, 1], [0, 1]]))
    inside = Detection("person", 0.9, (0.1, 0.1, 0.2, 0.3))
    outside = Detection("person", 0.9, (0.7, 0.1, 0.8, 0.3))
    weak = Detection("person", 0.1, (0.1, 0.1, 0.2, 0.3))
    d = fsm.step(FrameDetections(0, 0.0, (inside, inside, outside, weak), "m", 1.0), 0.0)
    assert (d.state, d.count) == (State.OCCUPIED, 2)
    d = fsm.step(FrameDetections(1, 0.1, (outside,), "m", 1.0), 0.1)
    assert (d.state, d.count) == (State.VERIFYING, 0)
    assert unsafe(State.FAULT, "x", 0.0).count == 0


# ---- ConfirmTracker -------------------------------------------------------

def dec(ts: float, state=State.VERIFYING, count=0, seq=1) -> Decision:
    return Decision(state == State.CLEAR, state, "x", seq, ts, ts, count)


REQ = ConfirmRequest(("cboot", 7), since=10.0)


def test_confirm_needs_k_frames_after_request():
    t = ConfirmTracker(frames_needed=3)
    assert t.step(None, dec(9.0)) == NO_CONFIRM
    assert t.step(REQ, dec(9.9, State.CLEAR)).result == "pending"     # 요청 전 프레임: 세지 않음
    assert t.step(REQ, dec(10.0, State.CLEAR)).result == "pending"    # 요청과 같은 시각도 세지 않음
    assert t.step(REQ, dec(10.1, State.CLEAR)).result == "pending"
    assert t.step(REQ, dec(10.2, State.CLEAR)).result == "pending"
    s = t.step(REQ, dec(10.3, State.CLEAR))
    assert (s.confirm_id, s.result) == (7, "empty")


def test_confirm_occupied_and_fault_are_sticky_until_new_id():
    t = ConfirmTracker(frames_needed=2)
    t.step(REQ, dec(10.1))
    t.step(REQ, dec(10.2))
    assert t.status.result == "empty"
    assert t.step(REQ, dec(10.3, State.OCCUPIED, count=1)).result == "occupied"   # 확인 후에도 사람 -> 뒤집힘
    assert t.step(REQ, dec(10.4)).result == "occupied"
    assert t.step(REQ, dec(10.5)).result == "occupied"
    new = ConfirmRequest(("cboot", 8), since=10.5)
    assert t.step(new, dec(10.6)).result == "pending"
    assert t.step(new, dec(10.7, State.FAULT)).result == "fault"
    assert t.step(new, dec(10.8)).result == "fault"


def test_confirm_frameless_fault_and_reset():
    t = ConfirmTracker(frames_needed=1)
    assert t.step(REQ, unsafe(State.FAULT, "no_frame", 10.5)).result == "fault"
    assert t.step(None, dec(10.6)) == NO_CONFIRM                       # C 무응답 -> 초기화
    assert t.step(REQ, dec(10.7)).result == "empty"                    # 같은 요청이 다시 와도 처음부터


def test_confirm_c_restart_with_same_id_is_a_new_request():
    t = ConfirmTracker(frames_needed=1)
    t.step(REQ, dec(10.1))
    assert t.status.result == "empty"
    again = ConfirmRequest(("newboot", 7), since=11.0)                 # C가 재시작해 같은 번호
    assert t.step(again, dec(10.9)).result == "pending"                # 옛 근거는 쓰지 않는다


# ---- NodeLink -------------------------------------------------------------

def make_link(clock, net):
    link = NodeLink("A", net.endpoint(NODE), HUB, clock, timeout_s=0.5)
    hub = net.endpoint(HUB)
    link.open(), hub.open()
    return link, hub


def test_node_link_request_since_is_first_seen():
    clock = FakeClock()
    net = LoopbackNetwork(clock)
    link, hub = make_link(clock, net)
    assert link.request(0.0) is None
    hub.send(encode(Control("C", "cb", 1, None)), NODE)
    link.poll(0)
    assert link.hub_alive(0.0) and link.request(0.0) is None           # 살아 있지만 요청 없음
    clock.advance(1.0)
    hub.send(encode(Control("C", "cb", 2, 7)), NODE)
    link.poll(0)
    clock.advance(0.2)
    hub.send(encode(Control("C", "cb", 3, 7)), NODE)                   # 같은 요청 반복
    link.poll(0)
    r = link.request(clock.now())
    assert r.key == ("cb", 7) and r.since == 1.0                       # 처음 받은 시각 유지
    clock.advance(0.6)
    assert link.request(clock.now()) is None                           # C 무응답


def test_node_link_rejects_garbage_and_wrong_type():
    clock = FakeClock()
    net = LoopbackNetwork(clock)
    link, hub = make_link(clock, net)
    hub.send(b"{nope", NODE)
    hub.send(encode(Report("B", "x", 1, State.FAULT, False, 0, "r", None, None, None)), NODE)
    hub.send(encode(Control("Z", "zb", 1, 9)), NODE)                   # C가 아닌 노드
    while link.poll(0):
        pass
    assert link.counts["bad_crc"] == 1 and link.counts["wrong_type"] == 1
    assert link.request(0.0) is None


def test_node_link_report_contents():
    clock = FakeClock(5.0)
    net = LoopbackNetwork(clock)
    link, hub = make_link(clock, net)
    d = Decision(True, State.CLEAR, "clear", 3, 4.88, 4.9)
    from psafe.safety.confirm import ConfirmStatus
    link.send_report(d, ConfirmStatus(7, "empty"), 5.0)
    link.send_report(unsafe(State.FAULT, "no_frame", 5.0), NO_CONFIRM, 5.0)
    r1, r2 = decode(hub.recv(0)[0]), decode(hub.recv(0)[0])
    assert (r1.node, r1.seq, r1.safe, r1.evidence_age_ms, r1.confirm_id, r1.confirm) == ("A", 1, True, 120, 7, "empty")
    assert (r2.seq, r2.evidence_age_ms, r2.confirm_id, r2.boot) == (2, None, None, r1.boot)


# ---- Runner 연결 ----------------------------------------------------------

class ScriptedHub(Transport):
    """A의 Transport 자리에 들어가는 가짜 C. 시각표에 따라 Control을 '받은 것처럼'
    돌려주고, A가 보낸 Report를 기록한다 (SyncRunner는 소스가 시간을 진행시키므로
    그 시각에 맞춰 응답할 수 있다)."""

    def __init__(self, clock, confirm_at: float, confirm_id: int = 7, stop_at: float = 1e9):
        self.clock, self.confirm_at, self.confirm_id, self.stop_at = clock, confirm_at, confirm_id, stop_at
        self._next = 0.0
        self._seq = 0
        self.reports: list[tuple[float, Report]] = []

    def send(self, data, to):
        self.reports.append((self.clock.now(), decode(data)))

    def recv(self, timeout):
        now = self.clock.now()
        if now < self._next or now >= self.stop_at:
            return None
        self._next = now + 0.2
        self._seq += 1
        cid = self.confirm_id if now >= self.confirm_at else None
        return encode(Control("C", "cb", self._seq, cid)), HUB


def node_cfg(seconds_objects, mode="sync", confirm_frames=5):
    sc = parse_scenario({"fps": 15, "timeline": seconds_objects})
    cfg = parse_config({
        "source": {"name": "dummy", "options": {"scenario": "unused", "width": 64, "height": 48}},
        "model": {"name": "dummy", "options": {}},
        "outputs": [],
        "fsm": {"clear_time_s": 0.5, "clear_min_frames": 5},
        "runner": {"mode": mode, "metrics_interval_s": 60},
        "link": {"node": "A", "hub": "c:5005", "confirm_frames": confirm_frames},
    })
    cfg.source.options["scenario"] = sc
    return cfg


def test_sync_node_confirms_empty_then_flips_on_person():
    cfg = node_cfg([
        {"from": 0, "to": 3, "objects": []},
        {"from": 3, "to": 4, "objects": ["person"]},
    ])
    clock = FakeClock()
    hub = ScriptedHub(clock, confirm_at=1.5)
    c = build(cfg, clock=clock, transport=hub)
    SyncRunner(c).run()

    reps = hub.reports
    assert all(r.node == "A" for _, r in reps)
    assert [r.seq for _, r in reps] == list(range(1, len(reps) + 1))
    before = [r for t, r in reps if t < 1.5]
    assert all(r.confirm_id is None for r in before)
    # 요청을 처음 받은 프레임은 근거가 아니다 (촬영 시각 == 받은 시각). 그 뒤 5장 (15 fps).
    pending_at = next(t for t, r in reps if r.confirm == "pending")
    empty_at = next(t for t, r in reps if r.confirm == "empty")
    assert pending_at >= 1.5
    assert empty_at == pytest.approx(pending_at + 5 / 15)
    assert next(r for t, r in reps if t >= 3.0).confirm == "occupied"   # 사람 등장 -> 뒤집힘
    last = reps[-1][1]
    assert (last.reason, last.safe, last.confirm) == ("shutdown", False, "fault")


def test_sync_node_without_link_is_unchanged():
    cfg = node_cfg([{"from": 0, "to": 1, "objects": []}])
    cfg.link = None
    c = build(cfg)
    r = SyncRunner(c).run()
    assert c.link is None and all(x == NO_CONFIRM for x in r.confirms)


def test_config_link_validation():
    base = node_cfg([{"from": 0, "to": 1, "objects": []}])
    assert base.link.node == "A" and base.link.bind == "0.0.0.0:5006"
    for bad in ({"node": "A", "hub": "nohost"}, {"node": "C", "hub": "c:1"},
                {"node": "A", "hub": "c:1", "confirm_frames": 0}, {"node": "A", "hub": "c:1", "oops": 1}):
        with pytest.raises(ConfigError):
            parse_config({"source": {"name": "dummy"}, "model": {"name": "dummy"}, "link": bad})


def test_threaded_node_with_loopback_hub():
    """실제 스레드: link-rx가 받은 요청이 worker를 거쳐 heartbeat Report로 나간다."""
    cfg = node_cfg([{"from": 0, "to": 3, "objects": []}], mode="threaded")
    clock = RealClock()
    net = LoopbackNetwork(clock)
    hub = net.endpoint(HUB)
    hub.open()
    c = build(cfg, clock=clock, transport=net.endpoint(NODE))
    got: list[Report] = []
    stop = threading.Event()

    def fake_c():
        seq = 0
        t0 = time.monotonic()
        while not stop.is_set():
            seq += 1
            cid = 7 if time.monotonic() - t0 > 1.0 else None
            hub.send(encode(Control("C", "cb", seq, cid)), NODE)
            while (m := hub.recv(0)) is not None:
                got.append(decode(m[0]))
            stop.wait(0.1)

    th = threading.Thread(target=fake_c, daemon=True)
    th.start()
    r = ThreadedRunner(c).run()
    stop.set()
    th.join(1)
    while (m := hub.recv(0)) is not None:
        got.append(decode(m[0]))
    hub.close()

    assert r.error is None
    results = [x.confirm for x in got]
    assert None in results and "empty" in results
    assert results.index("empty") > results.index(None)
    assert got[-1].reason == "shutdown" and got[-1].confirm == "fault"
