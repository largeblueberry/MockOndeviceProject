"""C 허브: 설정, Hub.poll/tick, HubRunner + 실제 UDP로 A·B 노드와 연결."""

import threading

import pytest

from psafe.app import build
from psafe.core.clock import FakeClock, RealClock
from psafe.core.config import ConfigError, is_hub_config, load_hub_config, parse_config, parse_hub_config
from psafe.core.packets import State
from psafe.core.verdict import VerdictKind as K
from psafe.hub import Hub, HubRunner
from psafe.link.messages import Control, Report, decode, encode
from psafe.link.transport import LoopbackNetwork, UdpTransport
from psafe.outputs.verdict_api import VerdictOutput
from psafe.runners import ThreadedRunner
from psafe.sources.scenario import parse_scenario


class Rec(VerdictOutput):
    def __init__(self):
        self.got = []

    def show(self, v):
        self.got.append(v)


def hub_cfg(**hub):
    return parse_hub_config({
        "hub": {"bind": "c:5005", "nodes": [{"name": "A", "address": "a:5006"}, {"name": "B", "address": "b:5007"}], **hub},
        "outputs": [],
    })


def test_repo_hub_config_loads():
    cfg = load_hub_config("config/hub_c.yaml")
    assert [n.name for n in cfg.hub.nodes] == ["A", "B"]
    assert [o.name for o in cfg.outputs] == ["console", "decision"]


@pytest.mark.parametrize("hub", [
    {"nodes": []},
    {"nodes": [{"name": "A", "address": "a:1"}, {"name": "A", "address": "a:2"}]},
    {"node": "A"},
    {"bind": "nope"},
    {"heartbeat_ms": 0},
    {"typo": 1},
])
def test_hub_config_validation(hub):
    base = {"bind": "c:5005", "nodes": [{"name": "A", "address": "a:5006"}]}
    with pytest.raises(ConfigError):
        parse_hub_config({"hub": {**base, **hub}})


def test_is_hub_config():
    assert is_hub_config({"hub": {}}) and not is_hub_config({"source": {}})


def test_hub_tick_flow_with_loopback():
    clock = FakeClock()
    net = LoopbackNetwork(clock)
    a, b = net.endpoint(("a", 5006)), net.endpoint(("b", 5007))
    a.open(), b.open()
    rec = Rec()
    hub = Hub(hub_cfg(), clock, transport=net.endpoint(("c", 5005)), outputs=[rec], first_confirm_id=50)
    hub.transport.open()

    def report(ep, node, seq, cid=None, confirm=None):
        ep.send(encode(Report(node, "b" + node, seq, State.CLEAR, True, 0, "clear", 100, cid, confirm)), ("c", 5005))

    assert hub.tick().kind == K.UNKNOWN                                     # 아무도 없음
    report(a, "A", 1), report(b, "B", 1)
    while hub.poll(0):
        pass
    v = hub.tick()
    assert (v.kind, v.confirm_id) == (K.CHECKING, 50)
    ctl = decode(a.recv(0)[0])                                              # 노드마다 Control이 간다
    ctl = decode(a.recv(0)[0])
    assert isinstance(ctl, Control) and ctl.confirm_id == 50 and decode(b.recv(0)[0]).node == "C"
    clock.advance(0.2)
    report(a, "A", 2, 50, "empty"), report(b, "B", 2, 50, "empty")
    a.send(b"garbage", ("c", 5005))
    while hub.poll(0):
        pass
    assert hub.tick().kind == K.EMPTY
    assert hub.counts["bad_json"] == 1
    assert [x.kind for x in rec.got] == [K.UNKNOWN, K.CHECKING, K.EMPTY]


def node_cfg(name, hub_addr, bind, objects_timeline):
    cfg = parse_config({
        "source": {"name": "dummy", "options": {"scenario": "unused", "width": 64, "height": 48}},
        "model": {"name": "dummy", "options": {}},
        "outputs": [],
        "fsm": {"clear_time_s": 0.5, "clear_min_frames": 5},
        "runner": {"mode": "threaded", "metrics_interval_s": 60},
        "link": {"node": name, "hub": hub_addr, "bind": bind, "confirm_frames": 5},
    })
    cfg.source.options["scenario"] = parse_scenario({"fps": 15, "timeline": objects_timeline})
    return cfg


def free_port() -> int:
    t = UdpTransport(("127.0.0.1", 0))
    t.open()
    port = t.address[1]
    t.close()
    return port


def test_three_processes_worth_of_components_over_real_udp():
    """A·B·C를 한 프로세스 안의 스레드로, 실제 localhost UDP로 연결한다.
    A에 사람이 등장했다 사라지고, B는 A보다 먼저 끝난다(= 꺼짐)."""
    pc, pa, pb = free_port(), free_port(), free_port()
    hub_c = parse_hub_config({"hub": {
        "bind": f"127.0.0.1:{pc}",
        "nodes": [{"name": "A", "address": f"127.0.0.1:{pa}"}, {"name": "B", "address": f"127.0.0.1:{pb}"}],
        "metrics_interval_s": 60,
    }, "outputs": []})
    rec = Rec()
    hub = Hub(hub_c, RealClock(), outputs=[rec])
    hr = HubRunner(hub, record=True)
    a = build(node_cfg("A", f"127.0.0.1:{pc}", f"127.0.0.1:{pa}",
                       [{"from": 0, "to": 2.5, "objects": []}, {"from": 2.5, "to": 3.5, "objects": ["person"]},
                        {"from": 3.5, "to": 7.5, "objects": []}]))
    # 사람 퇴장(3.5 s) -> A의 FSM 0.5 s -> 확인 ~0.6 s => ~4.8 s에 다시 EMPTY. B는 그 뒤 6.0 s에 꺼진다.
    # (5.0 s로 두면 재확인 도중 B가 꺼지는 경쟁이 생겨 간헐적으로 실패했다)
    b = build(node_cfg("B", f"127.0.0.1:{pc}", f"127.0.0.1:{pb}", [{"from": 0, "to": 6.0, "objects": []}]))

    th = threading.Thread(target=hr.run, daemon=True)
    th.start()
    ra, rb = {}, {}
    ta = threading.Thread(target=lambda: ra.update(r=ThreadedRunner(a).run()), daemon=True)
    tb = threading.Thread(target=lambda: rb.update(r=ThreadedRunner(b).run()), daemon=True)
    ta.start(), tb.start()
    ta.join(15), tb.join(15)
    hr.stop()
    th.join(5)

    assert ra["r"].error is None and rb["r"].error is None and hr.result.error is None
    kinds = [v.kind for v in hr.result.verdicts]
    reasons = [v.reason for v in hr.result.verdicts]
    first_empty = kinds.index(K.EMPTY)
    occupied = kinds.index(K.OCCUPIED, first_empty)
    assert kinds.index(K.EMPTY, occupied) > occupied                         # 사람이 나가면 재확인 후 다시 EMPTY
    assert "B_timeout" in reasons[occupied:]                                 # B가 꺼지면 판단 불가
    assert kinds[-1] == K.UNKNOWN
    assert rec.got[-1].reason == "shutdown"


def test_restart_is_not_counted_as_rejected():
    clock = FakeClock()
    net = LoopbackNetwork(clock)
    a = net.endpoint(("a", 5006))
    a.open()
    hub = Hub(hub_cfg(), clock, transport=net.endpoint(("c", 5005)), outputs=[])
    hub.transport.open()
    r = Report("A", "x", 1, State.FAULT, False, 0, "shutdown", None, None, None)
    a.send(encode(r), ("c", 5005))
    a.send(encode(r), ("c", 5005))
    while hub.poll(0):
        pass
    assert hub.tracker.counts["restart"] == 1 and hub.counts == {"wrong_type": 0, "dup": 1}
    from psafe.outputs.verdict_api import format_verdict
    assert "A: 0명 FAULT (shutdown)" in format_verdict(hub.tick())
