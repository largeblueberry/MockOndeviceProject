"""MockMcuOutput: ESP32 없이 같은 프로토콜로 동작하는 가짜 ESP32.

판단을 실제 패킷 바이트로 인코딩해서 protocol.Receiver(펌웨어와 같은 수신
로직)에 넣고, 관문(gate) 상태가 바뀔 때 로그를 남긴다. 펌웨어와 RPi 쪽
인코딩이 맞는지, 워치독이 언제 N을 내는지 PC에서 확인하는 용도다.

    outputs:
      - name: mock_mcu
        options: { drop_every: 0 }   # N번째 패킷마다 버려서 전송 손실 흉내 (0 = 손실 없음)
"""

import logging

from psafe.core.clock import Clock
from psafe.core.packets import Decision
from psafe.outputs.output_api import OUTPUTS, Output
from psafe.outputs.protocol import WATCHDOG_MS, Receiver, encode_decision

log = logging.getLogger("psafe.output.mock_mcu")


@OUTPUTS.register("mock_mcu")
class MockMcuOutput(Output):
    def __init__(self, clock: Clock, watchdog_ms: float = WATCHDOG_MS, drop_every: int = 0):
        self.clock = clock
        self.rx = Receiver(watchdog_ms)
        self.drop_every = drop_every
        self._seq = 0
        self._n = 0
        self.gate = False
        self.transitions: list[tuple[float, bool, str]] = []   # (시각, 허가 여부, 사유)

    def send(self, decision: Decision) -> None:
        now = self.clock.now()
        self._update(now)                  # 이번 패킷 전에 워치독이 만료됐는지 먼저 본다
        self._n += 1
        pkt = encode_decision(self._seq, decision)
        self._seq = (self._seq + 1) & 0xFF
        if not (self.drop_every and self._n % self.drop_every == 0):
            self.rx.feed(pkt, now)
        self._update(now)

    def _update(self, now: float) -> None:
        safe, why = self.rx.gate(now)
        if safe != self.gate:
            self.gate = safe
            self.transitions.append((now, safe, why))
            log.info("MCU gate %s (%s) t=%.2f", "Y" if safe else "N", why, now)

    def close(self) -> None:
        log.info("MCU packets %s", self.rx.counts)
