"""DummyDetector noise and DummySource camera behaviour."""

import numpy as np
import pytest

from psafe.core.clock import FakeClock
from psafe.models.dummy import DummyDetector
from psafe.sources.dummy import DummySource
from psafe.sources.scenario import parse_scenario

PERSON_10S = parse_scenario({"fps": 10, "timeline": [{"from": 0, "to": 10, "objects": ["person"]}]})


def frames(src, n):
    return [src.read() for _ in range(n)]


def persons(dets):
    return sum(o.cls == "person" for o in dets.objects)


# ---- detector -------------------------------------------------------------

def test_no_noise_reports_ground_truth():
    clock = FakeClock()
    src, det = DummySource(clock, PERSON_10S, 32, 24), DummyDetector(clock)
    assert all(persons(det.predict(f)) == 1 for f in frames(src, 20))


def test_miss_rate_drops_roughly_that_fraction():
    clock = FakeClock()
    src, det = DummySource(clock, PERSON_10S, 32, 24), DummyDetector(clock, miss_rate=0.3, seed=3)
    seen = [persons(det.predict(f)) for f in frames(src, 100)]
    assert 0.55 < sum(seen) / 100 < 0.85


def test_false_rate_adds_phantom_person():
    clock = FakeClock()
    empty = parse_scenario({"fps": 10, "timeline": [{"from": 0, "to": 5, "objects": []}]})
    src, det = DummySource(clock, empty, 32, 24), DummyDetector(clock, false_rate=1.0)
    assert all(persons(det.predict(f)) == 1 for f in frames(src, 10))


def test_same_seed_same_noise():
    def run():
        clock = FakeClock()
        src, det = DummySource(clock, PERSON_10S, 32, 24), DummyDetector(clock, miss_rate=0.5, seed=7)
        det.load()
        return [persons(det.predict(f)) for f in frames(src, 50)]
    assert run() == run()


def test_latency_advances_fake_clock_and_source_skips_frames():
    clock = FakeClock()
    src, det = DummySource(clock, PERSON_10S, 32, 24), DummyDetector(clock, latency_ms=250)
    seqs, ages = [], []
    for _ in range(5):
        f = src.read()
        det.predict(f)
        seqs.append(f.seq)
        ages.append(clock.now() - f.timestamp)
    # 10 fps, 250 ms per inference. At t=0.25 the newest captured frame is #2 (t=0.2), etc.
    assert seqs == [0, 2, 5, 7, 10]
    assert src.skipped == 6
    # Age at decision time = wait for capture + inference. Must reflect the real capture time.
    assert ages == pytest.approx([0.25, 0.30, 0.25, 0.30, 0.25])


def test_invalid_rates_rejected():
    with pytest.raises(ValueError):
        DummyDetector(FakeClock(), miss_rate=1.5)


# ---- source camera modes ---------------------------------------------------

def test_noise_makes_static_frames_differ():
    src = DummySource(FakeClock(), PERSON_10S, 32, 24)
    a, b = src.read(), src.read()
    assert not np.array_equal(a.image, b.image)


def test_outage_skips_time():
    sc = parse_scenario({"fps": 10, "timeline": [
        {"from": 0, "to": 1, "objects": []},
        {"from": 1, "to": 3, "objects": [], "camera": "outage"},
        {"from": 3, "to": 4, "objects": []},
    ]})
    src = DummySource(FakeClock(), sc, 32, 24)
    fs = [f for f in iter(src.read, None)]
    assert len(fs) == 20
    assert fs[10].seq == 30 and fs[10].timestamp == pytest.approx(3.0)
    assert src.outage_frames == 20


def test_blackout_hides_person_from_model():
    sc = parse_scenario({"fps": 10, "timeline": [{"from": 0, "to": 1, "objects": ["person"], "camera": "blackout"}]})
    f = DummySource(FakeClock(), sc, 32, 24).read()
    assert f.meta["gt"] == () and len(f.meta["world"]) == 1
    assert f.image.mean() < 5


def test_freeze_repeats_last_frame():
    sc = parse_scenario({"fps": 10, "timeline": [
        {"from": 0, "to": 1, "objects": []},
        {"from": 1, "to": 2, "objects": ["person"], "camera": "freeze"},
    ]})
    fs = frames(DummySource(FakeClock(), sc, 32, 24), 15)
    assert np.array_equal(fs[9].image, fs[14].image)
    assert fs[14].meta["gt"] == () and len(fs[14].meta["world"]) == 1
