"""VideoFileSource: 녹화된 영상을 실시간 카메라처럼 재생한다.

DummySource와 같은 시간 규칙을 따른다:
  * 주입된 시계 기준으로 파일의 fps에 맞춰 속도를 조절한다,
  * read()는 시각이 된 프레임 중 최신 것을 반환한다 (그 사이 프레임은 가져와서
    버리고 `skipped`에 집계),
  * timestamp = 프레임이 "촬영된" 시각 (start + idx / fps).
FakeClock과 함께 쓰면 영상이 즉시, 결정적으로 재생된다.
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
        fps: float | None = None,              # 파일의 fps 대신 사용
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
        self._next_idx = 0      # 전체 프레임 번호 (반복 재생해도 계속 증가)
        self._offset = 0        # 현재 반복에서 파일 0번 프레임의 전체 번호
        self._file_pos = 0      # 디코더가 다음에 반환할 프레임
        self.skipped = 0

    def read(self) -> FramePacket | None:
        if self._cap is None:
            self.open()
        cap = self._cap
        assert cap is not None

        due = int((self.clock.now() - self._start) * self.fps + EPS)
        idx = max(self._next_idx, due)
        self.skipped += idx - self._next_idx

        # 이미 늦은 프레임은 버린다 (grab = 디코딩을 최소화한 건너뛰기).
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
        """파일 끝. loop이면 전체 프레임 번호 `idx`를 파일 0번 프레임에 대응시킨다."""
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
