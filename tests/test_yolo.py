"""YOLO pre/post-processing without a model; real-model test runs only if
models/yolo11n_320.onnx exists."""

from pathlib import Path

import numpy as np
import pytest

from psafe.core.packets import FramePacket
from psafe.models.yolo_onnx import COCO80, decode, letterbox, names_from_metadata, nms, to_blob

NAMES = ["person", "car"]


def raw(*boxes):
    """Build a (1, 4+2, N) output from (cx, cy, w, h, p_person, p_car) tuples."""
    return np.array(boxes, np.float32).T[None]


def test_letterbox_landscape():
    img = np.zeros((480, 640, 3), np.uint8)
    out, scale, px, py = letterbox(img, 320)
    assert out.shape == (320, 320, 3)
    assert scale == 0.5 and px == 0 and py == 40      # 640x480 -> 320x240, padded top/bottom


def test_to_blob_layout():
    img = np.zeros((4, 4, 3), np.uint8)
    img[..., 2] = 255                                  # red in BGR
    b = to_blob(img)
    assert b.shape == (1, 3, 4, 4) and b.dtype == np.float32
    assert b[0, 0].max() == 1.0 and b[0, 2].max() == 0.0   # channel 0 is R after BGR->RGB


def test_decode_undoes_letterbox_and_normalizes():
    # 640x480 image -> scale 0.5, pad_y 40. Person at original (100..300, 100..400)
    # -> model space x 50..150, y 90..240 -> cx 100, cy 165, w 100, h 150
    out = raw((100, 165, 100, 150, 0.9, 0.05))
    (d,) = decode(out, NAMES, 0.25, 0.45, 0.5, 0, 40, 640, 480)
    assert d.cls == "person" and d.conf == pytest.approx(0.9)
    assert d.bbox == pytest.approx((100 / 640, 100 / 480, 300 / 640, 400 / 480), abs=1e-4)


def test_decode_filters_low_conf():
    assert decode(raw((50, 50, 10, 10, 0.1, 0.1)), NAMES, 0.25, 0.45, 1, 0, 0, 100, 100) == []


def test_nms_merges_overlaps_per_class_only():
    out = raw(
        (50, 50, 20, 20, 0.9, 0.0),    # person
        (51, 51, 20, 20, 0.8, 0.0),    # same person, lower score -> suppressed
        (50, 50, 20, 20, 0.0, 0.7),    # car at the same place -> kept (other class)
        (10, 10, 5, 5, 0.6, 0.0),      # another person far away -> kept
    )
    dets = decode(out, NAMES, 0.25, 0.45, 1, 0, 0, 100, 100)
    assert sorted((d.cls, round(d.conf, 1)) for d in dets) == [("car", 0.7), ("person", 0.6), ("person", 0.9)]


def test_nms_basic():
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60]], np.float32)
    assert nms(boxes, np.array([0.9, 0.8, 0.7]), 0.5) == [0, 2]


def test_boxes_clipped_to_image():
    (d,) = decode(raw((5, 5, 40, 40, 0.9, 0)), NAMES, 0.25, 0.45, 1, 0, 0, 100, 100)
    assert min(d.bbox) >= 0.0 and max(d.bbox) <= 1.0


def test_names_from_ultralytics_metadata():
    assert names_from_metadata({"names": "{0: 'person', 1: 'traffic light'}"}) == ["person", "traffic_light"]
    assert names_from_metadata({}) is None
    assert len(COCO80) == 80


WEIGHTS = Path("models/yolo11n_320.onnx")


@pytest.mark.skipif(not WEIGHTS.exists(), reason=f"{WEIGHTS} not downloaded")
def test_real_model_loads_and_runs():
    from psafe.models.yolo_onnx import YoloOnnxDetector
    det = YoloOnnxDetector(str(WEIGHTS))
    det.load()
    det.warmup()
    assert det.input_size == 320 and det.names[0] == "person"
    img = np.full((480, 640, 3), 114, np.uint8)
    d = det.predict(FramePacket(0, 0.0, "t", img))
    assert d.objects == () or all(0 <= v <= 1 for o in d.objects for v in o.bbox)
    assert d.infer_ms > 0
