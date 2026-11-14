"""Safety FSM (DESIGN.md §8). Pure logic: no I/O, time is passed in.

    INIT ──person──► OCCUPIED ◄──person── VERIFYING ──T s & N frames clear──► CLEAR
      └──no person──────────────────────────►┘ ▲                              │
                                               └────────── person ────────────┘ (→ OCCUPIED)
    any state ──fault──► FAULT ──K healthy frames──► VERIFYING   (person → OCCUPIED)

Rules (asymmetric on purpose):
  * One frame with a person in the ROI -> N immediately.
  * Y only after clear_time_s AND clear_min_frames of continuous "no person".
    Both: at low FPS, time alone could pass with only 2-3 frames looked at.
  * Any doubt about the input (stale frame, camera fault, no frames,
    detector error) -> FAULT (N). Leaving FAULT always goes through VERIFYING,
    never straight to CLEAR.
  * Clear time is measured on frame capture timestamps: it is "seconds the
    camera saw an empty bay", not "seconds the CPU spent".
"""

import logging

from psafe.core.config import FsmConfig
from psafe.core.packets import Decision, Detections, State, unsafe
from psafe.fsm.roi import Roi

log = logging.getLogger("psafe.fsm")

# Float timestamps: 2.8 - 1.8 == 0.9999999999999998. Without a tolerance the
# clear timer would pass or not depending on rounding. 1 us is far below any
# frame interval, so this never makes a decision meaningfully less safe.
TIME_EPS = 1e-6


class SafetyFSM:
    def __init__(self, cfg: FsmConfig):
        self.cfg = cfg
        self.roi = Roi(cfg.roi)
        self.max_age_s = cfg.max_frame_age_ms / 1000.0
        self.state = State.INIT
        self._last_seq = -1
        self._last_ts = float("-inf")
        self._clear_since = 0.0      # frame timestamp when the bay was first seen empty
        self._clear_frames = 0
        self._recover_frames = 0

    # ---- inputs ------------------------------------------------------------

    def step(self, dets: Detections, now: float, fault: str | None = None) -> Decision:
        """One processed frame. `fault` comes from the health monitor (None = healthy)."""
        fault = fault or self._input_fault(dets, now)
        self._last_seq, self._last_ts = max(self._last_seq, dets.seq), max(self._last_ts, dets.timestamp)
        if fault:
            return self.fault(fault, now, dets)

        if self._person_in_roi(dets):
            self._clear_frames = 0
            return self._go(State.OCCUPIED, False, "person_in_roi", now, dets)

        if self.state == State.FAULT:
            self._recover_frames += 1
            if self._recover_frames < self.cfg.fault_recover_frames:
                return self._go(State.FAULT, False, "recovering", now, dets)
            return self._start_verifying(now, dets)

        if self.state in (State.INIT, State.OCCUPIED):
            return self._start_verifying(now, dets)

        if self.state == State.VERIFYING:
            self._clear_frames += 1
            elapsed = dets.timestamp - self._clear_since
            if elapsed + TIME_EPS >= self.cfg.clear_time_s and self._clear_frames >= self.cfg.clear_min_frames:
                return self._go(State.CLEAR, True, "clear", now, dets)
            return self._go(State.VERIFYING, False, "verifying", now, dets)

        return self._go(State.CLEAR, True, "clear", now, dets)  # CLEAR stays CLEAR

    def fault(self, reason: str, now: float, dets: Detections | None = None) -> Decision:
        """Enter FAULT for any reason. Also used by runners (detector crash, etc.)."""
        self._recover_frames = 0
        self._clear_frames = 0
        return self._go(State.FAULT, False, reason, now, dets)

    def on_no_frame(self, now: float) -> Decision:
        """The worker waited max_frame_age_ms without receiving a frame."""
        return self.fault("no_frame", now)

    # ---- helpers -----------------------------------------------------------

    def _input_fault(self, dets: Detections, now: float) -> str | None:
        if dets.seq <= self._last_seq or dets.timestamp < self._last_ts:
            return "out_of_order"
        if now - dets.timestamp > self.max_age_s + TIME_EPS:
            return "stale_frame"
        return None

    def _person_in_roi(self, dets: Detections) -> bool:
        return any(
            o.cls == "person" and o.conf >= self.cfg.person_conf_low and self.roi.touches(o.bbox)
            for o in dets.objects
        )

    def _start_verifying(self, now: float, dets: Detections) -> Decision:
        self._clear_since = dets.timestamp
        self._clear_frames = 1
        return self._go(State.VERIFYING, False, "verifying", now, dets)

    def _go(self, state: State, safe: bool, reason: str, now: float, dets: Detections | None) -> Decision:
        if state != self.state:
            log.debug("%s -> %s (%s)", self.state.value, state.value, reason)
            self.state = state
        if dets is None:
            return unsafe(state, reason, now)
        return Decision(safe, state, reason, dets.seq, dets.timestamp, now)
