"""SerialOutput: 판단을 USB 시리얼로 ESP32에 보낸다 (DESIGN.md §7.2~7.3).

send()는 heartbeat 스레드가 200 ms마다 호출하므로 threaded 모드에서 쓴다.
포트가 끊기면 예외를 던지지 않고 1초마다 다시 연결을 시도한다. 그동안 ESP32는
패킷을 받지 못하므로 워치독에 의해 스스로 N이 된다.

ESP32가 같은 시리얼로 돌려보내는 상태 줄(BOOT / GATE / STAT)도 읽어서 로그에
남긴다. 관문 상태가 바뀔 때마다 "ESP32 > GATE Y seq=45 (last sent: Y)"처럼
마지막으로 보낸 판단과 함께 기록하므로, LED 없이도 판단과 실제 출력이 일치하는지
로그로 확인할 수 있다. 판단에는 쓰지 않는다 (관측용).

ESP32 DevKit은 포트를 열 때 DTR/RTS 신호로 리셋될 수 있다. 부팅하는 동안
(약 1초) 받은 패킷은 버려지며, ESP32는 부팅 직후 N이므로 안전에는 문제가 없다.
"""

import logging

from psafe.core.clock import Clock
from psafe.core.packets import Decision
from psafe.outputs.output_api import OUTPUTS, Output
from psafe.outputs.protocol import encode_decision

log = logging.getLogger("psafe.output.serial")

MAX_LINE = 256   # 이보다 긴 줄은 잡음으로 보고 버린다


@OUTPUTS.register("serial")
class SerialOutput(Output):
    def __init__(self, clock: Clock, port: str, baud: int = 115200, reconnect_s: float = 1.0,
                 drain_s: float = 0.3):
        self.clock = clock
        self.port, self.baud = port, baud
        self.reconnect_s = reconnect_s
        self.drain_s = drain_s          # 종료 시 ESP32의 마지막 응답을 기다리는 시간
        self._ser = None
        self._seq = 0
        self._next_retry = 0.0
        self._rx = b""
        self._last_sent: bool | None = None
        self.sent = 0
        self.errors = 0
        self.mcu_gate: bool | None = None   # ESP32가 마지막으로 보고한 관문 상태
        self.mcu_gate_changes = 0

    def _connect(self) -> None:
        import serial  # pyserial: 시리얼을 쓰지 않는 실행에서는 설치하지 않아도 된다

        self._ser = serial.Serial(self.port, self.baud, timeout=0, write_timeout=0.05)
        self._rx = b""

    def open(self) -> None:
        try:
            self._connect()
        except Exception as e:  # 시작 시 포트가 없으면 실행하지 않는다
            raise RuntimeError(f"serial port {self.port} could not be opened: {e}") from e
        log.info("serial %s @ %d opened", self.port, self.baud)

    def send(self, decision: Decision) -> None:
        if self._ser is None:
            now = self.clock.now()
            if now < self._next_retry:
                return
            try:
                self._connect()
                log.warning("serial %s reconnected", self.port)
            except Exception:
                self._next_retry = now + self.reconnect_s
                return
        try:
            self._ser.write(encode_decision(self._seq, decision))
            self._seq = (self._seq + 1) & 0xFF
            self._last_sent = decision.safe
            self.sent += 1
            self._read_status()
        except Exception as e:
            self.errors += 1
            log.warning("serial %s write failed (%s); retrying every %.1f s", self.port, e, self.reconnect_s)
            self._drop()
            self._next_retry = self.clock.now() + self.reconnect_s

    # ---- ESP32 상태 줄 ----------------------------------------------------

    def _read_status(self) -> None:
        data = self._ser.read(4096)     # timeout=0: 이미 도착한 만큼만 읽고 바로 반환
        if not data:
            return
        self._rx += data
        *lines, self._rx = self._rx.split(b"\n")
        if len(self._rx) > MAX_LINE:
            self._rx = b""
        for raw in lines:
            self._on_line(raw.decode("ascii", errors="replace").strip())

    def _on_line(self, line: str) -> None:
        if line.startswith("GATE "):
            gate = line.startswith("GATE Y")
            if gate != self.mcu_gate:
                self.mcu_gate_changes += 1
            self.mcu_gate = gate
            sent = "-" if self._last_sent is None else ("Y" if self._last_sent else "N")
            log.info("ESP32 > %s (last sent: %s)", line, sent)
        elif line.startswith("BOOT"):
            self.mcu_gate = False
            log.info("ESP32 > %s", line)
        elif line.startswith("STAT"):
            log.debug("ESP32 > %s", line)
        elif line:
            log.debug("ESP32 (other) > %s", line[:80])   # 부팅 ROM 메시지 등

    # ---- 종료 --------------------------------------------------------------

    def _drop(self) -> None:
        try:
            if self._ser is not None:
                self._ser.close()
        except Exception:
            pass
        self._ser = None

    def close(self) -> None:
        # Runner는 close 직전에 N("shutdown")을 보낸다. ESP32가 그것을 반영했는지
        # 응답(GATE N)을 잠시 기다려 로그에 남긴다.
        if self._ser is not None and self.drain_s > 0:
            end = self.clock.now() + self.drain_s
            try:
                while self.clock.now() < end and self.mcu_gate is not False:
                    self._read_status()
                    self.clock.sleep(0.02)
            except Exception:
                pass
        final = {None: "unknown", True: "Y", False: "N"}[self.mcu_gate]
        self._drop()
        log.info("serial %s closed (%d packets sent, %d errors, ESP32 gate at close: %s)",
                 self.port, self.sent, self.errors, final)
