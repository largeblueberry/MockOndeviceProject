"""RPi -> ESP32 패킷 프로토콜 (DESIGN.md §7.3).

    | STX 0xAA | SEQ | STATE | SAFE | CRC8 |     5바이트, 200 ms마다 송신 (하트비트 겸용)

    SEQ    송신할 때마다 1씩 증가, 255 다음은 0
    STATE  INIT=0, OCCUPIED=1, VERIFYING=2, CLEAR=3, FAULT=4
    SAFE   'Y'(0x59) 또는 'N'(0x4E). 'Y'는 STATE가 CLEAR일 때만 유효
    CRC8   SEQ, STATE, SAFE 3바이트에 대한 CRC-8 (다항식 0x07, 초기값 0x00)

수신 쪽(ESP32 펌웨어, MockMcu)은 같은 규칙을 따른다:
  * 최근 5바이트를 밀어 가며 보고(슬라이딩 윈도), 첫 바이트가 STX이고 CRC가 맞으면 패킷으로 인정
  * 규칙에 어긋나는 패킷(STATE 범위 밖, CLEAR가 아닌데 'Y')과 직전과 같은 SEQ는 무시
  * 마지막 유효 패킷 이후 500 ms가 지나면 강제 N
이 파일의 상수와 firmware/esp32/psafe_gate/psafe_gate.ino의 상수는 항상 같아야 한다.
"""

from dataclasses import dataclass

from psafe.core.packets import Decision, State

STX = 0xAA
PACKET_LEN = 5
CRC_POLY = 0x07
SAFE_Y = ord("Y")
SAFE_N = ord("N")
WATCHDOG_MS = 500

STATE_CODES: dict[State, int] = {
    State.INIT: 0,
    State.OCCUPIED: 1,
    State.VERIFYING: 2,
    State.CLEAR: 3,
    State.FAULT: 4,
}
CODE_STATES = {v: k for k, v in STATE_CODES.items()}


def crc8(data: bytes) -> int:
    """CRC-8 (다항식 0x07, 초기값 0x00, 반사 없음). "123456789" -> 0xF4."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ CRC_POLY) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def encode(seq: int, state: State, safe: bool) -> bytes:
    if safe and state != State.CLEAR:
        raise ValueError(f"Y is only valid in CLEAR, got {state.value}")
    body = bytes([seq & 0xFF, STATE_CODES[state], SAFE_Y if safe else SAFE_N])
    return bytes([STX]) + body + bytes([crc8(body)])


def encode_decision(seq: int, d: Decision) -> bytes:
    return encode(seq, d.state, d.safe)


@dataclass(frozen=True, slots=True)
class Packet:
    seq: int
    state: State
    safe: bool


def parse_window(win: bytes) -> Packet | str:
    """5바이트를 해석한다. 유효하면 Packet, 아니면 무시 사유 문자열
    ("no_stx", "bad_crc", "invalid")를 반환한다."""
    if len(win) != PACKET_LEN or win[0] != STX:
        return "no_stx"
    if crc8(win[1:4]) != win[4]:
        return "bad_crc"
    seq, st, sf = win[1], win[2], win[3]
    if st not in CODE_STATES or sf not in (SAFE_Y, SAFE_N):
        return "invalid"
    if sf == SAFE_Y and st != STATE_CODES[State.CLEAR]:
        return "invalid"
    return Packet(seq, CODE_STATES[st], sf == SAFE_Y)


class Receiver:
    """ESP32 펌웨어의 수신 로직을 Python으로 옮긴 것. MockMcu와 테스트가 사용한다."""

    def __init__(self, watchdog_ms: float = WATCHDOG_MS):
        self.watchdog_s = watchdog_ms / 1000.0
        self._win = bytearray()
        self._last_seq: int | None = None
        self._last_ok: float | None = None
        self._last_safe = False
        self.counts = {"ok": 0, "bad_crc": 0, "invalid": 0, "dup": 0}

    def feed(self, data: bytes, now: float) -> None:
        for b in data:
            self._win.append(b)
            if len(self._win) > PACKET_LEN:
                del self._win[0]
            if len(self._win) < PACKET_LEN or self._win[0] != STX:
                continue
            r = parse_window(bytes(self._win))
            if r == "bad_crc":
                self.counts["bad_crc"] += 1
                continue                      # 계속 밀어 가며 다음 STX를 찾는다
            self._win.clear()                 # 5바이트를 소비했다
            if isinstance(r, str):
                self.counts["invalid"] += 1
            elif r.seq == self._last_seq:
                self.counts["dup"] += 1
            else:
                self.counts["ok"] += 1
                self._last_seq, self._last_ok, self._last_safe = r.seq, now, r.safe

    def gate(self, now: float) -> tuple[bool, str]:
        """(허가 여부, 사유). 사유: boot | timeout | packet_n | ok"""
        if self._last_ok is None:
            return False, "boot"
        if now - self._last_ok > self.watchdog_s:
            return False, "timeout"
        if not self._last_safe:
            return False, "packet_n"
        return True, "ok"
