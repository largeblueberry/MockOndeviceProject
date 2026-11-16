"""ViewerOutput: 실시간 창 + 선택적 주석 녹화. 시연 화면으로도 쓴다.

ROI, 검출 결과(빨강 = ROI 안의 사람, 주황 = ROI 밖의 사람, 회색 = 다른 클래스),
Y/N 배너를 그린다. 배너는 하트비트 판단(ESP32가 받는 값)을 보여 준다. worker가
멈추면 프레임별 판단과 달라질 수 있는데, 시연에서 보여 주고 싶은 것이 바로 이 점이다.
"""

import logging
import threading

import cv2
import numpy as np

from psafe.core.packets import Decision, FrameDetections, FramePacket
from psafe.safety.roi import Roi
from psafe.outputs.output_api import OUTPUTS, Output

log = logging.getLogger("psafe.output.viewer")

GREEN, RED, ORANGE, GREY, YELLOW, WHITE = (60, 180, 60), (40, 40, 220), (0, 140, 255), (160, 160, 160), (0, 220, 255), (255, 255, 255)


def annotate(img: np.ndarray, roi: Roi, dets: FrameDetections | None, decision: Decision,
             person_conf_low: float = 0.0) -> np.ndarray:
    out = img.copy()
    h, w = out.shape[:2]
    poly = np.array([(int(x * w), int(y * h)) for x, y in roi.polygon], np.int32)
    cv2.polylines(out, [poly], True, YELLOW, 2)

    for o in (dets.objects if dets else ()):
        x1, y1, x2, y2 = o.bbox
        p1, p2 = (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h))
        if o.cls == "person" and o.conf >= person_conf_low:
            color = RED if roi.touches(o.bbox) else ORANGE
        else:
            color = GREY
        cv2.rectangle(out, p1, p2, color, 2)
        cv2.putText(out, f"{o.cls} {o.conf:.2f}", (p1[0], max(12, p1[1] - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)

    if dets:
        cv2.putText(out, f"{dets.model_name.split('/')[-1]}  {dets.infer_ms:.0f} ms  seq {dets.seq}",
                    (6, h - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45, WHITE, 1, cv2.LINE_AA)
    return draw_banner(out, decision)


def draw_banner(img: np.ndarray, d: Decision) -> np.ndarray:
    w = img.shape[1]
    cv2.rectangle(img, (0, 0), (w, 30), GREEN if d.safe else RED, -1)
    cv2.putText(img, f"{d.yn}  {d.state.value}  {d.reason}", (8, 21),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, WHITE, 2, cv2.LINE_AA)
    return img


@OUTPUTS.register("viewer")
class ViewerOutput(Output):
    def __init__(
        self,
        roi: list[list[float]],
        person_conf_low: float = 0.0,
        show: bool = True,
        window: str = "psafe",
        record_path: str | None = None,   # 예: recordings/run.mp4 (주석 포함)
        record_fps: float = 15.0,
        scale: float = 1.0,
    ):
        self.roi = Roi(roi)
        self.person_conf_low = person_conf_low
        self.show, self.window = show, window
        self.record_path, self.record_fps = record_path, record_fps
        self.scale = scale
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None     # 주석을 그린 최신 프레임 (worker가 씀)
        self._fresh = False
        self._heartbeat: Decision | None = None
        self._writer: cv2.VideoWriter | None = None

    def send(self, decision: Decision) -> None:
        with self._lock:
            self._heartbeat = decision

    def on_frame(self, pkt: FramePacket, dets: FrameDetections | None, decision: Decision) -> None:
        frame = annotate(pkt.image, self.roi, dets, decision, self.person_conf_low)
        if self.scale != 1.0:
            frame = cv2.resize(frame, None, fx=self.scale, fy=self.scale)
        if self.record_path:
            self._record(frame)
        with self._lock:
            self._frame, self._fresh = frame, True

    def poll(self) -> bool:
        if not self.show:
            return True
        with self._lock:
            frame, fresh, hb = self._frame, self._fresh, self._heartbeat
            self._fresh = False
        if frame is not None and (fresh or hb is not None):
            shown = frame.copy()
            if hb is not None:
                draw_banner(shown, hb)  # 지금 ESP32가 실제로 받는 값
            cv2.imshow(self.window, shown)
        key = cv2.waitKey(1) & 0xFF
        return key not in (ord("q"), 27)  # q / Esc -> 종료

    def _record(self, frame: np.ndarray) -> None:
        if self._writer is None:
            h, w = frame.shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*("mp4v" if self.record_path.endswith(".mp4") else "MJPG"))
            self._writer = cv2.VideoWriter(self.record_path, fourcc, self.record_fps, (w, h))
            if not self._writer.isOpened():
                log.error("cannot write %s; recording disabled", self.record_path)
                self.record_path = None
                return
            log.info("recording to %s", self.record_path)
        self._writer.write(frame)

    def close(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        if self.show:
            try:
                cv2.destroyWindow(self.window)
            except cv2.error:
                pass
