"""ThreadedRunner: 실제 시계, 실제 스레드. 테스트마다 ~1.5-3초 걸린다."""

import threading
import time
from pathlib import Path

import pytest

from psafe.app import build
from psafe.core.config import parse_config
from psafe.core.packets import Decision
from psafe.detectors.detector_api import Detector
from psafe.detectors.dummy import DummyDetector
from psafe.outputs.output_api import Output
from psafe.runners import ThreadedRunner
from psafe.scenario_check import run_scenario
from psafe.sources.scenario import parse_scenario


class Recorder(Output):
    def __init__(self):
        self.sent: list[tuple[float, Decision]] = []

    def send(self, d):
        self.sent.append((time.monotonic(), d))


def make(seconds=2.0, objects=(), model_opts=None, **runner):
    sc = parse_scenario({"fps": 15, "timeline": [{"from": 0, "to": seconds, "objects": list(objects)}]})
    cfg = parse_config({
        "source": {"name": "dummy", "options": {"scenario": "unused", "width": 64, "height": 48}},
        "model": {"name": "dummy", "options": model_opts or {}},
        "outputs": [],
        "fsm": {"clear_time_s": 0.5, "clear_min_frames": 5},
        "runner": {"mode": "threaded", "metrics_interval_s": 60, **runner},
    })
    cfg.source.options["scenario"] = sc  # 객체를 직접 전달, 파일 불필요
    c = build(cfg)
    rec = Recorder()
    c.outputs.append(rec)
    return c, rec


def test_heartbeat_period_and_final_n():
    c, rec = make(seconds=2.0)
    r = ThreadedRunner(c).run()

    assert r.error is None
    assert 8 <= r.heartbeats <= 12                      # 2 s / 200 ms
    gaps = [b[0] - a[0] for a, b in zip(rec.sent, rec.sent[1:-1])]
    assert max(gaps) < 0.3                              # 일정한 주기
    assert any(d.safe for _, d in rec.sent)             # 빈 구역 -> 언젠가 Y
    assert (rec.sent[-1][1].safe, rec.sent[-1][1].reason) == (False, "shutdown")
    assert r.frames >= 25                               # ~15 fps 모델


def test_slow_model_drops_frames_but_keeps_deciding():
    c, rec = make(seconds=2.0, model_opts={"latency_ms": 150})
    r = ThreadedRunner(c).run()
    assert r.dropped > 10                               # 캡처가 앞서가고 슬롯이 덮어씀
    assert 8 <= r.frames <= 16                          # 초당 ~6.7회 추론
    assert r.stale_heartbeats == 0                      # 150 ms < max_decision_age


class HangingDetector(DummyDetector):
    """몇 프레임은 정상 동작하다가 predict() 안에서 멈춘다 (드라이버/모델 멈춤)."""

    def __init__(self, clock, hang_after=10):
        super().__init__(clock)
        self.hang_after, self.n = hang_after, 0
        self.release = threading.Event()

    def predict(self, pkt):
        self.n += 1
        if self.n > self.hang_after:
            self.release.wait(5)
        return super().predict(pkt)


def test_hung_worker_heartbeat_sends_stale_n():
    c, rec = make(seconds=3.0)
    c.detector = det = HangingDetector(c.clock)
    try:
        r = ThreadedRunner(c, max_seconds=2.0).run()
    finally:
        det.release.set()
    assert r.stale_heartbeats >= 3
    stale = [d for _, d in rec.sent if d.reason == "decision_stale"]
    assert stale and not any(d.safe for d in stale)


class CrashingDetector(Detector):
    name = "crash"

    def predict(self, pkt):
        raise RuntimeError("boom")


def test_detector_exception_is_fault_not_crash():
    c, rec = make(seconds=1.0)
    c.detector = CrashingDetector()
    r = ThreadedRunner(c).run()
    assert r.error is None
    assert {d.reason for _, d in rec.sent} >= {"detector_error"}
    assert not any(d.safe for _, d in rec.sent)


def test_worker_crash_stops_run_with_error():
    c, rec = make(seconds=5.0)

    def broken_step(*a, **kw):
        raise RuntimeError("fsm bug")
    c.fsm.step = broken_step
    t0 = time.monotonic()
    r = ThreadedRunner(c).run()
    assert "worker" in (r.error or "")
    assert time.monotonic() - t0 < 2.0                  # 일찍 멈춤, 5초를 다 돌지 않음
    assert rec.sent[-1][1].reason == "shutdown"


def test_fake_clock_rejected():
    from psafe.core.clock import FakeClock
    c, _ = make()
    c.clock = FakeClock()
    with pytest.raises(ValueError):
        ThreadedRunner(c)


# ---- 전체 시나리오 실시간 실행 (느림: pytest -m slow) ---------------------

@pytest.mark.slow
@pytest.mark.parametrize("path", sorted(Path("scenarios").glob("*.yaml")), ids=lambda p: p.stem)
def test_scenario_threaded(path):
    report = run_scenario(path, mode="threaded")
    assert report.passed, report.summary()
