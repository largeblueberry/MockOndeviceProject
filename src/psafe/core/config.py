"""config.yaml -> 타입이 있는 dataclass (DESIGN.md §9).

교체 가능한 부품(source / model / outputs)은 `name + options` 형태다. options
dict는 선택된 클래스의 생성자로 그대로 전달되므로, 새 구현체를 추가해도 이
파일은 고칠 필요가 없다.
핵심 설정(fsm / health / runner)은 여기서 타입을 정하고 검증한다.
알 수 없는 키는 오류다. 안전 시스템에서 `clear_tme_s` 같은 오타가 조용히
기본값으로 대체되어서는 안 된다.
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
    # 프레임 정보의 유효 기간. 두 곳에서 같은 의미로 쓴다:
    #   FSM    - 도착한 프레임이 이보다 오래되었으면 FAULT (stale_frame)
    #   Runner - 이 시간 동안 새 프레임이 없으면 마지막 정보가 만료된 것이므로 FAULT (no_frame)
    max_frame_age_ms: float = 500.0
    fault_recover_frames: int = 5
    roi: list[list[float]] = field(
        default_factory=lambda: [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    )


@dataclass
class HealthConfig:
    min_brightness: float = 10.0
    max_brightness: float = 245.0
    freeze_diff_threshold: float = 0.5   # 픽셀 평균 절대 차이가 이 값 미만이면 "같은 프레임"
    freeze_frames: int = 1               # 같은 프레임이 몇 장 연속이면 FAULT (1 = 가장 엄격)


@dataclass
class RunnerConfig:
    mode: str = "sync"            # sync | threaded
    clock: str = "auto"           # auto | real | fake  (auto: sync면 fake, threaded면 real)
    heartbeat_ms: float = 200.0
    max_decision_age_ms: float = 600.0   # heartbeat가 보낼 판단이 이보다 오래되면 N (worker 멈춤 감지)
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
    except TypeError as e:  # 필수 필드 누락
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
