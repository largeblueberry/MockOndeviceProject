"""Every scenarios/*.yaml must meet its `expect` list.

Until the real FSM lands (M3) the placeholder always says N, so scenarios
expecting a Y are marked xfail(strict): once M3 makes them pass, the strict
marker turns them red as a reminder to delete NEEDS_FSM.
"""

from pathlib import Path

import pytest

from psafe.core.packets import Decision, State
from psafe.runners import RunResult
from psafe.scenario_check import check, decision_at, run_scenario
from psafe.sources.scenario import load_scenario, parse_scenario

SCENARIOS = sorted(Path("scenarios").glob("*.yaml"))
NEEDS_FSM = False  # M3: real FSM in place.


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


# ---- the checker itself ------------------------------------------------------

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
    assert report.result.timeouts >= 3                      # 2 s gap / 0.5 s timeout
    d = decision_at(report.result.decisions, report.result.start + 6.0)
    assert d.state == State.FAULT and d.reason == "no_frame"
