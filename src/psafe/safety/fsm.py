"""안전 FSM (DESIGN.md §8). 순수 로직: 입출력 없음, 시간은 인자로 받는다.

    INIT ──person──► OCCUPIED ◄──person── VERIFYING ──T s & N frames clear──► CLEAR
      └──no person──────────────────────────►┘ ▲                              │
                                               └────────── person ────────────┘ (→ OCCUPIED)
    any state ──fault──► FAULT ──K healthy frames──► VERIFYING   (person → OCCUPIED)

규칙 (의도적으로 비대칭):
  * ROI 안에 사람이 있는 프레임이 1장이라도 나오면 즉시 N.
  * Y는 "사람 없음"이 clear_time_s 동안, 그리고 clear_min_frames장 연속될 때만.
    둘 다 보는 이유: FPS가 낮으면 시간 조건만으로는 2~3장만 보고 통과할 수 있다.
  * 입력이 조금이라도 의심스러우면 (오래된 프레임, 카메라 이상, 프레임 없음,
    검출기 오류) FAULT (N). FAULT를 벗어날 때는 항상 VERIFYING을 거치며,
    CLEAR로 바로 가지 않는다.
  * 비어 있는 시간은 프레임 촬영 시각으로 잰다. "CPU가 쓴 시간"이 아니라
    "카메라가 빈 구역을 본 시간"이다.
"""

import logging

from psafe.core.config import FsmConfig
from psafe.core.packets import Decision, FrameDetections, State, unsafe
from psafe.safety.roi import Roi

log = logging.getLogger("psafe.safety")

# 실수 시각: 2.8 - 1.8 == 0.9999999999999998. 허용 오차가 없으면 반올림에 따라
# 비어 있음 타이머의 통과 여부가 달라진다. 1 us는 어떤 프레임 간격보다도 훨씬
# 작으므로, 이 오차 때문에 판단이 의미 있게 덜 안전해지지는 않는다.
TIME_EPS = 1e-6


class SafetyFSM:
    def __init__(self, cfg: FsmConfig):
        self.cfg = cfg
        self.roi = Roi(cfg.roi)
        self.max_age_s = cfg.max_frame_age_ms / 1000.0
        self.state = State.INIT
        self._last_seq = -1
        self._last_ts = float("-inf")
        self._clear_since = 0.0      # 구역이 처음 비어 보인 프레임의 시각
        self._clear_frames = 0
        self._recover_frames = 0

    # ---- 입력 ------------------------------------------------------------

    def step(self, dets: FrameDetections, now: float, fault: str | None = None) -> Decision:
        """처리된 프레임 1장. `fault`는 HealthMonitor 결과 (None = 정상)."""
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

        return self._go(State.CLEAR, True, "clear", now, dets)  # CLEAR는 CLEAR 유지

    def fault(self, reason: str, now: float, dets: FrameDetections | None = None) -> Decision:
        """어떤 사유로든 FAULT로 들어간다. Runner도 사용한다 (검출기 오류 등)."""
        self._recover_frames = 0
        self._clear_frames = 0
        return self._go(State.FAULT, False, reason, now, dets)

    def on_no_frame(self, now: float) -> Decision:
        """worker가 max_frame_age_ms 동안 프레임을 받지 못했다."""
        return self.fault("no_frame", now)

    # ---- 내부 함수 -----------------------------------------------------------

    def _input_fault(self, dets: FrameDetections, now: float) -> str | None:
        if dets.seq <= self._last_seq or dets.timestamp < self._last_ts:
            return "out_of_order"
        if now - dets.timestamp > self.max_age_s + TIME_EPS:
            return "stale_frame"
        return None

    def _person_in_roi(self, dets: FrameDetections) -> bool:
        return any(
            o.cls == "person" and o.conf >= self.cfg.person_conf_low and self.roi.touches(o.bbox)
            for o in dets.objects
        )

    def _start_verifying(self, now: float, dets: FrameDetections) -> Decision:
        self._clear_since = dets.timestamp
        self._clear_frames = 1
        return self._go(State.VERIFYING, False, "verifying", now, dets)

    def _go(self, state: State, safe: bool, reason: str, now: float, dets: FrameDetections | None) -> Decision:
        if state != self.state:
            log.debug("%s -> %s (%s)", self.state.value, state.value, reason)
            self.state = state
        if dets is None:
            return unsafe(state, reason, now)
        return Decision(safe, state, reason, dets.seq, dets.timestamp, now)
