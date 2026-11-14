"""Run a scenario file and check its `expect` list (DESIGN.md §6.2).

    python -m psafe --scenario scenarios/flicker.yaml --check

The scenario's `config:` block is merged over the base config, so a scenario
can carry its own model noise / thresholds (e.g. flicker.yaml sets miss_rate).
"""

import copy
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from psafe.app import build
from psafe.core.clock import FakeClock
from psafe.core.config import AppConfig, parse_config
from psafe.core.packets import Decision
from psafe.runners import RunResult, SyncRunner
from psafe.sources.scenario import Scenario, load_scenario


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge. Lists and scalars in `over` replace those in `base`."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# Small frames keep scenario runs fast; health checks don't depend on resolution.
SCENARIO_DEFAULTS = {"source": {"options": {"width": 160, "height": 120}}}


def config_for_scenario(base_raw: dict[str, Any], scenario_path: str | Path, sc: Scenario) -> AppConfig:
    raw = deep_merge(base_raw, SCENARIO_DEFAULTS)
    raw = deep_merge(raw, sc.config)
    raw = deep_merge(raw, {
        "source": {"name": "dummy", "options": {"scenario": str(scenario_path), "loop": False}},
        "runner": {"mode": "sync", "clock": "fake"},
    })
    return parse_config(raw)


def decision_at(decisions: list[Decision], t: float) -> Decision | None:
    """The decision in force at clock time t: the last one made at or before t."""
    times = [d.decided_at for d in decisions]
    i = bisect_right(times, t + 1e-9)
    return decisions[i - 1] if i else None


def check(sc: Scenario, result: RunResult) -> list[str]:
    failures = []
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
        head = f"[{status}] {self.scenario.name}: {len(self.scenario.expect)} expectations, " \
               f"{self.result.frames} frames, {self.result.skipped} skipped, {self.result.timeouts} timeouts"
        return "\n".join([head, *("    " + f for f in self.failures)])


def run_scenario(scenario_path: str | Path, base_config: str | Path = "config/default.yaml",
                 extra_outputs: list | None = None) -> ScenarioReport:
    sc = load_scenario(scenario_path)
    with open(base_config, encoding="utf-8") as f:
        base_raw = yaml.safe_load(f) or {}
    cfg = config_for_scenario(base_raw, scenario_path, sc)
    c = build(cfg, clock=FakeClock())
    c.outputs.extend(extra_outputs or [])
    result = SyncRunner(c).run()
    return ScenarioReport(sc, result, check(sc, result))
