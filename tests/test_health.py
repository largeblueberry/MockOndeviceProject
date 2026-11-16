import numpy as np
import pytest
import yaml

from psafe.core.config import HealthConfig
from psafe.core.packets import FramePacket
from psafe.fsm import HealthMonitor
from psafe.scenario_check import check, config_for_scenario
from psafe.app import build
from psafe.core.clock import FakeClock
from psafe.runners import SyncRunner
from psafe.sources.scenario import load_scenario

rng = np.random.default_rng(0)


def pkt(seq, img):
    return FramePacket(seq, seq / 10, "t", img)


def noisy(level, shape=(48, 64, 3)):
    return np.clip(level + rng.normal(0, 2, shape), 0, 255).astype(np.uint8)


def test_healthy_frames_pass():
    h = HealthMonitor(HealthConfig())
    assert all(h.check(pkt(i, noisy(90))) is None for i in range(10))


@pytest.mark.parametrize("level,reason", [(2, "too_dark"), (253, "too_bright")])
def test_brightness(level, reason):
    assert HealthMonitor(HealthConfig()).check(pkt(0, noisy(level))) == reason


def test_frozen_frame():
    h = HealthMonitor(HealthConfig(freeze_frames=2))
    img = noisy(90)
    assert h.check(pkt(0, img)) is None
    assert h.check(pkt(1, img)) is None          # 같은 프레임 1장: 아직 아님
    assert h.check(pkt(2, img)) == "frozen_frame"
    assert h.check(pkt(3, noisy(90))) is None    # 카메라가 다시 움직임


def test_without_health_checks_blackout_is_unsafe():
    """health 검사가 필요한 이유: 검사를 끄면 렌즈를 가린 시나리오에서
    사람이 구역 안에 서 있는데도 Y가 나온다."""
    path = "scenarios/camera_blackout.yaml"
    sc = load_scenario(path)
    with open("config/default.yaml", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw["health"].update(min_brightness=-1, freeze_diff_threshold=-1)  # 검사 끔
    c = build(config_for_scenario(raw, path, sc), clock=FakeClock())
    failures = check(sc, SyncRunner(c).run(), c.fsm.roi)
    assert any(f.startswith("UNSAFE") for f in failures)
