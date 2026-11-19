"""보드 간 통신: 메시지 형식, 전송 계층(Loopback·UDP), 노드별 수신 추적."""

import json

import pytest

from psafe.core.clock import FakeClock
from psafe.core.packets import State
from psafe.link.messages import (
    _FIELDS, MAX_MESSAGE_BYTES, PROTOCOL_VERSION, Control, Report, decode, encode, new_boot_id,
)
from psafe.link.peer import PeerTracker
from psafe.link.transport import LinkFault, LoopbackNetwork, UdpTransport


def report(**kw) -> Report:
    base = dict(node="A", boot="b1", seq=1, state=State.CLEAR, safe=True, count=0, reason="clear",
                evidence_age_ms=120, confirm_id=7, confirm="empty")
    base.update(kw)
    return Report(**base)


def control(**kw) -> Control:
    base = dict(node="C", boot="c1", seq=1, confirm_id=7)
    base.update(kw)
    return Control(**base)


def raw(d: dict) -> bytes:
    return json.dumps(d).encode()


# ---- 메시지 ---------------------------------------------------------------

def test_roundtrip_both_types():
    for m in (report(), report(state=State.OCCUPIED, safe=False, count=2, reason="person_in_roi",
                               evidence_age_ms=None, confirm_id=None, confirm=None),
              control(), control(confirm_id=None)):
        assert decode(encode(m)) == m


def test_field_sets_match_to_dict():
    assert frozenset(report().to_dict()) == _FIELDS["report"]
    assert frozenset(control().to_dict()) == _FIELDS["control"]


def test_report_size_is_well_under_limit():
    big = report(node="X" * 64, boot="b" * 64, reason="r" * 64, seq=2**53 - 1,
                 evidence_age_ms=2**53 - 1, confirm_id=2**53 - 1, confirm="occupied", safe=False,
                 state=State.FAULT)
    assert len(encode(big)) < MAX_MESSAGE_BYTES


@pytest.mark.parametrize("kw", [
    dict(safe=True, state=State.VERIFYING),          # CLEAR가 아닌데 Y
    dict(safe=True, evidence_age_ms=None),           # 근거 없는 Y
    dict(safe=True, count=1),                        # 사람이 있는데 Y
    dict(confirm_id=None, confirm="empty"),          # 요청 번호 없는 확인 결과
    dict(confirm="maybe"),
    dict(count=-1), dict(seq=-1), dict(seq=True),    # bool은 int로 받지 않는다
    dict(node=""), dict(node="노드"), dict(reason="a\nb"),
    dict(evidence_age_ms=1.5),
])
def test_invalid_reports_cannot_be_built(kw):
    with pytest.raises(ValueError):
        report(**kw)


def test_decode_rejections():
    good = report().to_dict()
    assert decode(b"\xff\xfe") == "bad_json"
    assert decode(b"{not json") == "bad_json"
    assert decode(b"[1,2]") == "bad_json"
    assert decode(b"x" * (MAX_MESSAGE_BYTES + 1)) == "too_large"
    assert decode(raw({**good, "v": PROTOCOL_VERSION + 1})) == "bad_version"
    assert decode(raw({**good, "v": True})) == "bad_version"
    assert decode(raw({**good, "type": "hello"})) == "bad_type"
    assert decode(raw({**good, "extra": 1})) == "bad_fields"
    missing = dict(good)
    del missing["count"]
    assert decode(raw(missing)) == "bad_fields"
    assert decode(raw({**good, "state": "SLEEPING"})) == "invalid"
    assert decode(raw({**good, "state": "VERIFYING"})) == "invalid"      # safe=true인데 CLEAR 아님
    assert decode(raw({**good, "safe": 1})) == "invalid"
    assert decode(raw({**good, "evidence_age_ms": "120"})) == "invalid"
    assert decode(b'{"v":1,"type":"control","node":"C","boot":"c","seq":NaN,"confirm_id":null}') == "bad_json"


def test_decode_never_raises_on_garbage():
    import random
    rng = random.Random(1)
    data = encode(report())
    for _ in range(2000):
        b = bytearray(data)
        for _ in range(rng.randint(1, 4)):
            b[rng.randrange(len(b))] = rng.randrange(256)
        r = decode(bytes(b))
        assert isinstance(r, (str, Report, Control))


def test_boot_ids_differ():
    assert len({new_boot_id() for _ in range(50)}) == 50


# ---- PeerTracker ----------------------------------------------------------

def test_tracker_seq_dup_and_timeout():
    t = PeerTracker(timeout_s=0.5)
    assert t.latest("A", 0.0) is None                       # 부팅 직후는 무응답
    assert t.feed(report(seq=5), 0.0) == "restart"           # 처음 보는 노드
    assert t.feed(report(seq=5), 0.1) == "dup"
    assert t.feed(report(seq=4), 0.1) == "dup"               # 지연 도착
    assert t.feed(report(seq=6, count=0), 0.2) == "ok"
    assert t.latest("A", 0.7).msg.seq == 6                   # 0.5 s 경과: 아직 살아 있음
    assert t.latest("A", 0.71) is None                       # 무응답
    assert t.silent_for("A", 0.71) == pytest.approx(0.51)


def test_tracker_restart_and_late_old_boot():
    t = PeerTracker()
    t.feed(report(boot="old", seq=100), 0.0)
    assert t.feed(report(boot="new", seq=0), 0.1) == "restart"   # 재시작으로 seq가 0
    assert t.feed(report(boot="new", seq=1), 0.2) == "ok"
    # 재시작 전 프로세스의 메시지가 늦게 도착: 받아들이면 옛 판단이 되살아난다
    assert t.feed(report(boot="old", seq=101, safe=False, state=State.FAULT, confirm=None), 0.3) == "old_boot"
    assert t.latest("A", 0.3).msg.boot == "new"


def test_tracker_expected_nodes():
    t = PeerTracker(expected={"A", "B"})
    assert t.feed(report(node="Z"), 0.0) == "unknown_node"
    assert t.latest("Z", 0.0) is None
    assert t.feed(report(node="B"), 0.0) == "restart"


# ---- Loopback -------------------------------------------------------------

def test_loopback_delivery_and_delay():
    clock = FakeClock()
    net = LoopbackNetwork(clock)
    a, c = net.endpoint(("a", 1)), net.endpoint(("c", 1))
    a.open(), c.open()
    net.set_fault(("a", 1), ("c", 1), LinkFault(delay_s=0.1))
    a.send(b"hi", ("c", 1))
    assert c.recv(0) is None
    clock.advance(0.1)
    assert c.recv(0) == (b"hi", ("a", 1))
    assert c.recv(0) is None


def test_loopback_cut_heal_and_no_endpoint():
    net = LoopbackNetwork(FakeClock())
    a, c = net.endpoint(("a", 1)), net.endpoint(("c", 1))
    a.open(), c.open()
    net.cut(("a", 1), ("c", 1))
    a.send(b"x", ("c", 1))
    assert c.recv(0) is None and net.counts["lost"] == 1
    net.heal(("a", 1), ("c", 1))
    a.send(b"y", ("c", 1))
    assert c.recv(0) == (b"y", ("a", 1))
    a.send(b"z", ("nobody", 9))                              # UDP처럼 조용히 사라진다
    assert net.counts["no_endpoint"] == 1


def test_loopback_faults_are_deterministic_with_seed():
    def run(seed: int) -> list:
        clock = FakeClock()
        net = LoopbackNetwork(clock, seed=seed)
        a, c = net.endpoint(("a", 1)), net.endpoint(("c", 1))
        a.open(), c.open()
        net.set_fault(("a", 1), ("c", 1), LinkFault(loss=0.3, duplicate=0.2, corrupt=0.2, jitter_s=0.05))
        got = []
        for i in range(100):
            a.send(encode(report(seq=i + 1)), ("c", 1))
            clock.advance(0.01)
            while (m := c.recv(0)) is not None:
                got.append(m[0])
        clock.advance(1.0)
        while (m := c.recv(0)) is not None:
            got.append(m[0])
        return got
    assert run(3) == run(3)
    got = run(3)
    assert 0 < len(got) < 130


def test_loopback_faults_end_to_end_through_tracker():
    """유실·중복·깨짐·순서 뒤바뀜이 섞여도 tracker는 seq가 증가하는 메시지만 받아들인다."""
    clock = FakeClock()
    net = LoopbackNetwork(clock, seed=7)
    a, c = net.endpoint(("a", 1)), net.endpoint(("c", 1))
    a.open(), c.open()
    net.set_fault(("a", 1), ("c", 1), LinkFault(loss=0.2, duplicate=0.3, corrupt=0.2, jitter_s=0.3))
    t: PeerTracker[Report] = PeerTracker(timeout_s=0.5)
    accepted, rejected = [], {}
    for i in range(300):
        a.send(encode(report(seq=i + 1)), ("c", 1))
        clock.advance(0.2)
        while (m := c.recv(0)) is not None:
            msg = decode(m[0])
            if isinstance(msg, str):
                rejected[msg] = rejected.get(msg, 0) + 1
                continue
            if t.feed(msg, clock.now()) in ("ok", "restart"):
                accepted.append(msg.seq)
    assert accepted == sorted(accepted) and len(set(accepted)) == len(accepted)
    assert rejected                                          # 깨진 메시지는 decode에서 걸러진다
    assert t.counts["dup"] > 0                               # 중복·뒤늦은 도착은 tracker에서 걸러진다


def test_loopback_real_wait():
    import threading
    from psafe.core.clock import RealClock
    net = LoopbackNetwork(RealClock())
    a, c = net.endpoint(("a", 1)), net.endpoint(("c", 1))
    a.open(), c.open()
    threading.Timer(0.05, lambda: a.send(b"late", ("c", 1))).start()
    assert c.recv(1.0) == (b"late", ("a", 1))
    assert c.recv(0.05) is None


# ---- UDP (localhost) ------------------------------------------------------

def test_udp_roundtrip_localhost():
    a, c = UdpTransport(("127.0.0.1", 0)), UdpTransport(("127.0.0.1", 0))
    a.open(), c.open()
    try:
        assert c.recv(0) is None                             # 기다리지 않음
        assert c.recv(0.05) is None                          # 시간 초과
        a.send(encode(report()), c.address)
        data, frm = c.recv(1.0)
        assert decode(data) == report() and frm == a.address
        # 아무도 없는 포트로 보내도 예외가 나지 않고, 이후 수신도 계속 된다 (Windows ICMP 문제)
        dead = UdpTransport(("127.0.0.1", 0))
        dead.open()
        gone = dead.address
        dead.close()
        c.send(b"x", gone)
        a.send(encode(control()), c.address)
        data, _ = c.recv(1.0)
        assert decode(data) == control()
    finally:
        a.close(), c.close()
