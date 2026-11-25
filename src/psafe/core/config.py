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
from types import UnionType
from typing import Any, get_args, get_type_hints

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
    # worker가 이 시간 동안 판단을 하나도 새로 내지 못하면(예: Coral invoke() hang) 실행을 오류로 끝낸다
    # -> 종료 코드 1 -> systemd가 재시작. 그동안 N은 max_decision_age_ms가 이미 보장한다 (복구용, 안전용 아님).
    # 카메라 고장·암전 중에는 worker가 매번 새 FAULT 판단을 내므로 발동하지 않는다. None = 끔.
    # (docs/edge_tpu/DISCUSSION.md R2-4, 합의 11)
    fatal_stale_s: float | None = None


@dataclass
class LinkConfig:
    """A·B 노드가 C와 통신할 때 (DESIGN.md §7.4). 없으면 단독 실행 (지금까지와 같음)."""
    node: str                          # 이 노드 이름: "A" / "B"
    hub: str                           # C의 수신 주소 "호스트:포트" (예: "192.168.20.30:5005")
    bind: str = "0.0.0.0:5006"         # 이 노드가 Control을 받는 주소
    hub_node: str = "C"
    node_timeout_ms: float = 500.0     # C가 이 시간 동안 조용하면 확인 요청을 버린다
    confirm_frames: int = 10           # CONFIRM에서 '비었음'에 필요한 요청 이후 깨끗한 프레임 수 (K, 미확정)


@dataclass
class AppConfig:
    source: ComponentConfig
    model: ComponentConfig
    outputs: list[ComponentConfig] = field(default_factory=lambda: [ComponentConfig("console")])
    fsm: FsmConfig = field(default_factory=FsmConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    link: LinkConfig | None = None


@dataclass
class HubNodeConfig:
    name: str                          # "A" / "B"
    address: str                       # 그 노드가 Control을 받는 주소 "호스트:포트"


@dataclass
class HubConfig:
    """C (허브) 설정 (DESIGN.md §7.4). 카메라·모델이 없다."""
    nodes: list[HubNodeConfig]
    bind: str = "0.0.0.0:5005"         # C가 Report를 받는 주소
    node: str = "C"                    # C 자신의 이름 (Control의 node 필드)
    heartbeat_ms: float = 200.0
    node_timeout_ms: float = 500.0
    max_evidence_age_ms: float = 1000.0
    confirm_timeout_s: float = 3.0
    metrics_interval_s: float = 5.0


@dataclass
class HubAppConfig:
    hub: HubConfig
    outputs: list[ComponentConfig] = field(default_factory=lambda: [ComponentConfig("console")])


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
        if isinstance(hint, UnionType):           # 예: LinkConfig | None
            if value is None:
                kwargs[name] = None
                continue
            hint = next((a for a in get_args(hint) if is_dataclass(a)), hint)
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
    fs = cfg.runner.fatal_stale_s
    if fs is not None and (not isinstance(fs, (int, float)) or fs * 1000 <= cfg.runner.max_decision_age_ms):
        raise ConfigError("config.runner.fatal_stale_s: must be a number larger than max_decision_age_ms "
                          f"({cfg.runner.max_decision_age_ms} ms), got {fs!r}")
    if cfg.link is not None:
        _check_link(cfg.link)
    return cfg


def parse_address(text: str) -> tuple[str, int]:
    """"호스트:포트" -> (호스트, 포트)."""
    host, sep, port = str(text).rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 <= int(port) <= 65535:
        raise ConfigError(f"address must be 'host:port', got {text!r}")
    return host, int(port)


def _check_link(link: LinkConfig) -> None:
    for name in ("node", "hub_node"):
        v = getattr(link, name)
        if not isinstance(v, str) or not 1 <= len(v) <= 64 or not v.isascii() or not v.isprintable():
            raise ConfigError(f"config.link.{name}: must be 1..64 printable ASCII chars, got {v!r}")
    if link.node == link.hub_node:
        raise ConfigError("config.link.node: must differ from hub_node")
    parse_address(link.hub)
    parse_address(link.bind)
    if link.node_timeout_ms <= 0:
        raise ConfigError("config.link.node_timeout_ms: must be > 0")
    if not isinstance(link.confirm_frames, int) or link.confirm_frames < 1:
        raise ConfigError("config.link.confirm_frames: must be an int >= 1")


def parse_hub_config(data: dict[str, Any]) -> HubAppConfig:
    cfg = _build(HubAppConfig, data, "config")
    h = cfg.hub
    names = [n.name for n in h.nodes]
    if not names or len(set(names)) != len(names):
        raise ConfigError(f"config.hub.nodes: need at least one node with unique names, got {names}")
    for n in [*names, h.node]:
        if not isinstance(n, str) or not 1 <= len(n) <= 64 or not n.isascii() or not n.isprintable():
            raise ConfigError(f"config.hub: node name must be 1..64 printable ASCII chars, got {n!r}")
    if h.node in names:
        raise ConfigError("config.hub.node: must differ from the node names")
    parse_address(h.bind)
    for n in h.nodes:
        parse_address(n.address)
    for k in ("heartbeat_ms", "node_timeout_ms", "max_evidence_age_ms", "confirm_timeout_s"):
        if getattr(h, k) <= 0:
            raise ConfigError(f"config.hub.{k}: must be > 0")
    return cfg


def read_yaml(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def is_hub_config(data: dict[str, Any]) -> bool:
    """설정 파일에 `hub:` 절이 있으면 C(허브)로 실행한다."""
    return isinstance(data, dict) and "hub" in data


def load_config(path: str | Path) -> AppConfig:
    return parse_config(read_yaml(path))


def load_hub_config(path: str | Path) -> HubAppConfig:
    return parse_hub_config(read_yaml(path))
