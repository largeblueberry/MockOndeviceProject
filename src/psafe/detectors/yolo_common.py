"""YOLO(v8 / 11, Ultralytics export) 공통 전처리·후처리. 런타임(ONNX, TFLite/Edge TPU)과 무관한 순수 함수.

검출기마다 다른 것은 "텐서를 어떻게 실행하나"뿐이고, letterbox -> (실행) -> decode -> NMS는
같다. 그래서 여기 한 곳에 두고 YoloOnnxDetector와 YoloTFLiteDetector가 같이 쓴다. 두 검출기
결과가 다르면 전·후처리가 아니라 런타임·양자화 차이라는 뜻이 된다 (docs/CORAL_DESIGN.md §3).
"""

import ast

import cv2
import numpy as np

from psafe.core.packets import Detection

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
