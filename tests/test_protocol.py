"""RPi -> ESP32 프로토콜, MockMcu, SerialOutput (실제 포트 없이)."""

import pytest

from psafe.app import build
from psafe.core.clock import FakeClock
from psafe.core.config import load_config
from psafe.core.packets import Decision, State, unsafe
from psafe.outputs.mock_mcu import MockMcuOutput
from psafe.outputs.protocol import STATE_CODES, STX, Receiver, crc8, encode, parse_window
from psafe.runners import SyncRunner


def test_crc8_check_value():
    assert crc8(b"123456789") == 0xF4   # CRC-8 (poly 0x07, init 0x00) 표준 검사값


def test_encode_layout_and_roundtrip():
    p = encode(7, State.CLEAR, True)
    assert len(p) == 5 and p[0] == STX and p[1] == 7 and p[2] == STATE_CODES[State.CLEAR] and p[3] == ord("Y")
    r = parse_window(p)
    assert (r.seq, r.state, r.safe) == (7, State.CLEAR, True)


def test_y_outside_clear_cannot_be_encoded():
    with pytest.raises(ValueError):
        encode(0, State.VERIFYING, True)


def test_parse_rejects_bad_crc_and_rule_violations():
    p = bytearray(encode(1, State.CLEAR, True))
    p[4] ^= 1
    assert parse_window(bytes(p)) == "bad_crc"
    body = bytes([1, STATE_CODES[State.FAULT], ord("Y")])     # CRC는 맞지만 FAULT인데 Y
    assert parse_window(bytes([STX]) + body + bytes([crc8(body)])) == "invalid"
    body = bytes([1, 9, ord("N")])                            # STATE 범위 밖
    assert parse_window(bytes([STX]) + body + bytes([crc8(body)])) == "invalid"


def test_receiver_gate_and_watchdog():
    rx = Receiver(watchdog_ms=500)
    assert rx.gate(0.0) == (False, "boot")
    rx.feed(encode(0, State.CLEAR, True), 0.0)
    assert rx.gate(0.2) == (True, "ok")
    assert rx.gate(0.5) == (True, "ok")            # 정확히 500 ms까지는 유효
    assert rx.gate(0.51) == (False, "timeout")
    rx.feed(encode(1, State.OCCUPIED, False), 0.6)
    assert rx.gate(0.6) == (False, "packet_n")


def test_receiver_ignores_duplicate_seq():
    rx = Receiver()
    rx.feed(encode(5, State.CLEAR, True), 0.0)
    rx.feed(encode(5, State.CLEAR, True), 0.4)    # 같은 바이트 반복 = 새 정보 아님
    assert rx.counts["dup"] == 1
    assert rx.gate(0.6) == (False, "timeout")      # 반복으로는 워치독이 갱신되지 않는다


def test_receiver_resyncs_after_garbage_and_split_bytes():
    rx = Receiver()
    junk = bytes([0x00, STX, 0x13, STX, 0xFF])     # STX가 섞인 잡음
    pkt = encode(0xAA, State.CLEAR, True)          # SEQ 자체가 0xAA인 경우
    stream = junk + pkt
    for i in range(len(stream)):                   # 1바이트씩 쪼개서 도착
        rx.feed(stream[i:i + 1], 0.0)
    assert rx.counts["ok"] == 1 and rx.gate(0.1) == (True, "ok")


def _run_with_mock(drop_every: int = 0) -> MockMcuOutput:
    cfg = load_config("config/default.yaml")
    c = build(cfg, FakeClock())
    mcu = MockMcuOutput(c.clock, drop_every=drop_every)
    c.outputs.append(mcu)
    SyncRunner(c).run()
    return mcu


def test_mock_mcu_follows_pipeline_decisions():
    mcu = _run_with_mock()
    ys = [t for t, safe, _ in mcu.transitions if safe]
    # entry_normal: 0~5초 사람 없음 -> 3.0에 Y, 5~9초 사람 -> N, 9.0 + 3초 -> 12.0에 Y
    assert ys == pytest.approx([3.0, 12.0], abs=0.1)
    assert mcu.transitions[-1][1] is False          # 마지막은 shutdown N
    assert mcu.rx.counts["bad_crc"] == mcu.rx.counts["invalid"] == mcu.rx.counts["dup"] == 0


def test_mock_mcu_tolerates_occasional_packet_loss():
    mcu = _run_with_mock(drop_every=3)              # 3개 중 1개 손실 (15 fps라 간격 < 500 ms)
    assert any(safe for _, safe, _ in mcu.transitions)


class FakeSerial:
    instances: list["FakeSerial"] = []
    fail_writes = 0

    def __init__(self, port, baud, timeout=None, write_timeout=None):
        self.port, self.written, self.closed = port, bytearray(), False
        self.incoming = bytearray()                  # ESP32가 보낸 것처럼 꾸밀 바이트
        FakeSerial.instances.append(self)

    def read(self, n):
        data, self.incoming[:] = bytes(self.incoming[:n]), self.incoming[n:]
        return data

    def write(self, data):
        if FakeSerial.fail_writes > 0:
            FakeSerial.fail_writes -= 1
            raise OSError("device disconnected")
        self.written += data

    def close(self):
        self.closed = True


@pytest.fixture
def fake_serial(monkeypatch):
    serial = pytest.importorskip("serial")
    FakeSerial.instances, FakeSerial.fail_writes = [], 0
    monkeypatch.setattr(serial, "Serial", FakeSerial)
    return FakeSerial


def _decision(safe: bool, t: float) -> Decision:
    if not safe:
        return unsafe(State.OCCUPIED, "person_in_roi", t)
    return Decision(True, State.CLEAR, "clear", 1, t, t)


def test_serial_output_writes_valid_packets(fake_serial):
    from psafe.outputs.serial_out import SerialOutput

    clock = FakeClock()
    out = SerialOutput(clock, port="COM_TEST")
    out.open()
    for i in range(300):                            # SEQ가 255 다음 0으로 넘어가는지 확인
        out.send(_decision(i % 2 == 0, clock.now()))
        clock.advance(0.2)
    data = bytes(fake_serial.instances[0].written)
    rx = Receiver()
    rx.feed(data, 0.0)
    assert rx.counts["ok"] == 300 and rx.counts["dup"] == 0
    assert data[1] == 0 and data[5 * 256 + 1] == 0  # 0번째, 256번째 패킷의 SEQ


def test_serial_output_reconnects_after_write_failure(fake_serial):
    from psafe.outputs.serial_out import SerialOutput

    clock = FakeClock()
    out = SerialOutput(clock, port="COM_TEST", reconnect_s=1.0)
    out.open()
    fake_serial.fail_writes = 1
    out.send(_decision(True, 0.0))                  # 실패 -> 포트 닫고 1초 뒤 재시도
    assert out.errors == 1 and fake_serial.instances[0].closed
    clock.advance(0.5)
    out.send(_decision(True, clock.now()))          # 아직 재시도 시간 전
    assert len(fake_serial.instances) == 1
    clock.advance(0.6)
    out.send(_decision(True, clock.now()))          # 재연결 후 송신
    assert len(fake_serial.instances) == 2 and len(fake_serial.instances[1].written) == 5


def test_serial_output_fails_fast_when_port_missing(monkeypatch):
    serial = pytest.importorskip("serial")
    from psafe.outputs.serial_out import SerialOutput

    def boom(*a, **k):
        raise serial.SerialException("could not open port")
    monkeypatch.setattr(serial, "Serial", boom)
    with pytest.raises(RuntimeError):
        SerialOutput(FakeClock(), port="COM_NONE").open()


def test_serial_output_logs_esp32_gate_lines(fake_serial, caplog):
    from psafe.outputs.serial_out import SerialOutput

    clock = FakeClock()
    out = SerialOutput(clock, port="COM_TEST")
    out.open()
    port = fake_serial.instances[0]
    port.incoming += b"\x00\xffets Jul 29 2019\r\nBOOT N\r\nGATE Y se"   # 부팅 잡음 + 줄이 쪼개져 도착
    with caplog.at_level("INFO", logger="psafe.output.serial"):
        out.send(_decision(True, 0.0))
        assert out.mcu_gate is False                        # BOOT N까지만 완성된 줄
        port.incoming += b"q=0\r\nSTAT ok=1 gate=Y\r\n"
        out.send(_decision(True, 0.2))
    assert out.mcu_gate is True and out.mcu_gate_changes == 1
    assert any("ESP32 > GATE Y seq=0 (last sent: Y)" in r.getMessage() for r in caplog.records)


def test_serial_output_close_waits_for_final_gate_n(fake_serial, caplog):
    from psafe.outputs.serial_out import SerialOutput

    clock = FakeClock()
    out = SerialOutput(clock, port="COM_TEST", drain_s=0.3)
    out.open()
    port = fake_serial.instances[0]
    port.incoming += b"GATE Y seq=0\n"
    out.send(_decision(True, 0.0))
    out.send(unsafe(State.FAULT, "shutdown", 0.2))          # Runner가 마지막으로 보내는 N
    port.incoming += b"GATE N reason=packet_n since_ok=0\n"
    with caplog.at_level("INFO", logger="psafe.output.serial"):
        out.close()
    assert out.mcu_gate is False
    assert "ESP32 gate at close: N" in caplog.records[-1].getMessage()
