"""통신 방식 비교 도구 (EXP-012). 메시지 형식, 분석 계산, localhost 왕복 (UDP / HTTP)."""

import json
import socket
import threading
import zlib

import pytest

from psafe.link import messages
from psafe.tools import commbench as cb


def free_port(kind=socket.SOCK_DGRAM) -> int:
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---- 메시지 ---------------------------------------------------------------

def test_message_is_real_report_v2_plus_bench_field():
    data = cb.make_message("abcd1234", 5, "udp", t_send_ns=42)
    d = json.loads(data)
    bench = d.pop("bench")
    d.pop("crc")
    assert set(d) == messages._FIELDS["report"]           # 실제 Report와 같은 필드
    assert bench["run"] == "abcd1234" and bench["t_send_ns"] == 42
    i = data.rfind(b',"crc":')
    assert zlib.crc32(data[:i]) == json.loads(data)["crc"]  # 같은 CRC 꼬리 형식
    assert cb.parse_message(data) == ("abcd1234", 5, 42)


@pytest.mark.parametrize("size", [512, 1024, 4096])
def test_message_padding_hits_requested_size(size):
    assert abs(len(cb.make_message("abcd1234", 123, "udp", size=size)) - size) <= 2   # 드물게 CRC 자릿수 때문에 ±2 B


def test_kind():
    assert [cb.kind(p) for p in cb.PROTOS] == ["udp", "http", "http", "mqtt", "mqtt"]


# ---- 분석 -----------------------------------------------------------------

def test_summarize_client_rtt_and_loss():
    ms = 1_000_000
    rows = [{"seq": i, "t_send_ns": i * 200 * ms, "t_echo_ns": i * 200 * ms + (i + 1) * ms, "status": "ok"}
            for i in range(9)]
    rows.append({"seq": 9, "t_send_ns": 1, "t_echo_ns": None, "status": "lost"})
    s = cb.summarize_client(rows)
    assert s["echoed"] == 9 and s["lost"] == 1 and s["loss_pct"] == pytest.approx(10.0)
    assert s["rtt_p50_ms"] == pytest.approx(5.0) and s["rtt_max_ms"] == pytest.approx(9.0)


def test_summarize_server_excess_delay_cancels_clock_offset_and_finds_stale():
    """보내는 쪽 시계가 +1000초 어긋나 있어도 초과 지연은 같다. 복구 뒤 몰려 온 옛날 메시지를 센다."""
    ms = 1_000_000
    offset = 1000 * 1000 * ms
    rows = [{"seq": i, "t_send_ns": i * 200 * ms + offset, "t_recv_ns": i * 200 * ms + 2 * ms,
             "server_boot": "b", "bytes": 300} for i in range(20)]
    # seq 20..44 (5초) 는 끊긴 동안 쌓였다가 복구 시점(9.0 s)에 한꺼번에 도착 (TCP 재전송 모양)
    for i in range(20, 45):
        rows.append({"seq": i, "t_send_ns": i * 200 * ms + offset, "t_recv_ns": 9000 * ms + i * ms,
                     "server_boot": "b", "bytes": 300})
    s = cb.summarize_server(rows, fault_s=5.0)
    assert s["excess_max_ms"] == pytest.approx(9000 + 20 - 4000 - 2, abs=1)
    assert s["stale_count"] > 0
    assert s["max_gap_ms"] == pytest.approx(9020 - 3802, abs=1)
    assert s["recovery_lag_ms"] == pytest.approx(9020 - 3802 - 5000, abs=1)
    assert ("C3" in cb.verdict({}, s, 5.0)[0][0]) and cb.verdict({}, s, 5.0)[0][2] == "FAIL"


def test_verdict_normal_run_pass():
    cli = {"rtt_p99_ms": 3.0}
    srv = {"max_gap_ms": 210.0, "stale_count": 0, "excess_max_ms": 4.0}
    assert [v[2] for v in cb.verdict(cli, srv, None)] == ["PASS", "PASS", "PASS"]


# ---- localhost 왕복 ---------------------------------------------------------

@pytest.mark.parametrize("proto, sock", [("udp", socket.SOCK_DGRAM), ("http", socket.SOCK_STREAM),
                                         ("http-new", socket.SOCK_STREAM)])
def test_localhost_round_trip(tmp_path, proto, sock):
    port = free_port(sock)
    srv_log, cli_log = tmp_path / "srv.csv", tmp_path / "cli.csv"
    srv = cb.Server(proto, str(srv_log), bind=("127.0.0.1", port))
    t = threading.Thread(target=srv.serve, args=(3.0,), daemon=True)
    t.start()
    assert srv.ready.wait(2)
    cl = cb.Client(proto, ("127.0.0.1", port), str(cli_log), rate=50, timeout=0.5, quiet=True)
    summ = cl.run_for(0.5)
    srv.stop()
    assert summ["slots"] == 25 and summ["echoed"] == 25 and summ["lost"] == 0
    res = cb.analyze(str(cli_log), str(srv_log))
    assert res["server"]["received"] == 25 and res["server"]["duplicates"] == 0
    assert res["proto"] == proto


def test_http_client_survives_server_down(tmp_path):
    """서버가 없으면 매 전송이 error로 기록되고 예외로 죽지 않는다 (고장 시험의 전제)."""
    port = free_port(socket.SOCK_STREAM)
    cl = cb.Client("http", ("127.0.0.1", port), str(tmp_path / "c.csv"), rate=20, timeout=0.2, quiet=True)
    summ = cl.run_for(0.3)
    assert summ["echoed"] == 0 and summ["errors"] + summ["skipped"] == summ["slots"]
