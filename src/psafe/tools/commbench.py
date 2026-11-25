"""보드 간 통신 방식 비교 측정 (EXPERIMENTS.md EXP-012). UDP / HTTP / MQTT에 같은 메시지를 보내 비교한다.

    # 받는 쪽 (허브 C 역할). 받은 시각을 기록하고 그대로 되돌려 보낸다(echo)
    python -m psafe.tools.commbench server --proto udp  --bind 0.0.0.0:6000 --log srv_udp.csv
    python -m psafe.tools.commbench server --proto http --bind 0.0.0.0:6001 --log srv_http.csv
    python -m psafe.tools.commbench server --proto mqtt1 --broker 192.168.20.22:1884 --log srv_mqtt1.csv

    # 보내는 쪽 (노드 A 역할). 5 Hz로 60초
    python -m psafe.tools.commbench client --proto udp --server 192.168.20.22:6000 --rate 5 --seconds 60 --log cli_udp.csv

    # 결과 (+ 고장 길이를 알면 복구 지연도 계산)
    python -m psafe.tools.commbench analyze cli_udp.csv srv_udp.csv [--fault-s 5]

방식 (--proto)
    udp        데이터그램 1개 = 메시지 1개 (지금 psafe.link가 쓰는 방식)
    http       HTTP POST, 연결 유지(keep-alive). 실패하면 다음 전송에서 다시 연결
    http-new   HTTP POST, 메시지마다 새 TCP 연결
    mqtt0      MQTT QoS 0 (브로커 경유, 최대 한 번)
    mqtt1      MQTT QoS 1 (브로커 경유, 최소 한 번. 끊긴 동안 보낸 것을 쌓아 뒀다가 다시 보낸다)

시각 (DESIGN E-17: 보드끼리 시계를 비교하지 않는다)
    * 시계는 time.perf_counter_ns (단조 + 고해상도). time.monotonic은 Windows에서 해상도가 15.6 ms라 쓰지 않는다.
    * 왕복 시간 RTT = 보내는 쪽 시계 하나로만 잰다 (보낸 시각 -> echo 받은 시각).
    * 받는 쪽의 '늦게 도착한 정도'는 (받은 시각 - 보낸 시각)에서 그 실행의 최솟값을 뺀 값(초과 지연)으로 본다.
      두 시계의 차이(상수)가 빼기에서 지워진다. 남는 오차는 시계 속도 차이(수십 ppm -> 10분에 수십 ms)뿐이라
      판정 기준(500 ms)에 비해 작다.

메시지: 실제 Report v2(psafe.link.messages)와 같은 필드 + 측정용 "bench" 필드, CRC 꼬리까지 같은 형식.
--size로 패딩을 붙여 크기를 바꾼다 (기본은 패딩 없음, 약 300 B. 드물게 CRC 자릿수 때문에 ±2 B).
"""

from __future__ import annotations

import argparse
import csv
import http.client
import http.server
import json
import logging
import math
import socket
import statistics
import sys
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from psafe.core.packets import State
from psafe.link.messages import Report, new_boot_id

log = logging.getLogger("psafe.commbench")

PROTOS = ("udp", "http", "http-new", "mqtt0", "mqtt1")
REQ_TOPIC, RESP_TOPIC = "psafe/bench/req", "psafe/bench/resp"
CLIENT_FIELDS = ("run", "proto", "seq", "t_send_ns", "t_echo_ns", "status")
SERVER_FIELDS = ("run", "proto", "seq", "t_send_ns", "t_recv_ns", "server_boot", "bytes")

# 측정 전에 정한 판정 기준 (EXPERIMENTS.md EXP-012). 결과를 보고 바꾸지 않는다.
C1_P99_RTT_MS = 50.0          # 평상시 왕복 p99
C2_MAX_GAP_MS = 500.0         # 평상시 받는 쪽 수신 간격 최대 < node_timeout_ms (오탐 없이 0.5초 감지)
C3_STALE_MS = 500.0           # 복구 뒤 이보다 늦게 도착한 메시지 = 옛날 메시지
DEFAULT_TIMEOUT_S = 1.0       # HTTP 요청 / echo 대기 한도


# ---------------------------------------------------------------------------
# 메시지
# ---------------------------------------------------------------------------

def kind(proto: str) -> str:
    """udp / http / mqtt (http-new, mqtt0/1의 계열)"""
    return "udp" if proto == "udp" else "http" if proto.startswith("http") else "mqtt"


def make_message(run: str, seq: int, proto: str, size: int = 0, t_send_ns: int | None = None) -> bytes:
    """Report v2와 같은 모양의 메시지. size > 0이면 그 바이트 수가 되도록 패딩한다."""
    r = Report(node="A", boot=run, seq=seq, state=State.CLEAR, safe=True, count=0, reason="clear",
               evidence_age_ms=120, confirm_id=7, confirm="empty", echo_seq=max(seq - 1, 0), echo_hold_ms=12)
    d = r.to_dict()
    d["bench"] = {"run": run, "proto": proto, "t_send_ns": time.perf_counter_ns() if t_send_ns is None else t_send_ns,
                  "pad": ""}
    data = _with_crc(d)
    if size > len(data):
        # CRC 값의 자릿수(보통 8~10)가 내용에 따라 바뀌어 길이가 1~2 B 어긋날 수 있다.
        # 패딩 길이와 패딩 글자를 바꿔 가며 정확히 맞는 것을 고른다 (글자가 바뀌면 CRC가 바뀐다)
        base = size - len(data)
        for pad in (base, base - 1, base - 2, base + 1, base - 3):
            for ch in "xyzwvutsrqponmlkjihgfedcba":
                d["bench"]["pad"] = ch * max(pad, 0)
                data = _with_crc(d)
                if len(data) == size:
                    return data
    return data


def _with_crc(d: dict) -> bytes:
    """messages.encode_raw와 같은 형식 (crc가 마지막 필드). 512 B 제한 없이 큰 크기도 만든다."""
    head = json.dumps(d, separators=(",", ":"), ensure_ascii=True).encode("ascii")[:-1]
    return head + b',"crc":' + str(zlib.crc32(head)).encode("ascii") + b"}"


def parse_message(data: bytes) -> tuple[str, int, int]:
    """(run, seq, t_send_ns). 받는 쪽이 실제처럼 JSON을 해석하는 비용도 포함된다."""
    d = json.loads(data)
    b = d["bench"]
    return b["run"], d["seq"], b["t_send_ns"]


# ---------------------------------------------------------------------------
# 기록
# ---------------------------------------------------------------------------

class CsvLog:
    """한 줄씩 바로 flush한다. 강제 종료(고장 시험)돼도 그때까지의 기록이 남는다."""

    def __init__(self, path: str | None, fields: tuple[str, ...]):
        self._lock = threading.Lock()
        self._f = None
        if path:
            p = Path(path)
            new = not p.exists() or p.stat().st_size == 0
            self._f = p.open("a", newline="", encoding="utf-8")
            self._w = csv.writer(self._f)
            if new:
                self._w.writerow(fields)
                self._f.flush()

    def row(self, *values) -> None:
        if self._f is None:
            return
        with self._lock:
            self._w.writerow(values)
            self._f.flush()

    def close(self) -> None:
        if self._f:
            self._f.close()


def parse_addr(s: str) -> tuple[str, int]:
    host, _, port = s.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"expected host:port, got {s!r}")
    return host, int(port)


# ---------------------------------------------------------------------------
# 받는 쪽 (server): 받으면 기록하고 같은 바이트를 그대로 돌려준다
# ---------------------------------------------------------------------------

class Server:
    def __init__(self, proto: str, log_path: str | None, bind=("0.0.0.0", 6000), broker=("127.0.0.1", 1884)):
        self.proto, self.bind, self.broker = proto, bind, broker
        self.boot = new_boot_id()
        self.log = CsvLog(log_path, SERVER_FIELDS)
        self.count = 0
        self.ready = threading.Event()
        self._stop = threading.Event()
        self._closers: list = []

    def _record(self, data: bytes) -> None:
        t = time.perf_counter_ns()
        try:
            run, seq, t_send = parse_message(data)
        except (ValueError, KeyError, TypeError):
            return
        self.count += 1
        self.log.row(run, self.proto, seq, t_send, t, self.boot, len(data))

    def serve(self, seconds: float | None = None) -> None:
        getattr(self, "_serve_" + kind(self.proto))()
        try:
            self._stop.wait(seconds)
        except KeyboardInterrupt:
            pass
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        for c in self._closers:
            try:
                c()
            except Exception:
                pass
        self._closers.clear()
        self.log.close()

    def _serve_udp(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if sys.platform == "win32" and hasattr(socket, "SIO_UDP_CONNRESET"):
            s.ioctl(socket.SIO_UDP_CONNRESET, False)
        s.bind(self.bind)
        s.settimeout(0.2)
        self.bind = s.getsockname()

        def loop():
            while not self._stop.is_set():
                try:
                    data, addr = s.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        return
                    continue
                self._record(data)
                try:
                    s.sendto(data, addr)
                except OSError:
                    pass

        threading.Thread(target=loop, daemon=True, name="udp-server").start()
        self._closers.append(s.close)
        self.ready.set()

    def _serve_http(self) -> None:
        srv = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"          # keep-alive 허용

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(n)
                srv._record(data)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        httpd = http.server.ThreadingHTTPServer(self.bind, Handler)
        httpd.daemon_threads = True
        self.bind = httpd.server_address
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True,
                         name="http-server").start()
        self._closers += [httpd.shutdown, httpd.server_close]
        self.ready.set()

    def _serve_mqtt(self) -> None:
        qos = int(self.proto[-1])
        c = _mqtt_client(f"bench-server-{self.boot}")

        def on_connect(client, userdata, flags, rc, props=None):
            client.subscribe(REQ_TOPIC, qos=qos)       # 재연결할 때마다 다시 구독
            self.ready.set()

        def on_message(client, userdata, msg):
            self._record(msg.payload)
            client.publish(RESP_TOPIC, msg.payload, qos=qos)

        c.on_connect, c.on_message = on_connect, on_message
        c.connect_async(*self.broker, keepalive=5)
        c.loop_start()
        self._closers += [c.loop_stop, c.disconnect]


def _mqtt_client(client_id: str):
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        raise SystemExit("paho-mqtt not installed: pip install -e .[commbench]") from None
    if hasattr(mqtt, "CallbackAPIVersion"):                         # paho 2.x
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    else:                                                           # paho 1.x
        c = mqtt.Client(client_id=client_id)
    # 기본값(1~120초 지수 백오프)이면 복구가 느린 게 MQTT 탓이 아니라 설정 탓이 된다. 공정하게 짧게 둔다
    c.reconnect_delay_set(min_delay=0.2, max_delay=2)
    return c


# ---------------------------------------------------------------------------
# 보내는 쪽 (client): 정해진 주기로 보내고, echo로 왕복 시간을 잰다
# ---------------------------------------------------------------------------

class Client:
    def __init__(self, proto: str, server=("127.0.0.1", 6000), log_path: str | None = None, rate: float = 5.0,
                 size: int = 0, timeout: float = DEFAULT_TIMEOUT_S, run: str | None = None, quiet: bool = False):
        self.proto, self.server, self.rate, self.size, self.timeout = proto, server, rate, size, timeout
        self.run = run or new_boot_id()
        self.log = CsvLog(log_path, CLIENT_FIELDS)
        self.quiet = quiet
        self.sent: dict[int, int] = {}          # seq -> t_send_ns
        self.echo: dict[int, int] = {}          # seq -> t_echo_ns
        self.status: dict[int, str] = {}        # seq -> ok / error:... / skipped
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._closers: list = []

    # -- 방식별 준비와 전송 ---------------------------------------------------

    def _open(self) -> None:
        k = kind(self.proto)
        if k == "udp":
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if sys.platform == "win32" and hasattr(socket, "SIO_UDP_CONNRESET"):
                s.ioctl(socket.SIO_UDP_CONNRESET, False)
            s.settimeout(0.2)
            self._sock = s
            threading.Thread(target=self._udp_recv, daemon=True, name="udp-recv").start()
            self._closers.append(s.close)
        elif k == "http":
            self._conn: http.client.HTTPConnection | None = None
        elif k == "mqtt":
            qos = int(self.proto[-1])
            c = _mqtt_client(f"bench-client-{self.run}")
            connected = threading.Event()

            def on_connect(client, userdata, flags, rc, props=None):
                client.subscribe(RESP_TOPIC, qos=qos)
                connected.set()

            def on_message(client, userdata, msg):
                self._got_echo(msg.payload)

            c.on_connect, c.on_message = on_connect, on_message
            c.connect_async(*self.server, keepalive=5)
            c.loop_start()
            if not connected.wait(5):
                raise SystemExit(f"MQTT broker {self.server} not reachable")
            self._mqtt, self._qos = c, qos
            self._closers += [c.loop_stop, c.disconnect]

    def _send(self, seq: int, data: bytes) -> None:
        k = kind(self.proto)
        if k == "udp":
            try:
                self._sock.sendto(data, self.server)
            except OSError as e:
                self.status[seq] = f"error:{type(e).__name__}"
        elif k == "mqtt":
            info = self._mqtt.publish(REQ_TOPIC, data, qos=self._qos)
            if info.rc != 0:
                self.status[seq] = f"error:mqtt_rc{info.rc}"
        else:
            self._http_post(seq, data)

    def _http_post(self, seq: int, data: bytes) -> None:
        """보내고 응답까지 기다린다 (요청-응답이 HTTP의 본래 모양). 실패하면 연결을 버리고 다음에 새로 연다."""
        try:
            if self._conn is None or self.proto == "http-new":
                self._conn = http.client.HTTPConnection(*self.server, timeout=self.timeout)
            self._conn.request("POST", "/report", body=data, headers={"Content-Type": "application/json"})
            resp = self._conn.getresponse()
            body = resp.read()
            if self.proto == "http-new":
                self._conn.close()
                self._conn = None
            self._got_echo(body)
        except (OSError, http.client.HTTPException) as e:
            self.status[seq] = f"error:{type(e).__name__}"
            if self._conn is not None:
                self._conn.close()
            self._conn = None

    def _udp_recv(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            self._got_echo(data)

    def _got_echo(self, data: bytes) -> None:
        t = time.perf_counter_ns()
        try:
            run, seq, _ = parse_message(data)
        except (ValueError, KeyError, TypeError):
            return
        if run != self.run:
            return
        with self._lock:
            self.echo.setdefault(seq, t)        # 중복 echo(QoS 1 재전송)는 첫 번째만

    # -- 실행 -----------------------------------------------------------------

    def run_for(self, seconds: float, cue: tuple[float, float] | None = None) -> dict:
        """seconds 동안 rate Hz로 보낸다. 늦어지면(HTTP가 막히면) 놓친 칸은 건너뛰고 skipped로 센다.
        cue=(시작초, 길이초): 고장을 손으로 넣을 시점을 화면에 알려 준다 (랜선 뽑기 등)."""
        self._open()
        period = 1.0 / self.rate
        n_total = int(seconds * self.rate)
        start = time.monotonic()
        next_report, cue_state = start + 1.0, 0
        seq = 0
        try:
            while seq < n_total:
                due = start + seq * period
                now = time.monotonic()
                if now < due:
                    time.sleep(due - now)
                elif now - due > period:            # 한 칸 넘게 밀렸다 -> 그 칸은 못 보냄
                    self.status[seq] = "skipped"
                    seq += 1
                    continue
                t_send = time.perf_counter_ns()
                self.sent[seq] = t_send
                self._send(seq, make_message(self.run, seq, self.proto, self.size, t_send))
                seq += 1
                now = time.monotonic()
                if cue and cue_state == 0 and now - start >= cue[0]:
                    print(f"\n>>> FAULT NOW ({now - start:.1f} s): inject the fault for {cue[1]:.0f} s", flush=True)
                    cue_state = 1
                if cue and cue_state == 1 and now - start >= cue[0] + cue[1]:
                    print(f"\n>>> RESTORE NOW ({now - start:.1f} s)", flush=True)
                    cue_state = 2
                if not self.quiet and now >= next_report:
                    with self._lock:
                        got = len(self.echo)
                    print(f"\r{now - start:6.1f} s  sent {len(self.sent):6d}  echo {got:6d}", end="", flush=True)
                    next_report += 1.0
            time.sleep(self.timeout + 1.0)          # 마지막 echo를 기다린다 (QoS 1 재전송 포함)
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            for c in self._closers:
                try:
                    c()
                except Exception:
                    pass
        if not self.quiet:
            print()
        for s in range(seq):
            t_send = self.sent.get(s)
            t_echo = self.echo.get(s)
            st = self.status.get(s) or ("ok" if t_echo is not None else "lost")
            self.log.row(self.run, self.proto, s, t_send or "", t_echo or "", st)
        self.log.close()
        return summarize_client([
            {"seq": s, "t_send_ns": self.sent.get(s), "t_echo_ns": self.echo.get(s),
             "status": self.status.get(s) or ("ok" if s in self.echo else "lost")} for s in range(seq)])


# ---------------------------------------------------------------------------
# 분석
# ---------------------------------------------------------------------------

def _pct(xs: list[float], p: float) -> float:
    """최근접 순위 백분위수 (표본이 적어도 실제 측정값 하나를 돌려준다)."""
    if not xs:
        return math.nan
    s = sorted(xs)
    k = max(0, math.ceil(p / 100 * len(s)) - 1)
    return s[k]


def summarize_client(rows: list[dict]) -> dict:
    rtt = [(r["t_echo_ns"] - r["t_send_ns"]) / 1e6 for r in rows
           if r.get("t_echo_ns") is not None and r.get("t_send_ns") is not None]
    n = len(rows)
    status = [r["status"] for r in rows]
    return {
        "sent": sum(1 for r in rows if r.get("t_send_ns") is not None),
        "slots": n,
        "echoed": len(rtt),
        "lost": status.count("lost"),
        "errors": sum(1 for s in status if s.startswith("error")),
        "skipped": status.count("skipped"),
        "loss_pct": 100.0 * (n - len(rtt)) / n if n else math.nan,
        "rtt_p50_ms": _pct(rtt, 50), "rtt_p95_ms": _pct(rtt, 95), "rtt_p99_ms": _pct(rtt, 99),
        "rtt_max_ms": max(rtt) if rtt else math.nan,
        "jitter_ms": statistics.pstdev(rtt) if len(rtt) > 1 else math.nan,
    }


def summarize_server(rows: list[dict], fault_s: float | None = None) -> dict:
    """받는 쪽 기록만으로 (자기 시계): 수신 간격, 초과 지연(=옛날 메시지), 중복."""
    if not rows:
        return {"received": 0}
    rows = sorted(rows, key=lambda r: r["t_recv_ns"])
    recv = [r["t_recv_ns"] for r in rows]
    gaps = [(b - a) / 1e6 for a, b in zip(recv, recv[1:])]
    delta = [(r["t_recv_ns"] - r["t_send_ns"]) / 1e6 for r in rows]
    base = min(delta)
    excess = [d - base for d in delta]
    seqs = [r["seq"] for r in rows]
    max_gap = max(gaps) if gaps else 0.0
    out = {
        "received": len(rows),
        "unique": len(set(seqs)),
        "duplicates": len(seqs) - len(set(seqs)),
        "max_gap_ms": max_gap,
        "gap_p99_ms": _pct(gaps, 99),
        "excess_p99_ms": _pct(excess, 99),
        "excess_max_ms": max(excess),
        "stale_count": sum(1 for e in excess if e > C3_STALE_MS),
        "bytes": rows[0].get("bytes"),
        "server_boots": len({r.get("server_boot") for r in rows}),
    }
    if fault_s is not None:
        out["recovery_lag_ms"] = max(0.0, max_gap - fault_s * 1000)   # 고장이 풀린 뒤 첫 수신까지 추가로 걸린 시간
    return out


def verdict(cli: dict, srv: dict, fault_s: float | None) -> list[tuple[str, str, str]]:
    """(기준, 값, 판정). 고장 실행이면 C1·C2는 해당 없음, 평상시 실행이면 C3는 '평상시 0이어야' 정도로만 본다."""
    out = []
    if fault_s is None:
        out.append(("C1 p99 RTT <= %.0f ms" % C1_P99_RTT_MS, f"{cli['rtt_p99_ms']:.1f} ms",
                    "PASS" if cli["rtt_p99_ms"] <= C1_P99_RTT_MS else "FAIL"))
        out.append(("C2 max receive gap < %.0f ms" % C2_MAX_GAP_MS, f"{srv.get('max_gap_ms', math.nan):.1f} ms",
                    "PASS" if srv.get("max_gap_ms", math.inf) < C2_MAX_GAP_MS else "FAIL"))
    out.append(("C3 stale (> %.0f ms late) == 0" % C3_STALE_MS,
                f"{srv.get('stale_count', 0)} (max {srv.get('excess_max_ms', math.nan):.0f} ms)",
                "PASS" if srv.get("stale_count", 0) == 0 else "FAIL"))
    return out


def _read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("seq", "t_send_ns", "t_echo_ns", "t_recv_ns", "bytes"):
            if k in r:
                r[k] = int(r[k]) if r[k] not in ("", None) else None
    return rows


def analyze(client_csv: str, server_csv: str | None, fault_s: float | None = None, run: str | None = None) -> dict:
    cli_rows = _read_csv(client_csv)
    run = run or (cli_rows[-1]["run"] if cli_rows else None)       # 기본: 파일의 마지막 실행
    cli_rows = [r for r in cli_rows if r["run"] == run]
    srv_rows = [r for r in _read_csv(server_csv) if r["run"] == run] if server_csv else []
    cli, srv = summarize_client(cli_rows), summarize_server(srv_rows, fault_s)
    proto = cli_rows[0]["proto"] if cli_rows else "?"
    return {"run": run, "proto": proto, "client": cli, "server": srv, "verdict": verdict(cli, srv, fault_s)}


def print_report(res: dict) -> None:
    c, s = res["client"], res["server"]
    print(f"run {res['run']}  proto {res['proto']}")
    print(f"  client: slots {c['slots']}  echoed {c['echoed']}  lost {c['lost']}  errors {c['errors']}  "
          f"skipped {c['skipped']}  loss {c['loss_pct']:.2f}%")
    print(f"          RTT p50 {c['rtt_p50_ms']:.2f}  p95 {c['rtt_p95_ms']:.2f}  p99 {c['rtt_p99_ms']:.2f}  "
          f"max {c['rtt_max_ms']:.2f}  jitter(sd) {c['jitter_ms']:.2f} ms")
    if s.get("received"):
        line = (f"  server: received {s['received']}  dup {s['duplicates']}  max gap {s['max_gap_ms']:.0f} ms  "
                f"excess p99 {s['excess_p99_ms']:.1f} / max {s['excess_max_ms']:.0f} ms  stale {s['stale_count']}  "
                f"{s['bytes']} B  server boots {s['server_boots']}")
        if "recovery_lag_ms" in s:
            line += f"  recovery lag {s['recovery_lag_ms']:.0f} ms"
        print(line)
    for name, val, ok in res["verdict"]:
        print(f"  [{ok}] {name}: {val}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m psafe.tools.commbench")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("server", help="받는 쪽 (허브 C 역할)")
    s.add_argument("--proto", choices=PROTOS, required=True)
    s.add_argument("--bind", type=parse_addr, default=("0.0.0.0", 6000), help="udp/http")
    s.add_argument("--broker", type=parse_addr, default=("127.0.0.1", 1884), help="mqtt")
    s.add_argument("--log", help="수신 기록 CSV (이어 쓴다)")
    s.add_argument("--seconds", type=float, help="기본: Ctrl+C까지")

    c = sub.add_parser("client", help="보내는 쪽 (노드 A 역할)")
    c.add_argument("--proto", choices=PROTOS, required=True)
    c.add_argument("--server", type=parse_addr, required=True, help="udp/http: 서버, mqtt: 브로커")
    c.add_argument("--rate", type=float, default=5.0, help="Hz (실제 Report 주기 = 5)")
    c.add_argument("--seconds", type=float, default=60.0)
    c.add_argument("--size", type=int, default=0, help="메시지 바이트 수 (0 = 패딩 없음)")
    c.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    c.add_argument("--log", help="송신 기록 CSV (이어 쓴다)")
    c.add_argument("--cue", type=float, nargs=2, metavar=("AT_S", "LEN_S"),
                   help="고장을 손으로 넣을 시점 안내 (예: --cue 20 5)")

    a = sub.add_parser("analyze", help="기록 분석과 판정")
    a.add_argument("client_csv")
    a.add_argument("server_csv", nargs="?")
    a.add_argument("--fault-s", type=float, help="고장 실행이면 고장 길이(초)")
    a.add_argument("--run", help="기본: 파일의 마지막 실행")
    a.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "server":
        srv = Server(args.proto, args.log, bind=args.bind, broker=args.broker)
        print(f"commbench server {args.proto} boot {srv.boot} (Ctrl+C to stop)", flush=True)
        srv.serve(args.seconds)
        print(f"received {srv.count}")
        return 0
    if args.cmd == "client":
        cl = Client(args.proto, args.server, args.log, args.rate, args.size, args.timeout)
        print(f"commbench client {args.proto} -> {args.server[0]}:{args.server[1]} run {cl.run} "
              f"({args.rate} Hz x {args.seconds} s)", flush=True)
        summ = cl.run_for(args.seconds, tuple(args.cue) if args.cue else None)
        print_report({"run": cl.run, "proto": args.proto, "client": summ, "server": {},
                      "verdict": []})
        return 0
    res = analyze(args.client_csv, args.server_csv, args.fault_s, args.run)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print_report(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
