import pytest

from psafe.core.config import FsmConfig
from psafe.core.packets import Detection, Detections, State
from psafe.fsm import SafetyFSM

FPS = 10
PERSON = Detection("person", 0.9, (0.4, 0.3, 0.6, 0.8))


class Driver:
    """Feeds the FSM one frame per 1/FPS s with zero latency."""

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
    # t=0 starts verifying; t=1.0 (frame 10) has elapsed >= 1 s and >= 5 frames
    assert [d.safe for d in ds].index(True) == 10
    assert ds[-1].state == State.CLEAR


def test_min_frames_guard_at_low_fps():
    # 1 s elapses quickly but only 5 frames required... make it 20
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
    drv.frames(8)          # 0.8 s clear, not enough
    drv.frame(PERSON)      # t=0.8
    ds = drv.frames(11)    # timer restarts at the first empty frame (t=0.9) -> Y at t=1.9
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
    assert states[:2] == [State.FAULT, State.FAULT]          # recovering (3 frames)
    assert states[2] == State.VERIFYING
    assert [d.safe for d in ds].index(True) == 2 + 10          # then a full clear period


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
