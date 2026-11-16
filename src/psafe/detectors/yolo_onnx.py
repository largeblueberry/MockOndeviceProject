"""ONNX Runtime으로 돌리는 YOLO (v8 / 11, Ultralytics export).

predict() = letterbox -> ONNX -> decode -> NMS -> bbox 정규화 -> 클래스 매핑.
모델별 처리는 모두 여기에 있다 (DESIGN.md §5.2). 따라서 파인튜닝한 모델로
바꾸는 것은 설정 변경만으로 된다: `weights`를 새 .onnx로 지정하고, 클래스
이름이 "person"이 아니면 매핑한다:  class_map: {minifig: person}

기대하는 출력 형태: (1, 4 + num_classes, num_anchors), 박스는 입력 픽셀 기준
(cx, cy, w, h). v8/v11에서 `YOLO(...).export(format="onnx")`가 만드는 형태다.
다른 형태는 load()에서 명확한 오류로 실패한다.
"""

import ast
import logging
import time

import cv2
import numpy as np

from psafe.core.packets import Detection, FrameDetections, FramePacket
from psafe.detectors.detector_api import DETECTORS, Detector

log = logging.getLogger("psafe.detector.yolo")

COCO80 = (
    "person bicycle car motorcycle airplane bus train truck boat traffic_light fire_hydrant stop_sign "
    "parking_meter bench bird cat dog horse sheep cow elephant bear zebra giraffe backpack umbrella "
    "handbag tie suitcase frisbee skis snowboard sports_ball kite baseball_bat baseball_glove skateboard "
    "surfboard tennis_racket bottle wine_glass cup fork knife spoon bowl banana apple sandwich orange "
    "broccoli carrot hot_dog pizza donut cake chair couch potted_plant bed dining_table toilet tv laptop "
    "mouse remote keyboard cell_phone microwave oven toaster sink refrigerator book clock vase scissors "
    "teddy_bear hair_drier toothbrush"
).split()


# ---- 순수 함수 (모델 없이 단위 테스트) ----------------------------

def letterbox(img: np.ndarray, size: int, pad_value: int = 114) -> tuple[np.ndarray, float, int, int]:
    """비율을 유지하며 크기를 줄이고 size x size로 여백을 채운다.
    (여백을 채운 BGR 이미지, 배율, pad_x, pad_y)를 반환한다."""
    h, w = img.shape[:2]
    scale = min(size / w, size / h)
    nw, nh = round(w * scale), round(h * scale)
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR) if (nw, nh) != (w, h) else img
    pad_x, pad_y = (size - nw) // 2, (size - nh) // 2
    out = np.full((size, size, 3), pad_value, np.uint8)
    out[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
    return out, scale, pad_x, pad_y


def to_blob(img_bgr: np.ndarray) -> np.ndarray:
    """BGR uint8 HWC -> [0, 1] 범위의 RGB float32 NCHW."""
    return np.ascontiguousarray(img_bgr[:, :, ::-1].transpose(2, 0, 1)[None], dtype=np.float32) / 255.0


def nms(boxes: np.ndarray, scores: np.ndarray, iou_thres: float) -> list[int]:
    """xyxy 박스에 대한 탐욕적 NMS. 남긴 인덱스를 점수 높은 순으로 반환한다."""
    order = scores.argsort()[::-1]
    x1, y1, x2, y2 = boxes.T
    areas = (x2 - x1).clip(0) * (y2 - y1).clip(0)
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        iw = (np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest])).clip(0)
        ih = (np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest])).clip(0)
        inter = iw * ih
        iou = inter / (areas[i] + areas[rest] - inter + 1e-9)
        order = rest[iou <= iou_thres]
    return keep


def decode(
    output: np.ndarray,
    names: list[str],
    conf_thres: float,
    iou_thres: float,
    scale: float,
    pad_x: int,
    pad_y: int,
    orig_w: int,
    orig_h: int,
) -> list[Detection]:
    """(1, 4+nc, N) 원시 출력 -> 원본 이미지 기준으로 bbox를 정규화한 검출 결과."""
    pred = output[0].T                                   # (N, 4+nc)
    scores_all = pred[:, 4:]
    cls = scores_all.argmax(axis=1)
    conf = scores_all[np.arange(len(pred)), cls]
    m = conf >= conf_thres
    if not m.any():
        return []
    pred, cls, conf = pred[m], cls[m], conf[m]

    cx, cy, w, h = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
    boxes = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)
    boxes[:, [0, 2]] = ((boxes[:, [0, 2]] - pad_x) / scale).clip(0, orig_w)   # letterbox 되돌리기
    boxes[:, [1, 3]] = ((boxes[:, [1, 3]] - pad_y) / scale).clip(0, orig_h)

    out: list[Detection] = []
    for c in np.unique(cls):                             # 클래스별 NMS
        idx = np.where(cls == c)[0]
        for k in nms(boxes[idx], conf[idx], iou_thres):
            x1, y1, x2, y2 = boxes[idx[k]]
            name = names[c] if c < len(names) else str(int(c))
            out.append(Detection(name, float(conf[idx[k]]),
                                 (x1 / orig_w, y1 / orig_h, x2 / orig_w, y2 / orig_h)))
    return out


def names_from_metadata(meta: dict[str, str]) -> list[str] | None:
    """Ultralytics는 {0: 'person', ...}을 ONNX 메타데이터에 문자열로 저장한다."""
    raw = meta.get("names")
    if not raw:
        return None
    try:
        d = ast.literal_eval(raw)
        return [str(d[i]).replace(" ", "_") for i in sorted(d)]
    except (ValueError, SyntaxError):
        return None


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
