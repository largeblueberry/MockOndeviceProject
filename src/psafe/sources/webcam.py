"""WebcamSource: OpenCV로 USB/노트북 카메라를 읽는다.

라즈베리파이 CSI 카메라에는 picamera2 기반 Source를 대신 써야 한다
(Bookworm의 libcamera 스택은 cv2.VideoCapture로 접근할 수 없다).
"""

import logging
import os

import cv2

from psafe.core.clock import Clock, FakeClock
from psafe.core.packets import FramePacket
from psafe.sources.source_api import SOURCES, Source

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
        max_failures: int = 10,      # 연속으로 이만큼 읽기에 실패하면 포기
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
        # Windows에서는 DirectShow가 기본 MSMF 백엔드보다 훨씬 빨리 열린다.
        api = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.index, api)
        if not cap.isOpened():
            raise RuntimeError(f"cannot open webcam {self.index}")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # 드라이버 큐를 짧게 유지 (모든 백엔드가 따르지는 않음)
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
            return None  # 카메라 사라짐: Runner가 스트림 종료로 보고 멈춘다 (N)
        # read()는 새 프레임이 올 때까지 대기하므로 "now" ~= 촬영 시각
        # (+ USB 전송 시간). 수백 ms 단위의 오래됨 검사에는 충분하다.
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
