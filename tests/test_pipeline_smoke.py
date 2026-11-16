"""M1 인수 조건: 가상 프레임이 source -> detector -> fsm -> output으로 흐른다."""

import pytest

from psafe.app import build
from psafe.core.clock import FakeClock
from psafe.core.config import load_config
from psafe.core.packets import Decision
from psafe.models import DETECTORS
from psafe.outputs.base import Output
from psafe.runners import SyncRunner
from psafe.sources.dummy import DummySource
from psafe.sources.scenario import parse_scenario


class Collect(Output):
    def __init__(self):
        self.got: list[Decision] = []

    def send(self, decision):
        self.got.append(decision)


def test_default_pipeline_runs_end_to_end():
    c = build(load_config("config/default.yaml"))
    sink = Collect()
    c.outputs.append(sink)
    result = SyncRunner(c).run()

    assert result.frames == 15 * 15              # 15 fps로 15초 시나리오
    assert len(sink.got) == result.frames + 1           # + 마지막 "shutdown" N
    assert (sink.got[-1].safe, sink.got[-1].reason) == (False, "shutdown")
    assert [d.seq for d in result.decisions] == list(range(result.frames))
    assert result.decisions[0].safe is False            # 첫 프레임에서는 절대 Y가 아님
    assert result.decisions[-1].safe is True            # 끝에는 구역이 비어 있음
    assert c.clock.now() == pytest.approx((result.frames - 1) / 15)  # 가짜 시간, 실제 대기 없음


def test_dummy_source_attaches_ground_truth_on_timeline():
    sc = parse_scenario({"fps": 10, "timeline": [
        {"from": 0, "to": 1, "objects": []},
        {"from": 1, "to": 2, "objects": ["person"]},
    ]})
    src = DummySource(FakeClock(), sc, width=64, height=48)
    frames = [src.read() for _ in range(20)]
    assert src.read() is None                    # 시나리오 종료
    assert [len(f.meta["gt"]) for f in frames] == [0] * 10 + [1] * 10
    assert frames[0].image.shape == (48, 64, 3)
    assert frames[15].timestamp == pytest.approx(1.5)


def test_unknown_model_name_lists_available():
    with pytest.raises(KeyError, match="dummy"):
        DETECTORS.create("yolo_does_not_exist")
