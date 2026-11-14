"""Runners drive the components (DESIGN.md §3.4).

SyncRunner: single loop, deterministic with FakeClock -> scenario tests.
ThreadedRunner (capture / worker / heartbeat threads) arrives with M3+.
"""

import logging
from contextlib import ExitStack
from dataclasses import dataclass, field

from psafe.app import Components
from psafe.core.packets import Decision

log = logging.getLogger("psafe.runner")


@dataclass
class RunResult:
    start: float = 0.0                 # clock time when the source opened
    decisions: list[Decision] = field(default_factory=list)
    frames: int = 0
    timeouts: int = 0                  # waits that ended without a frame
    skipped: int = 0                   # frames the source dropped because we were slow
    infer_ms_total: float = 0.0

    @property
    def avg_infer_ms(self) -> float:
        return self.infer_ms_total / self.frames if self.frames else 0.0


class SyncRunner:
    """source.read -> detector.predict -> fsm.step -> outputs.send, one frame at a time.

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
            # Open in pipeline order; ExitStack closes in reverse even on errors.
            for part in (c.source, c.detector, *c.outputs):
                stack.callback(part.close)
            c.source.open()
            c.detector.load()
            c.detector.warmup()
            for o in c.outputs:
                o.open()
            result.start = last_t = c.clock.now()

            while self.max_frames is None or result.frames < self.max_frames:
                pkt = c.source.read()
                if pkt is None:
                    break
                # Gap since the last frame/decision -> the worker would have timed out.
                while pkt.timestamp - last_t > self.frame_timeout_s:
                    last_t += self.frame_timeout_s
                    self._emit(c.fsm.on_no_frame(last_t), result)
                    result.timeouts += 1

                dets = c.detector.predict(pkt)
                last_t = c.clock.now()
                self._emit(c.fsm.step(dets, last_t), result)
                result.frames += 1
                result.infer_ms_total += dets.infer_ms

        result.skipped = getattr(c.source, "skipped", 0)
        log.info(
            "sync run done: %d frames, %d skipped, %d timeouts, avg infer %.2f ms",
            result.frames, result.skipped, result.timeouts, result.avg_infer_ms,
        )
        return result

    def _emit(self, decision: Decision, result: RunResult) -> None:
        for o in self.c.outputs:
            o.send(decision)
        result.decisions.append(decision)
