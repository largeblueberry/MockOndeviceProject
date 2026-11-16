"""ESP32 펌웨어(psafe_gate) 자동 점검. 파이프라인 없이 패킷을 직접 보내고,
ESP32가 보고하는 상태 줄(GATE / STAT)로 동작을 판정한다.

    python -m psafe.tools.mcucheck --list              # 시리얼 포트 목록
    python -m psafe.tools.mcucheck --port COM5         # 전체 점검 (약 12초)

점검 항목
  1. N 패킷             -> N 유지
  2. Y 패킷             -> Y로 전환
  3. 송신 중단          -> 약 0.5초 후 N (워치독)
  4. CRC가 깨진 Y        -> 무시 (N 유지)
  5. 같은 SEQ 반복       -> 무시되어 워치독으로 N
  6. CLEAR가 아닌데 Y     -> 무시 (N 유지)
  7. Y 중에 N 패킷       -> 즉시 N
"""

import argparse
import sys
import threading
import time
from dataclasses import dataclass, field

from psafe.core.packets import State
from psafe.outputs.protocol import SAFE_Y, STATE_CODES, STX, crc8, encode

PERIOD_S = 0.2


@dataclass
class Log:
    lines: list[tuple[float, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, line: str) -> None:
        with self.lock:
            self.lines.append((time.monotonic(), line))

    def between(self, t0: float, t1: float, prefix: str) -> list[tuple[float, str]]:
        with self.lock:
            return [(t, s) for t, s in self.lines if t0 <= t <= t1 and s.startswith(prefix)]


def reader(ser, log: Log, stop: threading.Event) -> None:
    buf = b""
    while not stop.is_set():
        chunk = ser.read(256)
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode("ascii", errors="replace").strip()
            if line:
                log.add(line)
                print(f"    ESP32 > {line}")


class Sender:
    def __init__(self, ser):
        self.ser = ser
        self.seq = 0
        self.last_sent = 0.0

    def stream(self, seconds: float, make) -> None:
        """PERIOD_S마다 make(seq)가 만든 바이트를 seconds 동안 보낸다."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.ser.write(make(self.seq))
            self.last_sent = time.monotonic()
            self.seq = (self.seq + 1) & 0xFF
            time.sleep(PERIOD_S)


def y(seq: int) -> bytes:
    return encode(seq, State.CLEAR, True)


def n(seq: int) -> bytes:
    return encode(seq, State.FAULT, False)


def bad_crc_y(seq: int) -> bytes:
    p = bytearray(y(seq))
    p[4] ^= 0xFF
    return bytes(p)


def y_in_fault(seq: int) -> bytes:
    """CRC는 맞지만 규칙 위반: FAULT 상태인데 'Y'."""
    body = bytes([seq, STATE_CODES[State.FAULT], SAFE_Y])
    return bytes([STX]) + body + bytes([crc8(body)])


def run(port: str) -> int:
    import serial

    ser = serial.Serial(port, 115200, timeout=0.05)
    log, stop = Log(), threading.Event()
    threading.Thread(target=reader, args=(ser, log, stop), daemon=True).start()
    tx = Sender(ser)
    results: list[tuple[str, bool, str]] = []

    print(f"[{port}] 연결. ESP32 부팅 대기 (포트를 열면 리셋될 수 있음)...")
    t_boot = time.monotonic()
    while time.monotonic() - t_boot < 3.0 and not log.between(t_boot, time.monotonic(), "BOOT"):
        time.sleep(0.05)
    booted = bool(log.between(t_boot, time.monotonic(), "BOOT"))
    print("  BOOT 확인" if booted else "  BOOT 줄 없음 (리셋되지 않았을 수 있음). 계속 진행")
    ser.reset_input_buffer()

    def check(name: str, t0: float, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} - {detail}")

    def step(name: str) -> float:
        print(f"\n- {name}")
        return time.monotonic()

    # 1. N 패킷 -> N 유지
    t0 = step("1. N 패킷 1초")
    tx.stream(1.0, n)
    ys = log.between(t0, time.monotonic(), "GATE Y")
    check("N 유지", t0, not ys, "GATE Y 없음" if not ys else "Y로 바뀜")

    # 2. Y 패킷 -> Y
    t0 = step("2. Y 패킷 2초")
    tx.stream(2.0, y)
    ys = log.between(t0, time.monotonic(), "GATE Y")
    check("Y 전환", t0, bool(ys), f"{ys[0][0] - t0:.2f}초 후 GATE Y" if ys else "GATE Y 없음")

    # 3. 송신 중단 -> 워치독 N
    t0 = step("3. 송신 중단 1.5초")
    last = tx.last_sent
    time.sleep(1.5)
    ns = log.between(t0, time.monotonic(), "GATE N reason=timeout")
    if ns:
        gap = ns[0][0] - last
        check("워치독 N", t0, 0.4 <= gap <= 0.9, f"마지막 송신 {gap:.2f}초 후 N ({ns[0][1]})")
    else:
        check("워치독 N", t0, False, "GATE N reason=timeout 없음")

    # 4. CRC 깨진 Y -> 무시
    t0 = step("4. CRC가 깨진 Y 1초")
    tx.stream(1.0, bad_crc_y)
    ys = log.between(t0, time.monotonic(), "GATE Y")
    check("CRC 오류 무시", t0, not ys, "N 유지" if not ys else "Y로 바뀜")

    # 5. 같은 SEQ 반복 -> 무시되어 워치독 N
    t0 = step("5. Y 1초 후 같은 SEQ 반복 1.5초")
    tx.stream(1.0, y)
    frozen = (tx.seq - 1) & 0xFF
    t_dup = time.monotonic()
    tx.stream(1.5, lambda _s: y(frozen))
    ns = log.between(t_dup, time.monotonic(), "GATE N reason=timeout")
    check("SEQ 반복 무시", t0, bool(ns), f"반복 시작 {ns[0][0] - t_dup:.2f}초 후 N" if ns else "N으로 안 바뀜")

    # 6. CLEAR가 아닌데 Y -> 무시
    t0 = step("6. FAULT 상태인데 Y 1초")
    tx.stream(1.0, y_in_fault)
    ys = log.between(t0, time.monotonic(), "GATE Y")
    check("규칙 위반 무시", t0, not ys, "N 유지" if not ys else "Y로 바뀜")

    # 7. Y 중에 N -> 즉시 N
    t0 = step("7. Y 1초 후 N 패킷 0.6초")
    tx.stream(1.0, y)
    t_n = time.monotonic()
    tx.stream(0.6, n)
    ns = log.between(t_n, time.monotonic(), "GATE N reason=packet_n")
    check("N 즉시 반영", t0, bool(ns) and ns[0][0] - t_n < 0.3,
          f"{ns[0][0] - t_n:.2f}초 후 N" if ns else "GATE N reason=packet_n 없음")

    tx.stream(0.4, n)            # 끝은 N으로
    time.sleep(1.1)              # 마지막 STAT 줄 받기
    stop.set()
    ser.close()

    passed = sum(ok for _, ok, _ in results)
    print(f"\n결과: {passed}/{len(results)} 통과")
    return 0 if passed == len(results) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m psafe.tools.mcucheck")
    ap.add_argument("--port", help="예: COM5, /dev/ttyUSB0")
    ap.add_argument("--list", action="store_true", help="시리얼 포트 목록")
    args = ap.parse_args(argv)
    if args.list or not args.port:
        from serial.tools import list_ports
        for p in list_ports.comports():
            print(f"{p.device:10s} {p.description}")
        return 0 if args.list else 2
    return run(args.port)


if __name__ == "__main__":
    sys.exit(main())
