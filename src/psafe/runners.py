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
    decisions: list[Decision] = field(default_factory=list)
    frames: int = 0
    infer_ms_total: float = 0.0

    @property
    def avg_infer_ms(self) -> float:
        return self.infer_ms_total / self.frames if self.frames else 0.0


class SyncRunner:
    """source.read -> detector.predict -> fsm.step -> outputs.send, one frame at a time."""

    def __init__(self, c: Components, max_frames: int | None = None):
        self.c = c
        self.max_frames = max_frames

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

            while self.max_frames is None or result.frames < self.max_frames:
                pkt = c.source.read()
                if pkt is None:
                    break
                dets = c.detector.predict(pkt)
                decision = c.fsm.step(dets, c.clock.now())
                for o in c.outputs:
                    o.send(decision)
                result.decisions.append(decision)
                result.frames += 1
                result.infer_ms_total += dets.infer_ms

        log.info("sync run done: %d frames, avg infer %.2f ms", result.frames, result.avg_infer_ms)
        return result
