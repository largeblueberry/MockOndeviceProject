"""Runners drive the components (DESIGN.md §3.1, §3.4).

SyncRunner      single loop; with FakeClock it is deterministic -> scenario tests.
ThreadedRunner  capture / worker / heartbeat threads + supervisor -> real runs.

Both use process_frame(), so the decision logic is identical; only the way
frames are scheduled differs.
"""

import logging
import threading
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Callable

from psafe.app import Components
from psafe.core.clock import FakeClock
from psafe.core.packets import Decision, Detections, FramePacket, State, unsafe
from psafe.core.slot import LatestSlot

log = logging.getLogger("psafe.runner")


def process_frame(c: Components, pkt: FramePacket) -> tuple[Decision, Detections | None]:
    """health check -> detector -> FSM for one frame. Shared by all runners.

    A detector exception must never crash the loop or leave the last Y
    standing: it becomes a FAULT decision.
    """
    fault = c.health.check(pkt)
    try:
        dets = c.detector.predict(pkt)
    except Exception:
        log.exception("detector failed on seq %d", pkt.seq)
        return c.fsm.fault("detector_error", c.clock.now()), None
    return c.fsm.step(dets, c.clock.now(), fault), dets


def notify_frame(c: Components, pkt: FramePacket, dets: Detections | None, decision: Decision) -> None:
    for o in c.outputs:
        try:
            o.on_frame(pkt, dets, decision)
        except Exception:
            log.exception("output %s.on_frame failed", type(o).__name__)


def poll_outputs(c: Components) -> bool:
    """Main-thread GUI hook. False if any output asked to stop."""
    keep = True
    for o in c.outputs:
        try:
            keep = o.poll() and keep
        except Exception:
            log.exception("output %s.poll failed", type(o).__name__)
    return keep


def open_all(c: Components, stack: ExitStack) -> float:
    """Open outputs -> detector -> source (camera LAST, so model loading time
    doesn't make the first frames stale). Each close is registered as soon as
    its part is opened; ExitStack closes in reverse order, even on errors.
    Returns the clock time the source was opened (= scenario t=0)."""
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
    start: float = 0.0                 # clock time when the source opened
    decisions: list[Decision] = field(default_factory=list)
    frames: int = 0                    # frames processed by the model
    timeouts: int = 0                  # waits that ended without a frame
    skipped: int = 0                   # frames the source dropped because we were slow
    dropped: int = 0                   # frames overwritten in the slot before the worker took them
    heartbeats: int = 0
    stale_heartbeats: int = 0          # heartbeats that had to send N because the decision was too old
    infer_ms_total: float = 0.0
    error: str | None = None           # set if the run stopped because something broke

    @property
    def avg_infer_ms(self) -> float:
        return self.infer_ms_total / self.frames if self.frames else 0.0


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

class SyncRunner:
    """source.read -> process_frame -> outputs.send, one frame at a time.

    Also reproduces what the threaded worker does when frames stop coming:
    every `frame timeout` without a frame it calls fsm.on_no_frame(). That is
    replayed after the fact from timestamps, so outages are visible in the
    decision log at the right times.
    """

    def __init__(self, c: Components, max_frames: int | None = None):
        self.c = c
        self.max_frames = max_frames
        self.frame_timeout_s = c.cfg.fsm.max_frame_age_ms / 1000.0

    def run(self) -> RunResult:
        c, result = self.c, RunResult()
        with ExitStack() as stack:
            result.start = last_t = open_all(c, stack)

            while self.max_frames is None or result.frames < self.max_frames:
                pkt = c.source.read()
                if pkt is None:
                    break
                # Gap since the last frame/decision -> the worker would have timed out.
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
    """Latest decision, written by the worker, read by the heartbeat.
    (Not a LatestSlot: fault decisions have seq -1, and the heartbeat must
    re-read the same decision every period.)"""

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
    T1 capture    source.read() as fast as the camera gives frames -> frame_slot (latest only)
    T2 worker     frame_slot.get(timeout) -> process_frame -> board   (owns the FSM, no lock needed)
                  timeout -> fsm.on_no_frame()
    T3 heartbeat  every heartbeat_ms: board.latest() -> outputs
                  decision older than max_decision_age_ms -> N  (catches a hung worker)
    main          supervisor: metrics, thread liveness, Ctrl+C, end of stream

    Any thread dying stops the run with result.error set; the last thing sent
    to outputs is always an N ("shutdown"). In production a process manager
    (systemd) restarts us, and the ESP32 watchdog covers the gap.
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
        self.max_decision_age_s = c.cfg.health.max_decision_age_ms / 1000.0
        self.frame_timeout_s = c.cfg.fsm.max_frame_age_ms / 1000.0

        self.frame_slot: LatestSlot[FramePacket] = LatestSlot()
        self.board = DecisionBoard()
        self.result = RunResult()
        self._stop = threading.Event()
        self._source_done = threading.Event()
        self._crashed: dict[str, str] = {}

    # ---- lifecycle ---------------------------------------------------------

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
        """Ask the run to end (from another thread, e.g. a test or a signal handler)."""
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
        last = (0, 0)  # (frames captured, frames processed) at the previous metrics line
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

    # ---- threads -----------------------------------------------------------

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
                self.board.publish(decision)        # publish first: drawing must not delay the heartbeat
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
            if delay < 0:          # fell behind (e.g. slow output): don't burst to catch up
                next_t, delay = c.clock.now(), 0.0
            self._stop.wait(delay)

    def _send_all(self, d: Decision) -> None:
        for o in self.c.outputs:
            try:
                o.send(d)
            except Exception:
                # One broken output must not silence the others. If the serial
                # link is the broken one, the ESP32 watchdog turns it into N.
                log.exception("output %s failed", type(o).__name__)
