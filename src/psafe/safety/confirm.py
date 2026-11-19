"""CONFIRM 처리, A·B 쪽 (DESIGN.md §7.4.5). 순수 로직: 입출력 없음.

C가 confirm_id를 보내면 "그 요청을 받은 뒤에 찍은 프레임만으로" 구역이 비었는지
다시 확인한다. 요청 전 판단(FSM이 이미 CLEAR였다는 사실)은 재사용하지 않는다.

FSM을 다시 구현하지 않는다. FSM이 이미 낸 Decision만 보고 판정하므로, ROI·신뢰도·
카메라 상태 규칙이 두 곳에서 어긋날 수 없다.

    pending ──K장 연속 깨끗함──► empty
       │                          │
       ├──사람──► occupied ◄──사람─┤       occupied, fault는 그 confirm_id 동안 유지
       └──이상──► fault    ◄──이상─┘       (새 confirm_id가 와야 다시 시작)

시각 비교(프레임 촬영 시각 > 요청을 받은 시각)는 둘 다 이 노드의 시계라서 안전하다.
"""

from dataclasses import dataclass

from psafe.core.packets import Decision, State


@dataclass(frozen=True, slots=True)
class ConfirmRequest:
    """링크가 worker에게 건네는 '지금 처리 중인 요청'."""
    key: tuple[str, int]      # (C의 boot, confirm_id). C가 재시작해 같은 번호를 다시 써도 다른 요청이 된다
    since: float              # 이 노드 시계로, 이 요청을 처음 받은 시각

    @property
    def confirm_id(self) -> int:
        return self.key[1]


@dataclass(frozen=True, slots=True)
class ConfirmStatus:
    confirm_id: int | None
    result: str | None        # None / pending / empty / occupied / fault (messages.CONFIRM_VALUES)


NO_CONFIRM = ConfirmStatus(None, None)
_FINAL = ("occupied", "fault")


class ConfirmTracker:
    def __init__(self, frames_needed: int):
        if frames_needed < 1:
            raise ValueError("frames_needed must be >= 1")
        self.frames_needed = frames_needed
        self._req: ConfirmRequest | None = None
        self._result: str | None = None
        self._clean = 0

    @property
    def status(self) -> ConfirmStatus:
        if self._req is None:
            return NO_CONFIRM
        return ConfirmStatus(self._req.confirm_id, self._result)

    def step(self, req: ConfirmRequest | None, d: Decision) -> ConfirmStatus:
        """worker가 판단 1개를 낼 때마다 호출한다. req는 링크가 아는 현재 요청
        (C 무응답이면 None -> 초기화)."""
        if req is None:
            self._req, self._result, self._clean = None, None, 0
            return NO_CONFIRM
        if self._req is None or req.key != self._req.key:
            self._req, self._result, self._clean = req, "pending", 0

        if self._result in _FINAL:
            return self.status

        if d.seq < 0:
            # 프레임이 없는 판단 (no_frame, detector_error 등). 지금 막 내린 판단이므로
            # 요청 이후의 정보다 -> 확인 실패.
            if d.decided_at > req.since:
                self._result = "fault"
            return self.status

        if d.timestamp <= req.since:
            return self.status                    # 요청 전에 찍힌 프레임: 근거로 쓰지 않는다

        if d.state == State.FAULT:
            self._result = "fault"
        elif d.count > 0 or d.state == State.OCCUPIED:
            self._result = "occupied"
        else:
            self._clean += 1
            if self._clean >= self.frames_needed:
                self._result = "empty"
        return self.status
