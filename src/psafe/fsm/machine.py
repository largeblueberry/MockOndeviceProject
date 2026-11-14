"""Safety FSM (DESIGN.md §8). Pure logic: no I/O, time is passed in.

M1: placeholder that always answers N (fail-safe). Real states arrive in M3.
"""

from psafe.core.config import FsmConfig
from psafe.core.packets import Decision, Detections, State, unsafe


class SafetyFSM:
    def __init__(self, cfg: FsmConfig):
        self.cfg = cfg
        self.state = State.INIT

    def step(self, dets: Detections, now: float) -> Decision:
        return unsafe(self.state, "fsm_not_implemented", now, dets.seq, dets.timestamp)

    def on_no_frame(self, now: float) -> Decision:
        """Called when the worker timed out waiting for a frame."""
        self.state = State.FAULT
        return unsafe(self.state, "no_frame", now)
