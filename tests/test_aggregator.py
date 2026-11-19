"""C의 판정: Aggregator (순수 로직), Verdict -> ESP32 매핑, VerdictOutput."""

import pytest

from psafe.core.clock import FakeClock
from psafe.core.packets import State
from psafe.core.verdict import VerdictKind as K
from psafe.core.verdict import to_protocol
from psafe.link.messages import Report
from psafe.link.peer import Received
from psafe.outputs.mock_mcu import MockMcuOutput
from psafe.outputs.verdict_api import ConsoleVerdictOutput, DecisionBridge, format_verdict, verdict_decision
from psafe.safety.aggregator import Aggregator

FIRST = 1000


def rep(node, state=State.CLEAR, count=0, age=100, cid=None, confirm=None, reason="r", seq=1):
    safe = state == State.CLEAR and count == 0 and age is not None
    return Report(node, "b" + node, seq, state, safe, count, reason, age, cid, confirm)


def at(r: Report, received_at: float, transit_s: float | None = 0.0) -> Received[Report]:
    return Received(r, received_at, transit_s)


def agg(**kw) -> Aggregator:
    return Aggregator(("A", "B"), first_confirm_id=FIRST, **kw)


def both(now, a: Report | None, b: Report | None, recv=None):
    t = now if recv is None else recv
    return {"A": at(a, t) if a else None, "B": at(b, t) if b else None}


def test_boot_is_unknown():
    g = agg()
    v = g.step({}, 0.0)
    assert (v.kind, v.reason) == (K.UNKNOWN, "A_timeout")
    assert [n.alive for n in v.nodes] == [False, False] and g.confirm_id is None


def test_full_confirm_flow():
    g = agg()
    v = g.step(both(0.0, rep("A"), rep("B")), 0.0)
    assert (v.kind, v.reason, v.confirm_id) == (K.CHECKING, "confirming", FIRST)
    # 아직 옛 번호(없음)에 대한 답
    v = g.step(both(0.2, rep("A"), rep("B")), 0.2)
    assert v.kind == K.CHECKING
    v = g.step(both(0.4, rep("A", cid=FIRST, confirm="pending"), rep("B", cid=FIRST, confirm="empty")), 0.4)
    assert v.kind == K.CHECKING and [n.confirm for n in v.nodes] == ["pending", "empty"]
    v = g.step(both(0.6, rep("A", cid=FIRST, confirm="empty"), rep("B", cid=FIRST, confirm="empty")), 0.6)
    assert (v.kind, v.reason, v.safe) == (K.EMPTY, "confirmed", True)
    # 유지
    v = g.step(both(0.8, rep("A", cid=FIRST, confirm="empty"), rep("B", cid=FIRST, confirm="empty")), 0.8)
    assert v.kind == K.EMPTY and v.confirm_id == FIRST


def test_empty_answer_for_old_id_does_not_count():
    g = agg()
    g.step(both(0.0, rep("A"), rep("B")), 0.0)
    v = g.step(both(0.2, rep("A", cid=FIRST - 1, confirm="empty"), rep("B", cid=FIRST - 1, confirm="empty")), 0.2)
    assert v.kind == K.CHECKING and [n.confirm for n in v.nodes] == [None, None]


def test_person_drops_confirmation_and_needs_new_id():
    g = agg()
    ok = lambda n: rep(n, cid=FIRST, confirm="empty")  # noqa: E731
    g.step(both(0.0, rep("A"), rep("B")), 0.0)
    assert g.step(both(0.2, ok("A"), ok("B")), 0.2).kind == K.EMPTY
    v = g.step(both(0.4, rep("A", State.OCCUPIED, count=2, age=50), ok("B")), 0.4)
    assert (v.kind, v.reason, v.confirm_id) == (K.OCCUPIED, "person_A", None)
    assert [n.count for n in v.nodes] == [2, 0]
    # 사람이 나가고 A가 다시 CLEAR가 되어도, 옛 번호의 'empty'로는 EMPTY가 아니다
    v = g.step(both(0.6, ok("A"), ok("B")), 0.6)
    assert (v.kind, v.confirm_id) == (K.CHECKING, FIRST + 1)
    v = g.step(both(0.8, rep("A", cid=FIRST + 1, confirm="empty"), rep("B", cid=FIRST + 1, confirm="empty")), 0.8)
    assert v.kind == K.EMPTY


def test_occupied_beats_unknown():
    g = agg()
    v = g.step(both(0.0, rep("A", State.OCCUPIED, count=1, age=50), None), 0.0)
    assert (v.kind, v.reason) == (K.OCCUPIED, "person_A")
    assert v.nodes[1].alive is False                                     # B 무응답도 함께 보인다


def test_confirm_occupied_answer_is_occupied():
    g = agg()
    g.step(both(0.0, rep("A"), rep("B")), 0.0)
    v = g.step(both(0.2, rep("A", cid=FIRST, confirm="occupied"), rep("B")), 0.2)
    assert v.kind == K.OCCUPIED


@pytest.mark.parametrize("a, reason", [
    (rep("A", State.FAULT, age=None, reason="frozen_frame"), "A_fault"),
    (rep("A", State.INIT, age=None), "A_init"),
    (rep("A", age=1001), "A_stale"),
])
def test_unknown_reasons(a, reason):
    v = agg().step(both(0.0, a, rep("B")), 0.0)
    assert (v.kind, v.reason) == (K.UNKNOWN, reason)


def test_evidence_age_uses_c_receive_time():
    g = agg()
    # A는 보낼 때 근거가 700 ms 전이라고 했고, C는 그 보고를 0.35초 전에 받았다 -> 1050 ms
    v = g.step({"A": at(rep("A", age=700), 0.0), "B": at(rep("B"), 0.35)}, 0.35)
    assert v.nodes[0].evidence_age_ms == pytest.approx(1050)
    assert (v.kind, v.reason) == (K.UNKNOWN, "A_stale")


def test_unknown_transit_blocks_empty():
    """왕복 시간을 모르면 근거 나이도 모른다 -> CLEAR여도 EMPTY 불가 (E-21)."""
    g = agg()
    v = g.step({"A": at(rep("A"), 0.0, transit_s=None), "B": at(rep("B"), 0.0)}, 0.0)
    assert (v.kind, v.reason) == (K.UNKNOWN, "A_no_rtt")
    assert v.nodes[0].evidence_age_ms is None and v.nodes[0].transit_ms is None


def test_transit_is_added_to_evidence_age():
    """A가 잰 근거 나이 100 ms + 전달 시간 상한 950 ms -> 1050 ms > 1000 -> 오래됨 (E-21)."""
    g = agg()
    v = g.step({"A": at(rep("A", age=100), 0.0, transit_s=0.95), "B": at(rep("B"), 0.0)}, 0.0)
    assert v.nodes[0].evidence_age_ms == pytest.approx(1050) and v.nodes[0].transit_ms == pytest.approx(950)
    assert (v.kind, v.reason) == (K.UNKNOWN, "A_stale")


def test_verifying_node_is_checking():
    v = agg().step(both(0.0, rep("A", State.VERIFYING, age=50), rep("B")), 0.0)
    assert (v.kind, v.reason, v.confirm_id) == (K.CHECKING, "A_verifying", None)


def test_confirm_timeout_and_retry():
    g = agg(confirm_timeout_s=1.0)
    g.step(both(0.0, rep("A"), rep("B")), 0.0)
    assert g.step(both(1.0, rep("A", cid=FIRST, confirm="pending"), rep("B")), 1.0).kind == K.CHECKING
    v = g.step(both(1.2, rep("A", cid=FIRST, confirm="pending"), rep("B")), 1.2)
    assert (v.kind, v.reason, v.confirm_id) == (K.UNKNOWN, "confirm_timeout", None)
    v = g.step(both(1.4, rep("A"), rep("B")), 1.4)
    assert (v.kind, v.confirm_id) == (K.CHECKING, FIRST + 1)              # 새 번호로 재시도


def test_confirm_fault_answer():
    g = agg()
    g.step(both(0.0, rep("A"), rep("B")), 0.0)
    v = g.step(both(0.2, rep("A", cid=FIRST, confirm="empty"), rep("B", cid=FIRST, confirm="fault")), 0.2)
    assert (v.kind, v.reason, v.confirm_id) == (K.UNKNOWN, "confirm_fault_B", None)


def test_node_timeout_drops_empty():
    g = agg()
    ok = lambda n: rep(n, cid=FIRST, confirm="empty")  # noqa: E731
    g.step(both(0.0, rep("A"), rep("B")), 0.0)
    assert g.step(both(0.2, ok("A"), ok("B")), 0.2).kind == K.EMPTY
    v = g.step({"A": at(ok("A"), 0.4), "B": None}, 0.4)
    assert (v.kind, v.reason, v.confirm_id) == (K.UNKNOWN, "B_timeout", None)


def test_first_confirm_id_is_random_per_boot():
    ids = {Aggregator(("A",)).step({"A": at(rep("A"), 0.0)}, 0.0).confirm_id for _ in range(20)}
    assert len(ids) > 15


def test_bad_node_list():
    for nodes in ((), ("A", "A")):
        with pytest.raises(ValueError):
            Aggregator(nodes)


# ---- 출력 -----------------------------------------------------------------

def test_protocol_mapping_y_only_when_empty():
    g = agg()
    v_unknown = g.step({}, 0.0)
    g.step(both(0.0, rep("A"), rep("B")), 0.0)
    v_empty = g.step(both(0.2, rep("A", cid=FIRST, confirm="empty"), rep("B", cid=FIRST, confirm="empty")), 0.2)
    assert to_protocol(v_unknown) == (State.FAULT, False)
    assert to_protocol(v_empty) == (State.CLEAR, True)
    d = verdict_decision(v_empty)
    assert (d.safe, d.state, d.reason, d.timestamp) == (True, State.CLEAR, "confirmed", 0.2)


def test_decision_bridge_drives_mock_esp32():
    clock = FakeClock()
    bridge = DecisionBridge(clock, "mock_mcu")
    assert isinstance(bridge.inner, MockMcuOutput)
    bridge.open()
    g = agg()
    bridge.show(g.step(both(0.0, rep("A"), rep("B")), 0.0))
    assert bridge.inner.rx.gate(0.0)[0] is False                   # 확인 중 -> N
    clock.advance(0.2)
    bridge.show(g.step(both(0.2, rep("A", cid=FIRST, confirm="empty"), rep("B", cid=FIRST, confirm="empty")), 0.2))
    assert bridge.inner.rx.gate(0.2)[0] is True                    # 비었음 확인 -> Y
    assert bridge.inner.rx.gate(0.8)[0] is False                   # C가 멈추면 워치독 -> N
    bridge.close()


def test_console_verdict_logs_on_change(caplog):
    caplog.set_level("INFO", logger="psafe.output.verdict")
    out = ConsoleVerdictOutput()
    g = agg()
    v = g.step(both(0.0, rep("A", State.OCCUPIED, count=2, age=50), None), 0.0)
    out.show(v)
    out.show(v)
    assert len(caplog.records) == 1
    line = format_verdict(v)
    assert "사람 있음" in line and "A: 2명" in line and "B: 무응답" in line
