"""ONNX Runtime으로 돌리는 YOLO (v8 / 11, Ultralytics export).

predict() = letterbox -> ONNX -> decode -> NMS -> bbox 정규화 -> 클래스 매핑.
모델별 처리는 모두 여기에 있다 (DESIGN.md §5.2). 따라서 파인튜닝한 모델로
바꾸는 것은 설정 변경만으로 된다: `weights`를 새 .onnx로 지정하고, 클래스
이름이 "person"이 아니면 매핑한다:  class_map: {minifig: person}

기대하는 출력 형태: (1, 4 + num_classes, num_anchors), 박스는 입력 픽셀 기준
(cx, cy, w, h). v8/v11에서 `YOLO(...).export(format="onnx")`가 만드는 형태다.
다른 형태는 load()에서 명확한 오류로 실패한다.
"""

import logging
import time

import numpy as np

from psafe.core.packets import FrameDetections, FramePacket
from psafe.detectors.detector_api import DETECTORS, Detector
from psafe.detectors.yolo_common import (  # noqa: F401  (기존 import 경로 유지: tests, tools)
    COCO80, decode, letterbox, names_from_metadata, nms, to_blob,
)

log = logging.getLogger("psafe.detector.yolo")

# ---- 검출기 ---------------------------------------------------------------

@DETECTORS.register("yolo_onnx")
class YoloOnnxDetector(Detector):
    def __init__(
        self,
        weights: str,
        input_size: int | None = None,        # 기본값: 모델 입력 형태에서 읽음
        conf_threshold: float = 0.20,         # 낮게 유지. FSM이 그 위에 person_conf_low를 적용한다
        iou_threshold: float = 0.45,
        class_map: dict[str, str] | None = None,   # 모델 클래스 이름 -> 내부 이름, 예: {minifig: person}
        classes: list[str] | None = None,     # 이 내부 이름만 남김 (None = 전부)
        threads: int = 4,
    ):
        self.weights = weights
        self.input_size = input_size
        self.conf_threshold, self.iou_threshold = conf_threshold, iou_threshold
        self.class_map = class_map or {}
        self.classes = set(classes) if classes else None
        self.threads = threads
        self.name = f"yolo_onnx:{weights}"
        self._session = None

    def load(self) -> None:
        import onnxruntime as ort  # 여기서 import해야 onnxruntime 없이도 패키지가 동작한다
        from pathlib import Path

        if not Path(self.weights).is_file():
            raise FileNotFoundError(f"model weights not found: {self.weights} "
                                    "(export with ultralytics, see docs/DESIGN.md §14.1)")
        so = ort.SessionOptions()
        so.intra_op_num_threads = self.threads
        self._session = sess = ort.InferenceSession(self.weights, so, providers=["CPUExecutionProvider"])
        inp = sess.get_inputs()[0]
        self._input_name = inp.name
        shape = inp.shape                                      # 예: [1, 3, 320, 320]
        static = isinstance(shape[2], int) and isinstance(shape[3], int)
        if self.input_size is None:
            if not static:
                raise ValueError(f"{self.weights}: dynamic input shape, set input_size in config")
            self.input_size = int(shape[2])
        elif static and shape[2] != self.input_size:
            raise ValueError(f"{self.weights}: model expects {shape[2]}, config says input_size={self.input_size}")

        out_shape = sess.get_outputs()[0].shape
        meta = sess.get_modelmeta().custom_metadata_map
        names = names_from_metadata(meta) or COCO80
        if isinstance(out_shape[1], int) and out_shape[1] != 4 + len(names):
            raise ValueError(f"{self.weights}: output {out_shape} doesn't match 4 + {len(names)} classes "
                             "(expected a YOLOv8/11 export)")
        self.names = [self.class_map.get(n, n) for n in names]
        log.info("loaded %s: input %d, %d classes, output %s", self.weights, self.input_size, len(names), out_shape)

    def warmup(self) -> None:
        assert self._session is not None and self.input_size
        dummy = np.zeros((1, 3, self.input_size, self.input_size), np.float32)
        for _ in range(2):
            self._session.run(None, {self._input_name: dummy})

    def predict(self, pkt: FramePacket) -> FrameDetections:
        assert self._session is not None and self.input_size, "load() first"
        t0 = time.perf_counter()   # 측정용일 뿐, 판단에는 쓰지 않는다
        img = pkt.image
        h, w = img.shape[:2]
        boxed, scale, px, py = letterbox(img, self.input_size)
        out = self._session.run(None, {self._input_name: to_blob(boxed)})[0]
        objs = decode(out, self.names, self.conf_threshold, self.iou_threshold, scale, px, py, w, h)
        if self.classes is not None:
            objs = [o for o in objs if o.cls in self.classes]
        infer_ms = (time.perf_counter() - t0) * 1000
        return FrameDetections(pkt.seq, pkt.timestamp, tuple(objs), self.name, infer_ms)

    def close(self) -> None:
        self._session = None
