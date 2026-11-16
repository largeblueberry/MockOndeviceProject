import pytest

from psafe.core.config import FsmConfig
from psafe.core.packets import Detection, Detections, State
from psafe.fsm import SafetyFSM

FPS = 10
PERSON = Detection("person", 0.9, (0.4, 0.3, 0.6, 0.8))


class Driver:
    """지연 없이 1/FPS초마다 프레임 1장씩 FSM에 넣는다."""

    def __init__(self, **cfg):
        self.fsm = SafetyFSM(FsmConfig(**{"clear_time_s": 1.0, "clear_min_frames": 5,
                                          "fault_recover_frames": 3, **cfg}))
        self.seq = 0

    def frame(self, *objs, fault=None, age=0.0):
        t = self.seq / FPS
        d = self.fsm.step(Detections(self.seq, t, tuple(objs), "t", 0.0), t + age, fault)
        self.seq += 1
        return d

    def frames(self, n, *objs, **kw):
        return [self.frame(*objs, **kw) for _ in range(n)]


def test_never_y_on_first_frame():
    d = Driver().frame()
    assert (d.safe, d.state) == (False, State.VERIFYING)


def test_clear_after_time_and_frames():
    ds = Driver().frames(15)
    # t=0에 검증 시작. t=1.0 (10번째 프레임)에서 경과 >= 1초이고 >= 5프레임
    assert [d.safe for d in ds].index(True) == 10
    assert ds[-1].state == State.CLEAR


def test_min_frames_guard_at_low_fps():
    # 1초는 금방 지나지만 프레임 조건은 5장뿐... 20장으로 늘림
    drv = Driver(clear_min_frames=20)
    ds = drv.frames(25)
    assert [d.safe for d in ds].index(True) == 19


def test_person_is_immediate_n_even_from_clear():
    drv = Driver()
    drv.frames(15)
    d = drv.frame(PERSON)
    assert (d.safe, d.state, d.reason) == (False, State.OCCUPIED, "person_in_roi")


def test_person_resets_clear_timer():
    drv = Driver()
    drv.frames(8)          # 0.8초 비어 있음, 부족
    drv.frame(PERSON)      # t=0.8
    ds = drv.frames(11)    # 첫 빈 프레임(t=0.9)에서 타이머 재시작 -> t=1.9에 Y
    assert not any(d.safe for d in ds[:-1])
    assert ds[-1].safe


def test_low_confidence_person_ignored():
    low = Detection("person", 0.1, PERSON.bbox)
    ds = Driver().frames(15, low)
    assert ds[-1].safe


def test_person_outside_roi_ignored():
    outside = Detection("person", 0.9, (0.0, 0.0, 0.05, 0.05))
    ds = Driver(roi=[[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]).frames(15, outside)
    assert ds[-1].safe


def test_non_person_classes_ignored():
    car = Detection("car", 0.99, (0.2, 0.2, 0.8, 0.8))
    assert Driver().frames(15, car)[-1].safe


@pytest.mark.parametrize("kw,reason", [
    ({"fault": "too_dark"}, "too_dark"),
    ({"age": 0.6}, "stale_frame"),
])
def test_faults_force_n(kw, reason):
    drv = Driver()
    drv.frames(15)
    d = drv.frame(**kw)
    assert (d.safe, d.state, d.reason) == (False, State.FAULT, reason)


def test_recovery_goes_through_verifying():
    drv = Driver()
    drv.frames(15)
    drv.frame(fault="frozen_frame")
    ds = drv.frames(20)
    states = [d.state for d in ds]
    assert states[:2] == [State.FAULT, State.FAULT]          # 복구 중 (3프레임)
    assert states[2] == State.VERIFYING
    assert [d.safe for d in ds].index(True) == 2 + 10          # 이어서 비어 있음 기간 전체


def test_person_during_fault_goes_occupied():
    drv = Driver()
    drv.frame(fault="too_dark")
    assert drv.frame(PERSON).state == State.OCCUPIED


def test_out_of_order_frame_is_fault():
    drv = Driver()
    drv.frames(3)
    drv.seq = 1
    assert drv.frame().reason == "out_of_order"


def test_no_frame_and_external_fault():
    drv = Driver()
    drv.frames(15)
    assert drv.fsm.on_no_frame(5.0).reason == "no_frame"
    d = drv.fsm.fault("detector_error", 5.1)
    assert (d.safe, d.seq) == (False, -1)
