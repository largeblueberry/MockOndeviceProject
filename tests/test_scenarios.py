"""모든 scenarios/*.yaml은 자신의 `expect` 목록을 만족해야 한다.

실제 FSM이 들어오기 전(M3 이전)에는 임시 구현이 항상 N을 내므로, Y를 기대하는
시나리오는 xfail(strict)로 표시했다. M3에서 통과하게 되면 strict 표시 때문에
실패로 바뀌어 NEEDS_FSM을 지우라는 알림 역할을 한다.
"""

from pathlib import Path

import pytest

from psafe.core.packets import Decision, State
from psafe.runners import RunResult
from psafe.scenario_check import check, decision_at, run_scenario
from psafe.sources.scenario import load_scenario, parse_scenario

SCENARIOS = sorted(Path("scenarios").glob("*.yaml"))
NEEDS_FSM = False  # M3: 실제 FSM 적용 완료.


def _param(p: Path):
    needs_y = any(e.safe for e in load_scenario(p).expect)
    marks = [pytest.mark.xfail(strict=True, reason="needs real FSM (M3)")] if NEEDS_FSM and needs_y else []
    return pytest.param(p, id=p.stem, marks=marks)


@pytest.mark.parametrize("path", [_param(p) for p in SCENARIOS])
def test_scenario(path):
    report = run_scenario(path)
    assert report.passed, report.summary()


def test_scenarios_exist():
    assert len(SCENARIOS) >= 6


# ---- 검증기 자체 ------------------------------------------------------

def _d(t, safe):
    return Decision(safe, State.CLEAR if safe else State.OCCUPIED, "x", 0, t, t)


def test_decision_at_picks_last_before_t():
    ds = [_d(0.0, False), _d(1.0, True), _d(2.0, False)]
    assert decision_at(ds, 1.5).decided_at == 1.0
    assert decision_at(ds, 2.0).decided_at == 2.0
    assert decision_at(ds, -0.1) is None


def test_check_reports_mismatch():
    sc = parse_scenario({"timeline": [{"from": 0, "to": 3}], "expect": [{"at": 1.5, "safe": True}]})
    ok = RunResult(start=10.0, decisions=[_d(10.0, False), _d(11.0, True)])
    bad = RunResult(start=10.0, decisions=[_d(10.0, False)])
    assert check(sc, ok) == []
    assert "want Y, got N" in check(sc, bad)[0]


def test_outage_produces_timeout_decisions():
    report = run_scenario("scenarios/camera_outage.yaml")
    assert report.result.timeouts >= 3                      # 2초 공백 / 0.5초 시간 초과
    d = decision_at(report.result.decisions, report.result.start + 6.0)
    assert d.state == State.FAULT and d.reason == "no_frame"
