"""Runner는 부품을 구동한다 (DESIGN.md §3.1, §3.4).

SyncRunner      단일 루프. FakeClock과 함께 쓰면 결과가 결정적이다 -> 시나리오 테스트.
ThreadedRunner  capture / worker / heartbeat 스레드 + 감독 -> 실제 운용.

둘 다 process_frame()을 쓰므로 판단 로직은 같고, 프레임을 처리하는 방식만 다르다.
"""

import logging
import threading
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Callable

from psafe.app import Components
from psafe.core.clock import FakeClock
from psafe.core.packets import Decision, FrameDetections, FramePacket, State, unsafe
from psafe.core.slot import LatestSlot

log = logging.getLogger("psafe.runner")


def process_frame(c: Components, pkt: FramePacket) -> tuple[Decision, FrameDetections | None]:
    """프레임 1장에 대해 health 검사 -> 검출기 -> FSM. 모든 Runner가 공유한다.

    검출기 예외가 루프를 죽이거나 마지막 Y를 그대로 남겨 두어서는 안 된다.
    예외는 FAULT 판단으로 바뀐다.
    """
    fault = c.health.check(pkt)
    try:
        dets = c.detector.predict(pkt)
    except Exception:
        log.exception("detector failed on seq %d", pkt.seq)
        return c.fsm.fault("detector_error", c.clock.now()), None
    return c.fsm.step(dets, c.clock.now(), fault), dets


def notify_frame(c: Components, pkt: FramePacket, dets: FrameDetections | None, decision: Decision) -> None:
    for o in c.outputs:
        try:
            o.on_frame(pkt, dets, decision)
        except Exception:
            log.exception("output %s.on_frame failed", type(o).__name__)


def poll_outputs(c: Components) -> bool:
    """메인 스레드 GUI 훅. 어떤 Output이든 종료를 요청하면 False."""
    keep = True
    for o in c.outputs:
        try:
            keep = o.poll() and keep
        except Exception:
            log.exception("output %s.poll failed", type(o).__name__)
    return keep


def open_all(c: Components, stack: ExitStack) -> float:
    """Output -> 검출기 -> Source 순으로 연다 (카메라를 마지막에 열어서 모델
    로딩 시간 때문에 첫 프레임이 오래된 프레임이 되지 않게 한다). 각 부품은
    열자마자 close를 등록하며, ExitStack은 오류가 나도 역순으로 닫는다.
    Source를 연 시각(= 시나리오 t=0)을 반환한다."""
    for o in c.outputs:
        o.open()
        stack.callback(o.close)
    c.detector.load()
    stack.callback(c.detector.close)
    c.detector.warmup()
    start = c.clock.now()
    c.source.open()
    stack.callback(c.source.close)
    return start


@dataclass
class RunResult:
    start: float = 0.0                 # Source를 연 시각
    decisions: list[Decision] = field(default_factory=list)
    frames: int = 0                    # 모델이 처리한 프레임 수
    timeouts: int = 0                  # 프레임 없이 끝난 대기 횟수
    skipped: int = 0                   # 처리가 느려서 Source가 건너뛴 프레임 수
    dropped: int = 0                   # worker가 가져가기 전에 슬롯에서 덮어쓰인 프레임 수
    heartbeats: int = 0
    stale_heartbeats: int = 0          # 판단이 너무 오래되어 N을 보낸 하트비트 수
    infer_ms_total: float = 0.0
    error: str | None = None           # 무언가 고장 나서 실행이 멈췄으면 설정됨

    @property
    def avg_infer_ms(self) -> float:
        return self.infer_ms_total / self.frames if self.frames else 0.0


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

class SyncRunner:
    """source.read -> process_frame -> outputs.send 를 한 프레임씩 수행한다.

    프레임이 끊겼을 때 threaded worker가 하는 동작도 재현한다: 프레임 없이
    `frame timeout`이 지날 때마다 fsm.on_no_frame()을 호출한다. 이는 시각
    정보로 사후에 재생되므로, 끊김이 판단 로그의 올바른 시점에 나타난다.
    """

    def __init__(self, c: Components, max_frames: int | None = None):
        self.c = c
        self.max_frames = max_frames
        # 프레임 대기 한도 = 프레임 유효 기간 (core/config.py FsmConfig.max_frame_age_ms 참고)
        self.frame_timeout_s = c.cfg.fsm.max_frame_age_ms / 1000.0

    def run(self) -> RunResult:
        c, result = self.c, RunResult()
        with ExitStack() as stack:
            result.start = last_t = open_all(c, stack)

            while self.max_frames is None or result.frames < self.max_frames:
                pkt = c.source.read()
                if pkt is None:
                    break
                # 직전 프레임/판단 이후 간격이 크면 -> worker라면 시간 초과가 났을 것.
                while pkt.timestamp - last_t > self.frame_timeout_s:
                    last_t += self.frame_timeout_s
                    self._emit(c.fsm.on_no_frame(last_t), result)
                    result.timeouts += 1

                decision, dets = process_frame(c, pkt)
                last_t = decision.decided_at
                self._emit(decision, result)
                notify_frame(c, pkt, dets, decision)
                result.frames += 1
                result.infer_ms_total += dets.infer_ms if dets else 0.0
                if not poll_outputs(c):
                    log.info("stop requested by output")
                    break

            self._emit(unsafe(State.FAULT, "shutdown", c.clock.now()), result, record=False)

        result.skipped = getattr(c.source, "skipped", 0)
        log.info(
            "sync run done: %d frames, %d skipped, %d timeouts, avg infer %.2f ms",
            result.frames, result.skipped, result.timeouts, result.avg_infer_ms,
        )
        return result

    def _emit(self, decision: Decision, result: RunResult, record: bool = True) -> None:
        for o in self.c.outputs:
            o.send(decision)
        if record:
            result.decisions.append(decision)


# ---------------------------------------------------------------------------
# Threaded
# ---------------------------------------------------------------------------

class DecisionBoard:
    """최신 판단. worker가 쓰고 heartbeat가 읽는다.
    (LatestSlot이 아닌 이유: FAULT 판단은 seq가 -1이고, heartbeat는 같은
    판단을 매 주기 다시 읽어야 한다.)"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._d: Decision | None = None

    def publish(self, d: Decision) -> None:
        with self._lock:
            self._d = d

    def latest(self) -> Decision | None:
        with self._lock:
            return self._d


class ThreadedRunner:
    """
    T1 capture    카메라가 주는 속도대로 source.read() -> frame_slot (최신 1장만)
    T2 worker     frame_slot.get(timeout) -> process_frame -> board   (FSM을 단독 소유, 락 불필요)
                  시간 초과 -> fsm.on_no_frame()
    T3 heartbeat  heartbeat_ms마다 board.latest() -> outputs
                  판단이 max_decision_age_ms보다 오래되면 -> N  (멈춘 worker를 잡아냄)
    main          감독: 지표 기록, 스레드 생존 확인, Ctrl+C, 스트림 종료

    어떤 스레드든 죽으면 result.error를 설정하고 실행을 멈춘다. Output에 마지막으로
    보내는 신호는 항상 N("shutdown")이다. 실제 운용에서는 프로세스 관리자
    (systemd)가 재시작하고, 그 사이는 ESP32 워치독이 맡는다.
    """

    JOIN_TIMEOUT_S = 2.0

    def __init__(self, c: Components, record: bool = False, max_seconds: float | None = None):
        if isinstance(c.clock, FakeClock):
            raise ValueError("ThreadedRunner needs a real clock (FakeClock only moves when slept on)")
        self.c = c
        self.record = record
        self.max_seconds = max_seconds
        rc = c.cfg.runner
        self.period_s = rc.heartbeat_ms / 1000.0
        self.metrics_interval_s = rc.metrics_interval_s
        self.max_decision_age_s = rc.max_decision_age_ms / 1000.0
        # 프레임 대기 한도 = 프레임 유효 기간 (core/config.py FsmConfig.max_frame_age_ms 참고)
        self.frame_timeout_s = c.cfg.fsm.max_frame_age_ms / 1000.0

        self.frame_slot: LatestSlot[FramePacket] = LatestSlot()
        self.board = DecisionBoard()
        self.result = RunResult()
        self._stop = threading.Event()
        self._source_done = threading.Event()
        self._crashed: dict[str, str] = {}

    # ---- 생명주기 ---------------------------------------------------------

    def run(self) -> RunResult:
        c, r = self.c, self.result
        with ExitStack() as stack:
            r.start = open_all(c, stack)
            threads = [
                threading.Thread(target=self._guard("capture", self._capture_loop), name="capture", daemon=True),
                threading.Thread(target=self._guard("worker", self._worker_loop), name="worker", daemon=True),
                threading.Thread(target=self._guard("heartbeat", self._heartbeat_loop), name="heartbeat", daemon=True),
            ]
            for t in threads:
                t.start()
            try:
                self._supervise(threads)
            except KeyboardInterrupt:
                log.info("interrupted")
            finally:
                self._stop.set()
                self.frame_slot.close()
                for t in threads:
                    t.join(self.JOIN_TIMEOUT_S)
                    if t.is_alive():
                        log.warning("thread %s did not stop (blocked in a call?); abandoning it", t.name)
                self._send_all(unsafe(State.FAULT, "shutdown", c.clock.now()))

        r.skipped = getattr(c.source, "skipped", 0)
        r.dropped = self.frame_slot.dropped
        log.info(
            "threaded run done: %d frames, %d dropped, %d timeouts, %d heartbeats (%d stale), avg infer %.1f ms%s",
            r.frames, r.dropped, r.timeouts, r.heartbeats, r.stale_heartbeats, r.avg_infer_ms,
            f", error: {r.error}" if r.error else "",
        )
        return r

    def stop(self) -> None:
        """실행 종료를 요청한다 (다른 스레드에서, 예: 테스트나 시그널 핸들러)."""
        self._stop.set()

    def _guard(self, name: str, fn: Callable[[], None]) -> Callable[[], None]:
        def wrapped() -> None:
            try:
                fn()
            except Exception as e:
                log.exception("%s thread crashed", name)
                self._crashed[name] = f"{type(e).__name__}: {e}"
        return wrapped

    def _supervise(self, threads: list[threading.Thread]) -> None:
        c, r = self.c, self.result
        next_metrics = c.clock.now() + self.metrics_interval_s
        last = (0, 0)  # 직전 지표 기록 시점의 (캡처한 프레임 수, 처리한 프레임 수)
        while not self._stop.is_set():
            if self._crashed:
                r.error = "; ".join(f"{k}: {v}" for k, v in self._crashed.items())
                log.error("stopping: %s", r.error)
                return
            if self._source_done.is_set():
                log.info("source ended")
                return
            dead = [t.name for t in threads if not t.is_alive()]
            if dead:
                r.error = f"thread(s) exited unexpectedly: {dead}"
                log.error("stopping: %s", r.error)
                return
            now = c.clock.now()
            if self.max_seconds is not None and now - r.start >= self.max_seconds:
                return
            if now >= next_metrics:
                last = self._log_metrics(last, now - next_metrics + self.metrics_interval_s)
                next_metrics = now + self.metrics_interval_s
            if not poll_outputs(c):
                log.info("stop requested by output")
                return
            self._stop.wait(0.03)

    def _log_metrics(self, last: tuple[int, int], interval: float) -> tuple[int, int]:
        cap, proc = self.frame_slot.put_count, self.result.frames
        d = self.board.latest()
        rg = self.c.health.take_ranges()
        fmt = lambda k, p: f"{rg[k][0]:.{p}f}..{rg[k][1]:.{p}f}" if k in rg else "-"  # noqa: E731
        log.info(
            "fps capture %.1f / model %.1f | dropped %d | avg infer %.1f ms | "
            "bright %s contrast %s diff %s | %s %s",
            (cap - last[0]) / interval, (proc - last[1]) / interval, self.frame_slot.dropped,
            self.result.avg_infer_ms, fmt("brightness", 0), fmt("contrast", 1), fmt("diff", 2),
            d.yn if d else "-", d.state.value if d else "-",
        )
        return cap, proc

    # ---- 스레드 -----------------------------------------------------------

    def _capture_loop(self) -> None:
        while not self._stop.is_set():
            pkt = self.c.source.read()
            if pkt is None:
                self._source_done.set()
                return
            self.frame_slot.put(pkt)

    def _worker_loop(self) -> None:
        c, r = self.c, self.result
        while not self._stop.is_set():
            pkt = self.frame_slot.get(timeout=self.frame_timeout_s)
            if self._stop.is_set():
                return
            if pkt is None:
                decision = c.fsm.on_no_frame(c.clock.now())
                self.board.publish(decision)
                r.timeouts += 1
            else:
                decision, dets = process_frame(c, pkt)
                self.board.publish(decision)        # 먼저 게시: 화면 그리기가 하트비트를 늦추면 안 된다
                r.frames += 1
                r.infer_ms_total += dets.infer_ms if dets else 0.0
                notify_frame(c, pkt, dets, decision)
            if self.record:
                r.decisions.append(decision)

    def _heartbeat_loop(self) -> None:
        c, r = self.c, self.result
        next_t = c.clock.now()
        while not self._stop.is_set():
            now = c.clock.now()
            d = self.board.latest()
            if d is None:
                d = unsafe(State.INIT, "no_decision_yet", now)
            elif now - d.decided_at > self.max_decision_age_s:
                d = unsafe(State.FAULT, "decision_stale", now)
                r.stale_heartbeats += 1
            self._send_all(d)
            r.heartbeats += 1

            next_t += self.period_s
            delay = next_t - c.clock.now()
            if delay < 0:          # 밀렸으면 (예: 느린 Output) 따라잡으려고 몰아서 보내지 않는다
                next_t, delay = c.clock.now(), 0.0
            self._stop.wait(delay)

    def _send_all(self, d: Decision) -> None:
        for o in self.c.outputs:
            try:
                o.send(d)
            except Exception:
                # 고장 난 Output 하나 때문에 나머지가 멈추면 안 된다. 고장 난 것이
                # 시리얼 링크라면 ESP32 워치독이 N으로 처리한다.
                log.exception("output %s failed", type(o).__name__)
