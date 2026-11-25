"""C의 판정 (DESIGN.md §7.4.5, §7.4.6). 순수 로직: 입출력 없음, 시각은 인자로 받는다.

입력: 노드별 최신 유효 Report (PeerTracker.latest()의 결과. 무응답이면 None)
출력: Verdict + 이번에 A·B에게 보낼 confirm_id

판정 우선순위 (위가 먼저):
    OCCUPIED   어느 노드든 사람 > 0, 또는 현재 확인에 'occupied'로 답함
    UNKNOWN    무응답, FAULT, 근거가 오래됨, 확인 실패·시간 초과, 규칙에 안 맞는 상태
    CHECKING   사람은 없지만 노드가 아직 CLEAR가 아니거나, 확인이 진행 중
    EMPTY      모든 노드가 CLEAR + 신선하고, 현재 confirm_id에 모두 'empty'로 답함

OCCUPIED가 UNKNOWN보다 위인 이유: 둘 다 N이라 안전성은 같다. 하지만 B가 무응답일 때
A가 본 사람을 '판단 불가'로 덮으면, 관리자가 가장 확실하고 바로 행동할 수 있는 정보를
잃는다. 노드별 상태는 Verdict.nodes로 항상 함께 나간다.

확인(CONFIRM) 규칙:
    * 모든 노드가 CLEAR + 신선 + 사람 0 -> 새 confirm_id 발급 (이미 있으면 유지)
    * 조건이 한 번이라도 깨지면 confirm_id를 버린다 -> 다시 EMPTY가 되려면 새 확인
    * 'fault' 답이나 시간 초과 -> UNKNOWN, confirm_id를 버린다 (다음 step에서 재발급)
    * confirm_id는 부팅마다 임의 값에서 시작한다. 재시작 전 번호에 대한 늦은 'empty'를
      새 확인의 답으로 오인하지 않기 위해서다.
"""

import secrets
from collections.abc import Mapping, Sequence

from psafe.core.packets import State
from psafe.core.verdict import NodeView, Verdict, VerdictKind
from psafe.link.messages import MAX_SEQ, Report
from psafe.link.peer import Received

MAX_EVIDENCE_AGE_MS = 1000.0   # A의 max_decision_age(600) + 보고 간격(200) + 여유. 보고 1개 유실까지 견딤
CONFIRM_TIMEOUT_S = 3.0


class Aggregator:
    def __init__(
        self,
        nodes: Sequence[str],
        max_evidence_age_ms: float = MAX_EVIDENCE_AGE_MS,
        confirm_timeout_s: float = CONFIRM_TIMEOUT_S,
        first_confirm_id: int | None = None,
    ):
        if not nodes or len(set(nodes)) != len(nodes):
            raise ValueError(f"nodes must be non-empty and unique, got {nodes!r}")
        self.nodes = tuple(nodes)
        self.max_evidence_age_ms = max_evidence_age_ms
        self.confirm_timeout_s = confirm_timeout_s
        self._next_id = secrets.randbelow(2**31) if first_confirm_id is None else first_confirm_id
        self._confirm_id: int | None = None
        self._confirm_started = 0.0

    @property
    def confirm_id(self) -> int | None:
        """A·B에게 보낼 현재 confirm_id. step() 이후의 값."""
        return self._confirm_id

    def step(self, latest: Mapping[str, Received[Report] | None], now: float) -> Verdict:
        views = tuple(self._view(n, latest.get(n), now) for n in self.nodes)

        # 1. 사람
        for v in views:
            if v.alive and (v.count > 0 or v.state == State.OCCUPIED or v.confirm == "occupied"):
                self._drop_confirm()
                return self._verdict(VerdictKind.OCCUPIED, f"person_{v.node}", views, now)

        # 2. 판단 불가
        for v in views:
            if (bad := self._unknown_reason(v)) is not None:
                self._drop_confirm()
                return self._verdict(VerdictKind.UNKNOWN, bad, views, now)

        # 3. 아직 CLEAR가 아닌 노드 (자기 FSM이 비어 있음 확인 중)
        for v in views:
            if v.state != State.CLEAR:
                self._drop_confirm()
                return self._verdict(VerdictKind.CHECKING, f"{v.node}_{v.state.value.lower()}", views, now)

        # 4. 모두 CLEAR + 신선: 확인
        if self._confirm_id is None:
            self._confirm_id = self._next_id
            self._next_id = (self._next_id + 1) % (MAX_SEQ + 1)
            self._confirm_started = now
            views = tuple(self._view(n, latest.get(n), now) for n in self.nodes)   # 새 번호 기준으로 다시

        answers = [v.confirm for v in views]
        if "fault" in answers:
            node = views[answers.index("fault")].node
            self._drop_confirm()
            return self._verdict(VerdictKind.UNKNOWN, f"confirm_fault_{node}", views, now)
        if all(a == "empty" for a in answers):
            return self._verdict(VerdictKind.EMPTY, "confirmed", views, now)
        if now - self._confirm_started > self.confirm_timeout_s:
            self._drop_confirm()
            return self._verdict(VerdictKind.UNKNOWN, "confirm_timeout", views, now)
        return self._verdict(VerdictKind.CHECKING, "confirming", views, now)

    # ---- 내부 함수 ---------------------------------------------------------

    def _view(self, node: str, rec: Received[Report] | None, now: float) -> NodeView:
        if rec is None:
            return NodeView(node, False, None, 0, "timeout", None, None)
        r = rec.msg
        # 근거 나이 = 노드가 잰 나이 + 전달 시간 상한(왕복 시간) + C가 받은 뒤 지난 시간 (E-21).
        # 전달 시간을 모르면 근거 나이도 모른다.
        transit_ms = None if rec.transit_s is None else rec.transit_s * 1000.0
        age = None
        if r.evidence_age_ms is not None and transit_ms is not None:
            age = r.evidence_age_ms + transit_ms + (now - rec.received_at) * 1000.0
        confirm = r.confirm if self._confirm_id is not None and r.confirm_id == self._confirm_id else None
        return NodeView(node, True, r.state, r.count, r.reason, age, confirm, transit_ms)

    def _unknown_reason(self, v: NodeView) -> str | None:
        if not v.alive:
            return f"{v.node}_timeout"
        if v.state in (State.FAULT, State.INIT):
            return f"{v.node}_{v.state.value.lower()}"
        if v.state == State.CLEAR and v.transit_ms is None:
            return f"{v.node}_no_rtt"             # 왕복 시간을 몰라서 근거 나이를 알 수 없음
        if v.state == State.CLEAR and (v.evidence_age_ms is None or v.evidence_age_ms > self.max_evidence_age_ms):
            return f"{v.node}_stale"
        return None

    def _drop_confirm(self) -> None:
        self._confirm_id = None

    def _verdict(self, kind: VerdictKind, reason: str, views: tuple[NodeView, ...], now: float) -> Verdict:
        return Verdict(kind, reason, views, self._confirm_id, now)
