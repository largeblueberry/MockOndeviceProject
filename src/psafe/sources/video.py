"""VideoFileSource: plays a recorded clip as if it were a live camera.

Same timing rules as DummySource:
  * paced to the file's fps against the injected clock,
  * read() returns the LATEST due frame (frames in between are grabbed and
    discarded, counted in `skipped`),
  * timestamp = when the frame was "captured" (start + idx / fps).
With FakeClock this makes a clip run instantly and deterministically.
"""

from pathlib import Path

import cv2

from psafe.core.clock import Clock
from psafe.core.packets import FramePacket
from psafe.sources.base import SOURCES, Source

EPS = 1e-9


@SOURCES.register("video")
class VideoFileSource(Source):
    def __init__(
        self,
        clock: Clock,
        path: str,
        loop: bool = False,
        fps: float | None = None,              # override the file's fps
        resize: list[int] | None = None,       # [width, height]
        source_id: str = "video",
    ):
        super().__init__(clock, source_id)
        self.path = str(path)
        self.loop = loop
        self.fps_override = fps
        self.resize = tuple(resize) if resize else None
        self.fps = 0.0
        self.skipped = 0
        self._cap: cv2.VideoCapture | None = None

    def open(self) -> None:
        if not Path(self.path).is_file():
            raise FileNotFoundError(f"video not found: {self.path}")
        self._cap = cv2.VideoCapture(self.path)
        if not self._cap.isOpened():
            raise RuntimeError(f"cannot open video: {self.path}")
        self.fps = float(self.fps_override or self._cap.get(cv2.CAP_PROP_FPS) or 30.0)
        self._start = self.clock.now()
        self._next_idx = 0      # global frame index (keeps growing across loops)
        self._offset = 0        # global index of file frame 0 in the current loop
        self._file_pos = 0      # next frame the decoder will return
        self.skipped = 0

    def read(self) -> FramePacket | None:
        if self._cap is None:
            self.open()
        cap = self._cap
        assert cap is not None

        due = int((self.clock.now() - self._start) * self.fps + EPS)
        idx = max(self._next_idx, due)
        self.skipped += idx - self._next_idx

        # Throw away frames we are too late for (grab = decode-light skip).
        while self._file_pos < idx - self._offset:
            if cap.grab():
                self._file_pos += 1
            elif not self._rewind(idx):
                return None

        self.clock.sleep(self._start + idx / self.fps - self.clock.now())
        ok, img = cap.read()
        if not ok:
            if not self._rewind(idx):
                return None
            ok, img = cap.read()
            if not ok:
                return None
        self._file_pos += 1
        self._next_idx = idx + 1
        if self.resize:
            img = cv2.resize(img, self.resize, interpolation=cv2.INTER_AREA)
        return FramePacket(
            seq=idx,
            timestamp=self._start + idx / self.fps,
            source_id=self.source_id,
            image=img,
            meta={"video_t": (self._file_pos - 1) / self.fps},
        )

    def _rewind(self, idx: int) -> bool:
        """End of file. With loop, map global frame `idx` to file frame 0."""
        if not self.loop or self._file_pos == 0:
            return False
        assert self._cap is not None
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        self._offset, self._file_pos = idx, 0
        return True

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
