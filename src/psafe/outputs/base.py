from abc import ABC, abstractmethod

from psafe.core.packets import Decision, Detections, FramePacket
from psafe.core.registry import Registry


class Output(ABC):
    """Consumes decisions.

    send()      every heartbeat (not only on change): must be cheap and must
                not block for long. This is what the ESP32 sees.
    on_frame()  optional, worker thread, once per processed frame, with the
                image and detections. For viewers / recorders. Keep it fast:
                it runs in the inference loop.
    poll()      optional, MAIN thread, ~20x per second. GUI calls (imshow,
                waitKey) go here, because many GUI backends require the main
                thread. Return False to ask the runner to stop (e.g. 'q').
    """

    def open(self) -> None: ...

    @abstractmethod
    def send(self, decision: Decision) -> None: ...

    def on_frame(self, pkt: FramePacket, dets: Detections | None, decision: Decision) -> None: ...

    def poll(self) -> bool:
        return True

    def close(self) -> None: ...


OUTPUTS: Registry[Output] = Registry("output")
