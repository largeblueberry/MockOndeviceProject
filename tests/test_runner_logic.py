"""Runner의 순수 로직 (스레드 없이). 외부 리뷰(2026-11-17)에서 찾은 경우."""

import numpy as np

from psafe.app import build
from psafe.core.config import parse_config
from psafe.core.packets import Decision, FramePacket, State
from psafe.detectors.dummy import DummyDetector
from psafe.runners import expire_decision, process_frame
from psafe.sources.scenario import parse_scenario

MAX_AGE = 0.6


def y(timestamp, decided_at):
    return Decision(True, State.CLEAR, "clear", 1, timestamp, decided_at)


def test_y_expires_by_evidence_age_not_only_decision_age():
    # 0 ms에 찍은 프레임으로 490 ms에 Y를 만들고 worker가 멈췄다.
    # 수정 전: 판단 나이(1.08-0.49=0.59 s)만 봐서 1.08초에도 Y를 보냈다.
    d = y(timestamp=0.0, decided_at=0.49)
    stale = expire_decision(d, now=1.08, max_age_s=MAX_AGE)
    assert stale is not None and not stale.safe and stale.reason == "decision_stale"


def test_fresh_y_is_kept():
    assert expire_decision(y(timestamp=1.0, decided_at=1.15), now=1.4, max_age_s=MAX_AGE) is None


def test_old_n_is_not_counted_stale():
    # N은 근거가 오래돼도 N이다. 판단 시각만 본다 (unsafe()의 timestamp는 -1).
    n = Decision(False, State.FAULT, "no_frame", -1, -1.0, 5.0)
    assert expire_decision(n, now=5.1, max_age_s=MAX_AGE) is None


class CountingDetector(DummyDetector):
    def __init__(self, clock):
        super().__init__(clock)
        self.calls = 0

    def predict(self, pkt):
        self.calls += 1
        return super().predict(pkt)


def make_components():
    sc = parse_scenario({"fps": 10, "timeline": [{"from": 0, "to": 1, "objects": []}]})
    cfg = parse_config({
        "source": {"name": "dummy", "options": {"scenario": "unused", "width": 64, "height": 48}},
        "model": {"name": "dummy"},
        "outputs": [],
    })
    cfg.source.options["scenario"] = sc
    c = build(cfg)
    c.detector = CountingDetector(c.clock)
    return c


def test_health_fault_skips_inference_and_is_n():
    # 수정 전: 카메라 이상을 알아도 추론을 먼저 끝내서 N이 100~250 ms 늦었다.
    c = make_components()
    dark = FramePacket(0, c.clock.now(), "t", np.zeros((48, 64, 3), np.uint8))
    d, dets = process_frame(c, dark)
    assert (d.safe, d.state, d.reason) == (False, State.FAULT, "too_dark")
    assert dets is None and c.detector.calls == 0


def test_healthy_frame_still_runs_inference():
    c = make_components()
    img = np.random.default_rng(0).integers(0, 255, (48, 64, 3), dtype=np.uint8)
    d, dets = process_frame(c, FramePacket(0, c.clock.now(), "t", img, {"visible": []}))
    assert dets is not None and c.detector.calls == 1
