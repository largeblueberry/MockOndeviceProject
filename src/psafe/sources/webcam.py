"""WebcamSource: USB/laptop camera through OpenCV.

On the Raspberry Pi CSI camera, use a picamera2-based source instead
(Bookworm's libcamera stack is not reachable through cv2.VideoCapture).
"""

import logging
import os

import cv2

from psafe.core.clock import Clock, FakeClock
from psafe.core.packets import FramePacket
from psafe.sources.base import SOURCES, Source

log = logging.getLogger("psafe.source.webcam")


@SOURCES.register("webcam")
class WebcamSource(Source):
    def __init__(
        self,
        clock: Clock,
        index: int = 0,
        width: int = 640,
        height: int = 480,
        fps: float = 30,
        mirror: bool = False,
        max_failures: int = 10,      # consecutive failed reads before giving up
        source_id: str = "webcam",
    ):
        if isinstance(clock, FakeClock):
            raise ValueError("webcam is live; it needs a real clock (use runner.mode: threaded)")
        super().__init__(clock, source_id)
        self.index, self.width, self.height, self.fps = index, width, height, fps
        self.mirror = mirror
        self.max_failures = max_failures
        self._cap: cv2.VideoCapture | None = None
        self._seq = 0

    def open(self) -> None:
        # DirectShow opens much faster than the default MSMF backend on Windows.
        api = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.index, api)
        if not cap.isOpened():
            raise RuntimeError(f"cannot open webcam {self.index}")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # keep the driver queue short (not all backends obey)
        self._cap = cap
        self._seq = 0
        log.info("webcam %d opened: %dx%d @ %.0f fps", self.index,
                 cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT), cap.get(cv2.CAP_PROP_FPS))

    def read(self) -> FramePacket | None:
        assert self._cap is not None, "open() first"
        for attempt in range(self.max_failures):
            ok, img = self._cap.read()
            if ok:
                break
            log.warning("webcam read failed (%d/%d)", attempt + 1, self.max_failures)
            self.clock.sleep(0.05)
        else:
            return None  # camera gone: runner sees end of stream and stops (N)
        # read() blocks until a new frame arrives, so "now" ~= capture time
        # (+ USB transfer). Good enough for staleness checks at 100s of ms.
        ts = self.clock.now()
        if self.mirror:
            img = cv2.flip(img, 1)
        pkt = FramePacket(self._seq, ts, self.source_id, img)
        self._seq += 1
        return pkt

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
