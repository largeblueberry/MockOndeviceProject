"""YoloTFLiteDetector (Coral 경로)를 HW·TFLite 패키지 없이 검사한다.

FakeTpuEngine이 invoke()의 입력·출력 배열만 흉내 낸다. Pi와 Coral 사이에서 실제로 오가는 것이
그 배열뿐이므로 (docs/edge_tpu/troubleshooting.md Q4), 그 앞뒤의 우리 코드는 여기서 전부 검증된다.
남는 것(실제 delegate 로드, 속도, USB 분리 동작)은 Coral 실기 실험 몫이다.
"""

import json
import logging

import numpy as np
import pytest

from psafe.core.packets import FramePacket, State
from psafe.detectors import DETECTORS
from psafe.detectors import tpu_runtime
from psafe.detectors.manifest import EDGETPU_MARKER, ManifestError, ModelManifest, sha256_bytes
from psafe.detectors.tpu_runtime import LocalTpuEngine, TensorSpec
from psafe.detectors.yolo_tflite import YoloTFLiteDetector, dequantize, input_lut, to_chw_pixels

S = 320
N = 2100
NAMES = ["person", "car"]
IN_INT8 = TensorSpec((1, S, S, 3), np.dtype(np.int8), 1 / 255, -128)
OUT_SCALE, OUT_ZP = 1 / 250, -128


class FakeTpuEngine:
    def __init__(self, inp=IN_INT8, out=None, output=None, runtime=("fake", "0"), fail=None):
        self.inp = inp
        self.out = out or TensorSpec((1, 4 + len(NAMES), N), np.dtype(np.int8), OUT_SCALE, OUT_ZP)
        self.output = output
        self._runtime = runtime
        self.fail = fail
        self.inputs = []
        self.loaded = None
        self.closed = False

    def load(self, model_path):
        self.loaded = model_path

    def input_spec(self):
        return self.inp

    def output_specs(self):
        return (self.out,)

    def invoke(self, inp):
        if self.fail:
            raise self.fail
        self.inputs.append(inp)
        out = self.output if self.output is not None else np.full(self.out.shape, OUT_ZP, self.out.dtype)
        return (out.copy(),)

    def runtime(self):
        return self._runtime

    def close(self):
        self.closed = True


def quant(v):
    return np.clip(np.round(np.asarray(v, np.float64) / OUT_SCALE) + OUT_ZP, -128, 127).astype(np.int8)


def output_with_person(layout="chw", units="normalized"):
    """앵커 7번에 person 0.9 박스 하나. 입력 기준 (cx, cy, w, h) = (160, 160, 64, 128) 픽셀."""
    box = np.array([160, 160, 64, 128], np.float64)
    if units == "normalized":
        box = box / S
    y = np.zeros((1, 4 + len(NAMES), N))
    y[0, :4, 7] = box
    y[0, 4, 7] = 0.9
    if units == "pixel":       # pixel 좌표(0..320)는 int8 scale 1/250로 담을 수 없으니 float 출력으로 시험
        out = y.astype(np.float32)
    else:
        out = quant(y)
    return out.transpose(0, 2, 1).copy() if layout == "hwc" else out


def write_model(tmp_path, *, compiled=True, layout="chw", units="normalized", imgsz=S, names=NAMES,
                edgetpu_flag="auto", tpu_ops=100, build_tf=None, sha=None, fname=None):
    fname = fname or ("model_edgetpu.tflite" if compiled else "model_int8.tflite")
    data = b"TFL3" + (EDGETPU_MARKER if compiled else b"") + b"\x00" * 64
    w = tmp_path / fname
    w.write_bytes(data)
    entry = {"sha256": sha or sha256_bytes(data)}
    if edgetpu_flag is not None:
        entry["edgetpu"] = compiled if edgetpu_flag == "auto" else edgetpu_flag
    m = {"schema": 1, "id": "test", "imgsz": imgsz, "names": names, "box_units": units,
         "output_layout": layout, "files": {fname: entry}, "compile": {"tpu_ops": tpu_ops}}
    if build_tf:
        m["build"] = {"tensorflow": build_tf}
    (tmp_path / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    return str(w)


def make(tmp_path, delegate="edgetpu", engine=None, **kw):
    w = write_model(tmp_path, compiled=(delegate == "edgetpu"), **kw)
    return YoloTFLiteDetector(weights=w, delegate=delegate, engine=engine or FakeTpuEngine())


def frame(h=480, w=640, seq=0):
    return FramePacket(seq, 0.0, "t", np.random.default_rng(0).integers(0, 255, (h, w, 3), dtype=np.uint8))


# ---- 순수 함수 -------------------------------------------------------------

def test_input_lut_int8_matches_formula():
    lut = input_lut(IN_INT8)
    v = np.arange(256)
    assert lut.dtype == np.int8
    np.testing.assert_array_equal(lut, np.clip(np.round(v / 255 / (1 / 255)) - 128, -128, 127))
    assert lut[0] == -128 and lut[255] == 127


def test_input_lut_uint8_identity_when_scale_is_1_over_255():
    lut = input_lut(TensorSpec((1, S, S, 3), np.dtype(np.uint8), 1 / 255, 0))
    np.testing.assert_array_equal(lut, np.arange(256, dtype=np.uint8))


def test_input_lut_clips_to_dtype_range():
    lut = input_lut(TensorSpec((1, S, S, 3), np.dtype(np.int8), 1 / 512, 0))   # 1.0 -> 512: 넘침
    assert lut.max() == 127 and lut.min() == 0


def test_dequantize_int_and_float():
    spec = TensorSpec((1, 3), np.dtype(np.int8), 0.5, 10)
    np.testing.assert_allclose(dequantize(np.array([[10, 12, -10]], np.int8), spec), [[0, 1, -10]])
    f = np.array([[1.5]], np.float32)
    out = dequantize(f, TensorSpec((1, 1), np.dtype(np.float32)))
    assert out is not f and out[0, 0] == 1.5


def test_to_chw_pixels_handles_layout_and_units():
    y = np.zeros((1, N, 6), np.float32)
    y[0, 3, :4] = [0.5, 0.25, 0.1, 0.2]
    out = to_chw_pixels(y, "hwc", "normalized", S)
    assert out.shape == (1, 6, N)
    np.testing.assert_allclose(out[0, :4, 3], [160, 80, 32, 64])


# ---- 검출 결과 ----------------------------------------------------------------

def check_person(dets):
    (d,) = dets.objects
    assert d.cls == "person" and d.conf == pytest.approx(0.9, abs=0.01)
    # 640x480 -> letterbox 0.5배, 위아래 40 px 여백. 입력 (128..192, 96..224) -> 원본 (256..384, 112..368)
    np.testing.assert_allclose(d.bbox, (256 / 640, 112 / 480, 384 / 640, 368 / 480), atol=0.01)


@pytest.mark.parametrize("layout", ["chw", "hwc"])
def test_predict_decodes_quantized_normalized_output(tmp_path, layout):
    det = make(tmp_path, layout=layout, engine=FakeTpuEngine(
        out=TensorSpec((1, 6, N) if layout == "chw" else (1, N, 6), np.dtype(np.int8), OUT_SCALE, OUT_ZP),
        output=output_with_person(layout)))
    det.load()
    dets = det.predict(frame())
    check_person(dets)
    assert dets.model_name.startswith("yolo_tflite[edgetpu]")


def test_predict_with_float_pixel_output(tmp_path):
    det = make(tmp_path, units="pixel", engine=FakeTpuEngine(
        out=TensorSpec((1, 6, N), np.dtype(np.float32)), output=output_with_person(units="pixel")))
    det.load()
    check_person(det.predict(frame()))


def test_input_sent_to_engine_is_quantized_nhwc_rgb(tmp_path):
    eng = FakeTpuEngine()
    det = make(tmp_path, engine=eng)
    det.load()
    img = np.zeros((480, 640, 3), np.uint8)
    img[..., 2] = 255                                    # BGR 기준 빨강
    det.predict(FramePacket(0, 0.0, "t", img))
    x = eng.inputs[-1]
    assert x.shape == (1, S, S, 3) and x.dtype == np.int8
    assert x.flags["C_CONTIGUOUS"]                       # boxed[:, :, ::-1]의 음수 stride가 엔진으로 새지 않음
    lut = input_lut(IN_INT8)
    assert tuple(x[0, S // 2, S // 2]) == (lut[255], lut[0], lut[0])     # RGB 순서
    assert tuple(x[0, 0, 0]) == (lut[114],) * 3                         # letterbox 여백


def test_float_input_model_uses_plain_scaling(tmp_path):
    eng = FakeTpuEngine(inp=TensorSpec((1, S, S, 3), np.dtype(np.float32)))
    det = make(tmp_path, engine=eng)
    det.load()
    det.predict(frame())
    assert eng.inputs[-1].dtype == np.float32 and eng.inputs[-1].max() <= 1.0


def test_class_map_and_classes_filter(tmp_path):
    w = write_model(tmp_path, names=["occupant", "car"])
    eng = FakeTpuEngine(output=output_with_person())
    det = YoloTFLiteDetector(weights=w, engine=eng, class_map={"occupant": "person"}, classes=["car"])
    det.load()
    assert det.predict(frame()).objects == ()            # person으로 바뀐 뒤 car만 남기므로 0개


def test_warmup_sends_zero_tensors_of_input_dtype(tmp_path):
    eng = FakeTpuEngine()
    det = make(tmp_path, engine=eng)
    det.load()
    det.warmup()
    assert len(eng.inputs) == 2 and eng.inputs[0].dtype == np.int8


# ---- load() 검사: 하나라도 틀리면 시작하지 않는다 ------------------------------------

def test_missing_manifest(tmp_path):
    w = write_model(tmp_path)
    (tmp_path / "manifest.json").unlink()
    with pytest.raises(FileNotFoundError, match="manifest"):
        YoloTFLiteDetector(weights=w, engine=FakeTpuEngine()).load()


def test_sha256_mismatch(tmp_path):
    with pytest.raises(ManifestError, match="sha256"):
        make(tmp_path, sha="0" * 64).load()


def test_uncompiled_model_with_edgetpu_delegate_is_rejected(tmp_path):
    # 조용한 CPU 실행 방지: 미컴파일 모델에 delegate를 붙이면 TPU로 아무것도 안 간다
    w = write_model(tmp_path, compiled=False)
    with pytest.raises(ManifestError, match="not compiled"):
        YoloTFLiteDetector(weights=w, delegate="edgetpu", engine=FakeTpuEngine()).load()


def test_compiled_model_with_no_delegate_is_rejected(tmp_path):
    w = write_model(tmp_path, compiled=True)
    with pytest.raises(ManifestError, match="cannot run with delegate: none"):
        YoloTFLiteDetector(weights=w, delegate="none", engine=FakeTpuEngine()).load()


def test_manifest_edgetpu_flag_must_match_file(tmp_path):
    with pytest.raises(ManifestError, match="edgetpu=False"):
        make(tmp_path, edgetpu_flag=False).load()


def test_zero_tpu_ops_is_rejected(tmp_path):
    with pytest.raises(ManifestError, match="tpu_ops == 0"):
        make(tmp_path, tpu_ops=0).load()


def test_weights_not_listed_in_manifest(tmp_path):
    w = write_model(tmp_path)
    other = tmp_path / "other_edgetpu.tflite"
    other.write_bytes((tmp_path / "model_edgetpu.tflite").read_bytes())
    with pytest.raises(ManifestError, match="no entry"):
        YoloTFLiteDetector(weights=str(other), engine=FakeTpuEngine()).load()


def test_input_size_must_match_manifest(tmp_path):
    with pytest.raises(ManifestError, match="input shape"):
        make(tmp_path, imgsz=640).load()


def test_output_channels_must_match_names(tmp_path):
    with pytest.raises(ManifestError, match="4 \\+ 1 classes"):
        make(tmp_path, names=["person"]).load()


def test_integer_output_without_quantization_is_rejected(tmp_path):
    eng = FakeTpuEngine(out=TensorSpec((1, 6, N), np.dtype(np.int8)))      # scale 0 -> 역양자화 불가
    with pytest.raises(ManifestError, match="integer output"):
        make(tmp_path, engine=eng).load()


def test_failed_tensor_check_closes_engine(tmp_path):
    # engine.load()로 Coral을 연 뒤 검사에서 실패하면 직접 닫아야 한다 (open_all은 아직 close를 등록 전)
    eng = FakeTpuEngine()
    with pytest.raises(ManifestError):
        make(tmp_path, imgsz=640, engine=eng).load()
    assert eng.loaded and eng.closed


def test_newer_build_tf_only_warns(tmp_path, caplog):
    det = make(tmp_path, build_tf="2.18.0", engine=FakeTpuEngine(runtime=("tflite-runtime", "2.17.1")))
    with caplog.at_level(logging.WARNING, "psafe.detector.tflite"):
        det.load()
    assert "built with TF 2.18.0" in caplog.text


def test_bad_manifest_values(tmp_path):
    base = {"schema": 1, "imgsz": 320, "names": ["person"], "box_units": "normalized",
            "output_layout": "chw", "files": {"m.tflite": {"sha256": "ab"}}}
    for key, bad in [("schema", 2), ("box_units", "cm"), ("output_layout", "nchw"), ("names", []),
                     ("imgsz", True), ("files", {})]:
        with pytest.raises(ManifestError):
            ModelManifest.from_dict({**base, key: bad}, tmp_path / "manifest.json")


# ---- 고장: Coral이 실행 중 빠지면 ---------------------------------------------------

def test_invoke_error_propagates_and_becomes_fault(tmp_path):
    from psafe.runners import process_frame
    from tests.test_runner_logic import make_components

    eng = FakeTpuEngine()
    det = make(tmp_path, engine=eng)
    det.load()
    eng.fail = RuntimeError("USB transfer error")
    with pytest.raises(RuntimeError):
        det.predict(frame())

    c = make_components()
    c.detector = det
    img = np.random.default_rng(1).integers(0, 255, (48, 64, 3), dtype=np.uint8)
    d, dets = process_frame(c, FramePacket(0, c.clock.now(), "t", img))
    assert (d.safe, d.state, d.reason) == (False, State.FAULT, "detector_error") and dets is None


# ---- 레지스트리, LocalTpuEngine --------------------------------------------------

def test_registry_creates_detector_from_config(tmp_path):
    w = write_model(tmp_path)
    det = DETECTORS.create("yolo_tflite", weights=w, delegate="edgetpu", threads=1)
    assert isinstance(det, YoloTFLiteDetector) and isinstance(det.engine, LocalTpuEngine)


def test_bad_delegate_name(tmp_path):
    with pytest.raises(ValueError, match="delegate"):
        YoloTFLiteDetector(weights="x.tflite", delegate="gpu")


class _FakeInterpreter:
    def __init__(self, model_path, experimental_delegates, num_threads):
        self.delegates = experimental_delegates

    def allocate_tensors(self):
        pass

    def get_input_details(self):
        return [{"index": 0, "shape": np.array([1, S, S, 3]), "dtype": np.int8, "quantization": (1 / 255, -128)}]

    def get_output_details(self):
        return [{"index": 1, "shape": np.array([1, 6, N]), "dtype": np.int8, "quantization": (OUT_SCALE, OUT_ZP)}]


def test_local_engine_delegate_failure_raises_without_cpu_fallback(monkeypatch):
    created = []

    def bad_delegate(lib):
        raise ValueError(f"Failed to load delegate from {lib}")

    def interp(**kw):
        created.append(kw)
        return _FakeInterpreter(**kw)

    monkeypatch.setattr(tpu_runtime, "_find_provider",
                        lambda: tpu_runtime._Provider("tflite-runtime", interp, bad_delegate))
    eng = LocalTpuEngine("edgetpu", delegate_lib="libedgetpu.so.1")
    with pytest.raises(RuntimeError, match="no CPU fallback"):
        eng.load("m.tflite")
    assert created == []                                   # 인터프리터를 아예 만들지 않았다


def test_local_engine_reads_specs(monkeypatch):
    monkeypatch.setattr(tpu_runtime, "_find_provider",
                        lambda: tpu_runtime._Provider("tflite-runtime", _FakeInterpreter, lambda lib: ("delegate", lib)))
    eng = LocalTpuEngine("edgetpu", delegate_lib="edgetpu.dll")
    eng.load("m.tflite")
    assert eng._itp.delegates == [("delegate", "edgetpu.dll")]
    assert eng.input_spec() == TensorSpec((1, S, S, 3), np.dtype(np.int8), 1 / 255, -128)
    assert eng.output_specs()[0].quantized


def test_spec_prefers_quantization_parameters_and_rejects_per_channel():
    base = {"name": "out", "shape": np.array([1, 6, N]), "dtype": np.int8, "quantization": (0.0, 0)}
    spec = tpu_runtime._spec({**base, "quantization_parameters": {"scales": np.array([0.004]),
                                                                  "zero_points": np.array([-128])}})
    assert spec.quantized and spec.scale == pytest.approx(0.004) and spec.zero_point == -128
    with pytest.raises(ValueError, match="per-channel"):
        tpu_runtime._spec({**base, "quantization_parameters": {"scales": np.array([0.1, 0.2]),
                                                               "zero_points": np.array([0, 0])}})
    legacy = tpu_runtime._spec({**base, "quantization": (0.5, 3)})
    assert (legacy.scale, legacy.zero_point) == (0.5, 3)


def test_local_engine_none_delegate_attaches_nothing(monkeypatch):
    monkeypatch.setattr(tpu_runtime, "_find_provider",
                        lambda: tpu_runtime._Provider("tensorflow", _FakeInterpreter, lambda lib: 1 / 0))
    eng = LocalTpuEngine("none")
    eng.load("m.tflite")
    assert eng._itp.delegates == []
