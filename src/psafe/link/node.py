"""A·B 쪽 링크 (DESIGN.md §7.4.7). C의 Control을 받고, Report를 보낸다.

스레드 구성 (ThreadedRunner):
    link-rx    poll()을 반복: 수신 -> decode -> PeerTracker -> 현재 요청 갱신   (락 1개)
    worker     request(now)로 현재 요청을 읽어 ConfirmTracker에 넘긴다         (읽기만)
    heartbeat  send_report(...)로 200 ms마다 Report 송신

C가 무응답이면 request()가 None을 돌려주고, ConfirmTracker는 초기화된다. 이 노드의
자체 출력(ESP32 등)은 C와 상관없이 그대로 동작한다.
"""

import logging
import threading

from psafe.core.clock import Clock
from psafe.core.packets import Decision
from psafe.link.messages import Control, Report, decode, encode, new_boot_id
from psafe.link.peer import NODE_TIMEOUT_MS, PeerTracker
from psafe.link.transport import Address, Transport
from psafe.safety.confirm import NO_CONFIRM, ConfirmRequest, ConfirmStatus

log = logging.getLogger("psafe.link")


class NodeLink:
    def __init__(
        self,
        node: str,
        transport: Transport,
        hub: Address,
        clock: Clock,
        hub_node: str = "C",
        timeout_s: float = NODE_TIMEOUT_MS / 1000.0,
    ):
        self.node = node
        self.transport = transport
        self.hub = hub
        self.clock = clock
        self.hub_node = hub_node
        self.boot = new_boot_id()
        self._seq = 0
        self._lock = threading.Lock()
        self._tracker: PeerTracker[Control] = PeerTracker(timeout_s, expected={hub_node})
        self._req: ConfirmRequest | None = None          # 마지막으로 처음 본 요청
        self.counts: dict[str, int] = {"sent": 0, "wrong_type": 0}

    def open(self) -> None:
        self.transport.open()

    def close(self) -> None:
        self.transport.close()

    # ---- 수신 (link-rx 스레드) ---------------------------------------------

    def poll(self, timeout: float) -> bool:
        """데이터그램 1개를 처리한다. 받은 것이 없으면 False."""
        got = self.transport.recv(timeout)
        if got is None:
            return False
        msg = decode(got[0])
        if isinstance(msg, str):
            self._count(msg)
            return True
        if not isinstance(msg, Control):
            self._count("wrong_type")                    # 다른 노드의 Report가 잘못 들어옴
            return True
        now = self.clock.now()
        with self._lock:
            r = self._tracker.feed(msg, now)
            if r in ("ok", "restart"):
                if msg.confirm_id is None:
                    self._req = None
                else:
                    key = (msg.boot, msg.confirm_id)
                    if self._req is None or self._req.key != key:
                        self._req = ConfirmRequest(key, since=now)
        return True

    def request(self, now: float) -> ConfirmRequest | None:
        """worker용: 지금 처리할 요청. C가 무응답이거나 요청이 없으면 None."""
        with self._lock:
            latest = self._tracker.latest(self.hub_node, now)
            if latest is None or self._req is None:
                return None
            if (latest.msg.boot, latest.msg.confirm_id) != self._req.key:
                return None                              # 방어: 정상이라면 poll()에서 맞춰진다
            return self._req

    def hub_alive(self, now: float) -> bool:
        with self._lock:
            return self._tracker.latest(self.hub_node, now) is not None

    # ---- 송신 (heartbeat 스레드) -------------------------------------------

    def send_report(self, d: Decision, confirm: ConfirmStatus, now: float) -> None:
        self._seq += 1
        evidence_age_ms = None if d.seq < 0 else max(0, round((now - d.timestamp) * 1000))
        if confirm.confirm_id is None:
            confirm = NO_CONFIRM
        # 왕복 시간용 echo (E-21): 마지막으로 받은 Control과, 받은 뒤 지난 시간 (이 노드 시계의 길이)
        with self._lock:
            last = self._tracker.latest(self.hub_node, now)
        echo_seq = echo_hold_ms = None
        if last is not None:
            echo_seq, echo_hold_ms = last.msg.seq, max(0, round((now - last.received_at) * 1000))
        rep = Report(
            node=self.node, boot=self.boot, seq=self._seq,
            state=d.state, safe=d.safe, count=d.count, reason=d.reason[:64] or "-",
            evidence_age_ms=evidence_age_ms,
            confirm_id=confirm.confirm_id, confirm=confirm.result,
            echo_seq=echo_seq, echo_hold_ms=echo_hold_ms,
        )
        self.transport.send(encode(rep), self.hub)
        self.counts["sent"] += 1

    def _count(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1
