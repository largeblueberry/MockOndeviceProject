"""config.yaml -> typed dataclasses (DESIGN.md §9).

Pluggable parts (source / model / outputs) are `name + options`; the options
dict is passed straight to the chosen class's constructor, so adding a new
implementation never requires touching this file.
Core settings (fsm / health / runner) are typed and validated here.
Unknown keys are errors: a typo like `clear_tme_s` must not silently fall back
to a default in a safety system.
"""

from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_type_hints

import yaml


@dataclass
class ComponentConfig:
    name: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class FsmConfig:
    person_conf_low: float = 0.30
    clear_time_s: float = 3.0
    clear_min_frames: int = 10
    max_frame_age_ms: float = 500.0
    fault_recover_frames: int = 5
    roi: list[list[float]] = field(
        default_factory=lambda: [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    )


@dataclass
class HealthConfig:
    min_brightness: float = 10.0
    max_brightness: float = 245.0
    freeze_diff_threshold: float = 0.5   # mean abs pixel diff below this = "same frame"
    freeze_frames: int = 1               # consecutive same frames before FAULT (1 = strictest)
    max_decision_age_ms: float = 600.0


@dataclass
class RunnerConfig:
    mode: str = "sync"            # sync | threaded
    clock: str = "auto"           # auto | real | fake  (auto: fake for sync, real for threaded)
    heartbeat_ms: float = 200.0
    metrics_interval_s: float = 5.0


@dataclass
class AppConfig:
    source: ComponentConfig
    model: ComponentConfig
    outputs: list[ComponentConfig] = field(default_factory=lambda: [ComponentConfig("console")])
    fsm: FsmConfig = field(default_factory=FsmConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    runner: RunnerConfig = field(default_factory=RunnerConfig)


class ConfigError(ValueError):
    pass


def _build(cls: type, data: Any, path: str) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping, got {type(data).__name__}")
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)} (allowed: {sorted(known)})")
    kwargs = {}
    for name, value in data.items():
        hint = hints[name]
        if is_dataclass(hint):
            kwargs[name] = _build(hint, value, f"{path}.{name}")
        elif getattr(hint, "__origin__", None) is list and is_dataclass(hint.__args__[0]):
            if not isinstance(value, list):
                raise ConfigError(f"{path}.{name}: expected a list")
            kwargs[name] = [_build(hint.__args__[0], v, f"{path}.{name}[{i}]") for i, v in enumerate(value)]
        else:
            kwargs[name] = value
    try:
        return cls(**kwargs)
    except TypeError as e:  # missing required field
        raise ConfigError(f"{path}: {e}") from None


def parse_config(data: dict[str, Any]) -> AppConfig:
    cfg = _build(AppConfig, data, "config")
    if cfg.runner.mode not in ("sync", "threaded"):
        raise ConfigError(f"config.runner.mode: must be sync|threaded, got {cfg.runner.mode!r}")
    if cfg.runner.clock not in ("auto", "real", "fake"):
        raise ConfigError(f"config.runner.clock: must be auto|real|fake, got {cfg.runner.clock!r}")
    if len(cfg.fsm.roi) < 3:
        raise ConfigError("config.fsm.roi: polygon needs at least 3 points")
    return cfg


def load_config(path: str | Path) -> AppConfig:
    with open(path, encoding="utf-8") as f:
        return parse_config(yaml.safe_load(f) or {})
