"""시나리오 파일을 실행하고 `expect` 목록을 검증한다 (DESIGN.md §6.2).

    python -m psafe --scenario scenarios/flicker.yaml --check

시나리오의 `config:` 블록은 기본 설정 위에 병합되므로, 시나리오마다 모델
노이즈나 임계값을 따로 가질 수 있다 (예: flicker.yaml은 miss_rate를 설정).
"""

import copy
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from psafe.app import build
from psafe.core.config import AppConfig, parse_config
from psafe.core.packets import Decision
from psafe.fsm.roi import Roi
from psafe.runners import RunResult, SyncRunner, ThreadedRunner
from psafe.sources.scenario import Scenario, load_scenario


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """dict 재귀 병합. `over`의 리스트와 단일 값은 `base`의 값을 대체한다."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# 작은 프레임을 써서 시나리오 실행을 빠르게 한다. health 검사는 해상도와 무관하다.
SCENARIO_DEFAULTS = {"source": {"options": {"width": 160, "height": 120}}}


def config_for_scenario(base_raw: dict[str, Any], scenario_path: str | Path, sc: Scenario,
                        mode: str = "sync") -> AppConfig:
    raw = deep_merge(base_raw, SCENARIO_DEFAULTS)
    raw = deep_merge(raw, sc.config)
    raw = deep_merge(raw, {
        "source": {"name": "dummy", "options": {"scenario": str(scenario_path), "loop": False}},
        "runner": {"mode": mode, "clock": "fake" if mode == "sync" else "real"},
    })
    return parse_config(raw)


def decision_at(decisions: list[Decision], t: float) -> Decision | None:
    """시각 t에 유효한 판단: t 이전(포함)에 내려진 마지막 판단."""
    times = [d.decided_at for d in decisions]
    i = bisect_right(times, t + 1e-9)
    return decisions[i - 1] if i else None


def unsafe_yes(sc: Scenario, result: RunResult, roi: Roi) -> list[str]:
    """샘플링한 `expect` 시점만이 아니라 모든 판단에 대한 안전 불변식:
    실제로 ROI 안에 사람이 있던 프레임을 근거로 Y가 나와서는 안 된다.
    카메라/모델이 본 것이 아니라 시나리오의 world(정답)를 쓰므로, 모델이 보지
    못한 blackout/freeze 경우도 잡아낸다."""
    bad = []
    for d in result.decisions:
        if not d.safe or d.seq < 0:
            continue
        t = d.seq / sc.fps
        if any(o.cls == "person" and roi.touches(o.bbox) for o in sc.objects_at(t)):
            bad.append(d)
    if not bad:
        return []
    first = bad[0]
    return [f"UNSAFE: {len(bad)} Y decision(s) while a person was in the ROI "
            f"(first: frame t={first.seq / sc.fps:.2f}s, {first.state.value}: {first.reason})"]


def check(sc: Scenario, result: RunResult, roi: Roi | None = None) -> list[str]:
    failures = unsafe_yes(sc, result, roi) if roi else []
    for e in sc.expect:
        d = decision_at(result.decisions, result.start + e.at)
        want = "Y" if e.safe else "N"
        if d is None:
            failures.append(f"t={e.at:5.2f}s want {want}, got no decision yet")
        elif d.safe != e.safe:
            failures.append(f"t={e.at:5.2f}s want {want}, got {d.yn} ({d.state.value}: {d.reason})")
    return failures


@dataclass
class ScenarioReport:
    scenario: Scenario
    result: RunResult
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        r = self.result
        head = f"[{status}] {self.scenario.name}: {len(self.scenario.expect)} expectations, " \
               f"{r.frames} frames, {r.skipped + r.dropped} dropped, {r.timeouts} timeouts"
        if r.heartbeats:
            head += f", {r.heartbeats} heartbeats ({r.stale_heartbeats} stale)"
        return "\n".join([head, *("    " + f for f in self.failures)])


def run_scenario(scenario_path: str | Path, base_config: str | Path = "config/default.yaml",
                 extra_outputs: list | None = None, mode: str = "sync") -> ScenarioReport:
    """mode="sync": FakeClock, 즉시 실행, 결정적.
    mode="threaded": 실제 시계와 실제 스레드 -> 시나리오의 실제 길이만큼 걸리며
    타이밍 흔들림도 포함된다. 그래도 같은 expect가 성립해야 한다."""
    sc = load_scenario(scenario_path)
    with open(base_config, encoding="utf-8") as f:
        base_raw = yaml.safe_load(f) or {}
    cfg = config_for_scenario(base_raw, scenario_path, sc, mode)
    c = build(cfg)
    c.outputs.extend(extra_outputs or [])
    if mode == "sync":
        result = SyncRunner(c).run()
    else:
        result = ThreadedRunner(c, record=True).run()
    failures = check(sc, result, c.fsm.roi)
    if result.error:
        failures.insert(0, f"run error: {result.error}")
    return ScenarioReport(sc, result, failures)
