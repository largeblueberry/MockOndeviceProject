"""보드 간 전송 계층 (DESIGN.md §7.4.2). 바이트를 주소로 보내고 받기만 한다.

    Transport            인터페이스. 메시지 형식(messages.py)도, 판단 로직도 모른다
    UdpTransport         실제 네트워크 (Python 표준 socket)
    LoopbackNetwork      메모리 안의 가짜 네트워크. 유실·지연·중복·깨짐을 일부러 넣는다
                         -> A·B·C를 한 프로세스에서, FakeClock으로 결정적으로 돌린다

전송 계층은 신뢰성을 보장하지 않는다 (UDP와 같다). 유실은 다음 주기 메시지가,
끊김은 받는 쪽의 무응답 판정(peer.py)이 처리한다. 그래서 send()는 네트워크
오류로 예외를 던지지 않고 세기만 한다.
"""

import logging
import random
import socket
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from psafe.core.clock import Clock

log = logging.getLogger("psafe.link")

Address = tuple[str, int]          # (호스트, 포트)
RECV_BUFFER = 65535                # 데이터그램 최대 크기. 작게 잡으면 Windows에서 큰 패킷에 예외가 난다


class Transport(ABC):
    """주소 하나에 묶인 송수신 끝점."""

    def open(self) -> None: ...

    @abstractmethod
    def send(self, data: bytes, to: Address) -> None:
        """보낸다. 도착은 보장하지 않으며, 네트워크 오류로 예외를 던지지 않는다."""

    @abstractmethod
    def recv(self, timeout: float) -> tuple[bytes, Address] | None:
        """데이터그램 1개를 받는다. timeout초 안에 없으면 None."""

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# UDP
# ---------------------------------------------------------------------------

class UdpTransport(Transport):
    def __init__(self, bind: Address):
        self.bind = bind
        self._sock: socket.socket | None = None
        self.send_errors = 0

    def open(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if sys.platform == "win32" and hasattr(socket, "SIO_UDP_CONNRESET"):
            # Windows는 상대 포트가 닫혀 있다는 ICMP를 받으면 다음 recvfrom()에서
            # ConnectionResetError를 던진다. UDP에서는 의미 없는 오류라 끈다.
            s.ioctl(socket.SIO_UDP_CONNRESET, False)
        s.bind(self.bind)
        self._sock = s

    @property
    def address(self) -> Address:
        """실제로 묶인 주소 (포트 0으로 열었을 때 OS가 고른 포트 확인용)."""
        if self._sock is None:
            raise RuntimeError("transport is not open")
        return self._sock.getsockname()[:2]

    def send(self, data: bytes, to: Address) -> None:
        if self._sock is None:
            raise RuntimeError("transport is not open")
        try:
            self._sock.sendto(data, to)
        except OSError as e:
            # 랜선이 빠졌거나 경로가 없음. 받는 쪽 무응답 판정이 처리한다.
            self.send_errors += 1
            if self.send_errors == 1 or self.send_errors % 100 == 0:
                log.warning("udp send to %s failed (%d so far): %s", to, self.send_errors, e)

    def recv(self, timeout: float) -> tuple[bytes, Address] | None:
        if self._sock is None:
            raise RuntimeError("transport is not open")
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            if timeout <= 0:
                self._sock.settimeout(0.0)          # 기다리지 않음: 없으면 BlockingIOError
            else:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._sock.settimeout(left)
            try:
                data, addr = self._sock.recvfrom(RECV_BUFFER)
                return data, addr[:2]
            except (socket.timeout, BlockingIOError):
                return None
            except ConnectionResetError:
                # SIO_UDP_CONNRESET이 없는 환경 대비. 다음 데이터그램을 기다린다.
                if time.monotonic() >= deadline:
                    return None

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None


# ---------------------------------------------------------------------------
# Loopback (테스트·시뮬레이션용)
# ---------------------------------------------------------------------------

@dataclass
class LinkFault:
    """한 방향 링크(보내는 쪽 -> 받는 쪽)에 넣을 고장. 확률은 0..1."""
    loss: float = 0.0          # 사라짐
    duplicate: float = 0.0     # 두 번 도착
    corrupt: float = 0.0       # 바이트 하나가 바뀜
    delay_s: float = 0.0       # 고정 지연
    jitter_s: float = 0.0      # 0..jitter_s 사이 추가 지연 (순서가 뒤바뀔 수 있다)


class LoopbackNetwork:
    """주소 -> 끝점 사전과 도착 대기열. 시각은 주입된 Clock을 따른다.

    FakeClock과 쓸 때는 recv(timeout=0)으로 '지금 도착한 것'만 꺼낸다
    (시뮬레이션 쪽이 시간을 직접 진행시킨다). RealClock이면 recv가 실제로 기다린다.
    """

    def __init__(self, clock: Clock, seed: int = 0):
        self.clock = clock
        self._rng = random.Random(seed)
        self._cond = threading.Condition()
        self._endpoints: dict[Address, "LoopbackTransport"] = {}
        self._faults: dict[tuple[Address, Address], LinkFault] = {}
        self._order = 0            # 같은 도착 시각이면 보낸 순서대로
        self.counts = {"sent": 0, "lost": 0, "duplicated": 0, "corrupted": 0, "no_endpoint": 0}

    def endpoint(self, addr: Address) -> "LoopbackTransport":
        return LoopbackTransport(self, addr)

    def set_fault(self, src: Address, dst: Address, fault: LinkFault | None) -> None:
        """src -> dst 방향 고장 설정. None이면 정상으로 되돌린다."""
        with self._cond:
            if fault is None:
                self._faults.pop((src, dst), None)
            else:
                self._faults[(src, dst)] = fault

    def cut(self, a: Address, b: Address) -> None:
        """양방향 단절 (랜선 뽑기)."""
        self.set_fault(a, b, LinkFault(loss=1.0))
        self.set_fault(b, a, LinkFault(loss=1.0))

    def heal(self, a: Address, b: Address) -> None:
        self.set_fault(a, b, None)
        self.set_fault(b, a, None)

    # ---- 끝점이 호출 ------------------------------------------------------

    def _attach(self, ep: "LoopbackTransport") -> None:
        with self._cond:
            if ep.address in self._endpoints:
                raise OSError(f"address already in use: {ep.address}")
            self._endpoints[ep.address] = ep

    def _detach(self, ep: "LoopbackTransport") -> None:
        with self._cond:
            if self._endpoints.get(ep.address) is ep:
                del self._endpoints[ep.address]
            self._cond.notify_all()

    def _send(self, src: Address, dst: Address, data: bytes) -> None:
        with self._cond:
            self.counts["sent"] += 1
            f = self._faults.get((src, dst), LinkFault())
            if self._rng.random() < f.loss:
                self.counts["lost"] += 1
                return
            copies = 1
            if self._rng.random() < f.duplicate:
                copies = 2
                self.counts["duplicated"] += 1
            if data and self._rng.random() < f.corrupt:
                b = bytearray(data)
                b[self._rng.randrange(len(b))] ^= 1 << self._rng.randrange(8)
                data = bytes(b)
                self.counts["corrupted"] += 1
            ep = self._endpoints.get(dst)
            if ep is None:                       # 받는 쪽이 없음 (UDP처럼 조용히 사라진다)
                self.counts["no_endpoint"] += 1
                return
            now = self.clock.now()
            for _ in range(copies):
                at = now + f.delay_s + (self._rng.random() * f.jitter_s if f.jitter_s else 0.0)
                self._order += 1
                ep._inbox.append((at, self._order, data, src))
            ep._inbox.sort()
            self._cond.notify_all()

    def _recv(self, ep: "LoopbackTransport", timeout: float) -> tuple[bytes, Address] | None:
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._cond:
            while True:
                if ep._inbox and ep._inbox[0][0] <= self.clock.now():
                    _, _, data, src = ep._inbox.pop(0)
                    return data, src
                left = deadline - time.monotonic()
                if left <= 0 or ep._closed:
                    return None
                # 지연 중인 메시지의 도착 시각은 Clock 기준이라 깨워 줄 사람이 없다 -> 짧게 나눠 기다린다
                self._cond.wait(min(left, 0.005))


class LoopbackTransport(Transport):
    def __init__(self, net: LoopbackNetwork, address: Address):
        self.net = net
        self.address = address
        self._inbox: list[tuple[float, int, bytes, Address]] = []   # (도착 시각, 순번, 데이터, 보낸 주소)
        self._closed = True

    def open(self) -> None:
        self.net._attach(self)
        self._closed = False

    def send(self, data: bytes, to: Address) -> None:
        if self._closed:
            raise RuntimeError("transport is not open")
        self.net._send(self.address, to, data)

    def recv(self, timeout: float) -> tuple[bytes, Address] | None:
        if self._closed:
            raise RuntimeError("transport is not open")
        return self.net._recv(self, timeout)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.net._detach(self)
