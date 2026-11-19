"""노드별 수신 추적 (DESIGN.md §7.4.4 수신 규칙). 순수 로직: 시각은 인자로 받는다.

ESP32 쪽 Receiver(outputs/protocol.py)의 네트워크판이다. A·B(Control 수신)와
C(Report 수신)가 같이 쓴다.

    * 같은 boot에서 seq가 이전보다 작거나 같으면 무시 (지연 도착·중복)
    * boot가 바뀌면 재시작으로 보고 받아들인다. 단, 이미 지나간 boot의 메시지는
      무시한다 (재시작 전 프로세스가 보낸 메시지가 늦게 도착한 것)
    * 마지막 유효 메시지 후 timeout_s가 지나면 그 노드는 무응답 (latest()가 None)
    * 한 번도 받지 못한 노드도 무응답 (부팅 직후는 항상 무응답)

시각(now)은 모두 '받는 쪽' 시계다. 보내는 쪽 시각은 쓰지 않는다 (§7.4.3).
"""

import logging
from collections import deque
from dataclasses import dataclass
from typing import Generic, TypeVar

from psafe.link.messages import Control, Report

log = logging.getLogger("psafe.link")

NODE_TIMEOUT_MS = 500
RETIRED_BOOTS = 8                  # 노드별로 기억할 지나간 boot 수

M = TypeVar("M", Report, Control)


@dataclass(frozen=True, slots=True)
class Received(Generic[M]):
    msg: M
    received_at: float             # 받는 쪽 Clock 기준


@dataclass
class _Peer(Generic[M]):
    boot: str
    seq: int
    last: Received[M]
    retired: deque[str]


class PeerTracker(Generic[M]):
    """노드 이름별 최신 유효 메시지.

    expected: 받아들일 노드 이름 집합. None이면 아무 노드나 받는다.
    """

    def __init__(self, timeout_s: float = NODE_TIMEOUT_MS / 1000.0, expected: set[str] | None = None):
        self.timeout_s = timeout_s
        self.expected = frozenset(expected) if expected is not None else None
        self._peers: dict[str, _Peer[M]] = {}
        self.counts = {"ok": 0, "restart": 0, "dup": 0, "old_boot": 0, "unknown_node": 0}

    def feed(self, msg: M, now: float) -> str:
        """메시지 1개를 처리한다. 결과: ok | restart | dup | old_boot | unknown_node.
        ok와 restart만 받아들여진 것이다."""
        r = self._classify(msg)
        self.counts[r] += 1
        if r == "ok":
            p = self._peers[msg.node]
            p.seq, p.last = msg.seq, Received(msg, now)
        elif r == "restart":
            p = self._peers.get(msg.node)
            if p is None:
                self._peers[msg.node] = _Peer(msg.boot, msg.seq, Received(msg, now), deque(maxlen=RETIRED_BOOTS))
            else:
                log.info("node %s restarted (boot %s -> %s)", msg.node, p.boot, msg.boot)
                p.retired.append(p.boot)
                p.boot, p.seq, p.last = msg.boot, msg.seq, Received(msg, now)
        return r

    def _classify(self, msg: M) -> str:
        if self.expected is not None and msg.node not in self.expected:
            return "unknown_node"
        p = self._peers.get(msg.node)
        if p is None:
            return "restart"                 # 처음 보는 노드 = 첫 부팅
        if msg.boot == p.boot:
            return "ok" if msg.seq > p.seq else "dup"
        if msg.boot in p.retired:
            return "old_boot"
        return "restart"

    def latest(self, node: str, now: float) -> Received[M] | None:
        """그 노드의 최신 유효 메시지. 무응답(한 번도 못 받음, timeout 초과)이면 None."""
        p = self._peers.get(node)
        if p is None or now - p.last.received_at > self.timeout_s:
            return None
        return p.last

    def silent_for(self, node: str, now: float) -> float | None:
        """마지막 유효 메시지 이후 경과 시간(초). 한 번도 못 받았으면 None. 화면·로그용."""
        p = self._peers.get(node)
        return None if p is None else now - p.last.received_at
