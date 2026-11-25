"""TFLite로 돌리는 YOLO. delegate="edgetpu"면 신경망 계산을 Coral에서 한다.

한 프레임 (docs/edge_tpu/troubleshooting.md Q4):
    letterbox -> RGB -> int8 양자화(LUT) -> [invoke: Coral] -> 역양자화 -> (좌표 단위 맞춤) -> decode/NMS
YoloOnnxDetector와 다른 것은 가운데(양자화, invoke, 역양자화)뿐이고, 앞뒤는 yolo_common을 같이 쓴다.

모델 파일의 성질(imgsz, names, 좌표 단위, 출력 축 순서, 해시, 컴파일 여부)은 옆 manifest.json에서
읽고 load()에서 전부 검사한다. 검사 하나라도 틀리면 예외 -> 노드가 시작되지 않음 -> 허브는 timeout으로
본다 (EMPTY 불가). 실행 중 invoke() 예외는 runners.process_frame이 detector_error FAULT로 바꾼다.

    model:
      name: yolo_tflite
      options:
        weights: models/y8n_320_incar_v1/model_edgetpu.tflite   # 같은 폴더의 manifest.json을 읽음
        delegate: edgetpu        # 배포는 항상 edgetpu. none은 PC에서 컴파일 전 INT8 모델 평가용
        threads: 2               # TPU로 안 간 연산용 CPU 스레드
        conf_threshold: 0.20
        # class_map: { occupant: person }
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np

from psafe.core.packets import FrameDetections, FramePacket
from psafe.detectors.detector_api import DETECTORS, Detector
from psafe.detectors.manifest import (
    EDGETPU_MARKER, ModelManifest, ManifestError, default_manifest_path, is_edgetpu_compiled, major_minor, sha256_bytes,
)
from psafe.detectors.tpu_runtime import DELEGATES, LocalTpuEngine, TensorSpec, TpuEngine
from psafe.detectors.yolo_common import decode, letterbox

log = logging.getLogger("psafe.detector.tflite")

INPUT_DTYPES = (np.dtype(np.int8), np.dtype(np.uint8), np.dtype(np.float32))


# ---- 순수 함수 (모델 없이 단위 테스트) ----------------------------

def input_lut(spec: TensorSpec) -> np.ndarray:
    """픽셀값 0..255 -> 양자화된 입력값 표 (256칸). 매 프레임 float 연산 없이 `lut[rgb]` 한 번으로 끝난다.
    q = clip(round((v / 255) / scale) + zero_point, dtype 범위)"""
    info = np.iinfo(spec.dtype)
    v = np.arange(256, dtype=np.float64) / 255.0
    q = np.round(v / spec.scale) + spec.zero_point
    return np.clip(q, info.min, info.max).astype(spec.dtype)


def dequantize(q: np.ndarray, spec: TensorSpec) -> np.ndarray:
    """정수 출력 -> float32. 양자화되지 않은 출력이면 float32 복사본."""
    if spec.quantized:
        return (q.astype(np.float32) - spec.zero_point) * np.float32(spec.scale)
    return np.array(q, dtype=np.float32, copy=True)


def to_chw_pixels(y: np.ndarray, layout: str, box_units: str, imgsz: int) -> np.ndarray:
    """출력을 yolo_common.decode가 기대하는 (1, 4+nc, N), 입력 픽셀 좌표로 맞춘다. y는 이미 복사본."""
    if layout == "hwc":
        y = np.ascontiguousarray(y.transpose(0, 2, 1))
    if box_units == "normalized":
        y[:, :4, :] *= imgsz
    return y


# ---- 검출기 ---------------------------------------------------------------

@DETECTORS.register("yolo_tflite")
class YoloTFLiteDetector(Detector):
    def __init__(
        self,
        weights: str,
        delegate: str = "edgetpu",
        manifest: str | None = None,          # 기본: weights와 같은 폴더의 manifest.json
        conf_threshold: float = 0.20,         # 낮게 유지. FSM이 그 위에 person_conf_low를 적용한다
        iou_threshold: float = 0.45,
        class_map: dict[str, str] | None = None,
        classes: list[str] | None = None,
        threads: int = 2,
        delegate_lib: str | None = None,      # 기본: OS별 Edge TPU 라이브러리 이름
        engine: TpuEngine | None = None,      # 테스트용 주입. 보통은 None -> LocalTpuEngine
    ):
        if delegate not in DELEGATES:
            raise ValueError(f"delegate must be one of {DELEGATES}, got {delegate!r}")
        self.weights = weights
        self.delegate = delegate
        self.manifest_path = Path(manifest) if manifest else default_manifest_path(weights)
        self.conf_threshold, self.iou_threshold = conf_threshold, iou_threshold
        self.class_map = class_map or {}
        self.classes = set(classes) if classes else None
        self.engine: TpuEngine = engine or LocalTpuEngine(delegate, threads, delegate_lib)
        self.name = f"yolo_tflite[{delegate}]:{weights}"
        self.last_invoke_ms = 0.0             # invoke만의 시간 (bench·metrics용. infer_ms는 전후처리 포함)
        self._loaded = False

    # -- load: 검사를 모두 통과해야 시작한다 --------------------------------

    def load(self) -> None:
        wpath = Path(self.weights)
        if not wpath.is_file():
            raise FileNotFoundError(f"model weights not found: {wpath}")
        m = self.manifest = ModelManifest.load(self.manifest_path)
        data = wpath.read_bytes()
        self._check_file(m, wpath, data)

        self.engine.load(str(wpath))
        try:
            self._check_runtime_version(m)
            self._check_tensors(m)
        except BaseException:
            # open_all()은 load()가 성공한 뒤에야 close를 등록한다. 여기서 실패하면 Coral 핸들을
            # 직접 닫아야 남지 않는다 (DISCUSSION.md R6-3.2)
            self.engine.close()
            raise

        self._lut = input_lut(self._in) if self._in.quantized else None
        self.names = [self.class_map.get(n, n) for n in m.names]
        self.input_size = m.imgsz
        log.info("loaded %s (manifest %s): input %s %s, output %s, %d classes",
                 wpath, m.id, self._in.shape, self._in.dtype, self._out.shape, len(m.names))
        self._loaded = True

    def _check_file(self, m: ModelManifest, wpath: Path, data: bytes) -> None:
        entry = m.files.get(wpath.name)
        if entry is None:
            raise ManifestError(f"{m.path}: no entry for '{wpath.name}' in files {sorted(m.files)}")
        digest = sha256_bytes(data)
        if digest != entry.sha256:
            raise ManifestError(f"{wpath}: sha256 {digest[:12]}.. does not match manifest {entry.sha256[:12]}.. "
                                "(corrupt copy, or model replaced without its manifest)")
        compiled = is_edgetpu_compiled(data)
        if entry.edgetpu is not None and entry.edgetpu != compiled:
            raise ManifestError(f"{wpath}: manifest says edgetpu={entry.edgetpu}, file says {compiled}")
        if self.delegate == "edgetpu" and not compiled:
            # 미컴파일 모델에 delegate를 붙이면 TPU로 아무것도 안 가고 CPU에서 조용히 돈다 (합의 2)
            raise ManifestError(f"{wpath}: not compiled for Edge TPU (no '{EDGETPU_MARKER.decode()}'); "
                                "run edgetpu_compiler, or use delegate: none for CPU evaluation")
        if self.delegate == "none" and compiled:
            raise ManifestError(f"{wpath}: Edge TPU compiled model cannot run with delegate: none; "
                                "use the uncompiled INT8 .tflite")
        if self.delegate == "edgetpu" and m.tpu_ops == 0:
            raise ManifestError(f"{m.path}: compile.tpu_ops == 0, nothing runs on the Edge TPU")

    def _check_runtime_version(self, m: ModelManifest) -> None:
        """변환 TF가 런타임보다 새것이면 경고만. 실제 opcode 불일치는 allocate_tensors()가 이미 예외로 잡았다
        (DISCUSSION.md R3-4). ai-edge-litert는 버전 체계가 TF와 달라 비교하지 않는다."""
        pkg, ver = self.engine.runtime()
        if m.build_tensorflow is None or pkg not in ("tflite-runtime", "tensorflow"):
            return
        built, run = major_minor(m.build_tensorflow), major_minor(ver)
        if built and run and built > run:
            log.warning("model built with TF %s but runtime is %s %s; newer op versions may misbehave",
                        m.build_tensorflow, pkg, ver)

    def _check_tensors(self, m: ModelManifest) -> None:
        S, nc = m.imgsz, len(m.names)
        self._in = i = self.engine.input_spec()
        if i.shape != (1, S, S, 3):
            raise ManifestError(f"{self.weights}: input shape {i.shape}, expected (1, {S}, {S}, 3) from manifest imgsz")
        if i.dtype not in INPUT_DTYPES:
            raise ManifestError(f"{self.weights}: input dtype {i.dtype} not supported ({INPUT_DTYPES})")
        if np.issubdtype(i.dtype, np.integer) and not i.quantized:
            raise ManifestError(f"{self.weights}: integer input without quantization parameters")
        outs = self.engine.output_specs()
        if len(outs) != 1:
            raise ManifestError(f"{self.weights}: expected 1 output tensor, got {len(outs)}")
        self._out = o = outs[0]
        if np.issubdtype(o.dtype, np.integer) and not o.quantized:
            # 역양자화 없이 int8을 float로 쓰면 박스·신뢰도가 조용히 틀린다 (R6-3.1)
            raise ManifestError(f"{self.weights}: integer output without quantization parameters")
        ch_axis = 1 if m.output_layout == "chw" else 2
        if len(o.shape) != 3 or o.shape[0] != 1 or o.shape[ch_axis] != 4 + nc:
            raise ManifestError(f"{self.weights}: output {o.shape} doesn't match layout '{m.output_layout}' "
                                f"with 4 + {nc} classes")

    # -- 실행 -----------------------------------------------------------------

    def warmup(self) -> None:
        assert self._loaded, "load() first"
        dummy = np.zeros(self._in.shape, self._in.dtype)
        for _ in range(2):      # 첫 invoke는 가중치를 Coral로 올리느라 느리다
            self.engine.invoke(dummy)

    def predict(self, pkt: FramePacket) -> FrameDetections:
        assert self._loaded, "load() first"
        t0 = time.perf_counter()   # 측정용일 뿐, 판단에는 쓰지 않는다
        img = pkt.image
        h, w = img.shape[:2]
        S = self.input_size
        boxed, scale, px, py = letterbox(img, S)
        rgb = boxed[:, :, ::-1]
        x = self._lut[rgb][None] if self._lut is not None else (rgb.astype(np.float32) / 255.0)[None]

        t1 = time.perf_counter()
        (raw,) = self.engine.invoke(x)          # 예외는 그대로 올린다 -> process_frame이 FAULT로 바꾼다
        self.last_invoke_ms = (time.perf_counter() - t1) * 1000

        y = to_chw_pixels(dequantize(raw, self._out), self.manifest.output_layout, self.manifest.box_units, S)
        objs = decode(y, self.names, self.conf_threshold, self.iou_threshold, scale, px, py, w, h)
        if self.classes is not None:
            objs = [o for o in objs if o.cls in self.classes]
        infer_ms = (time.perf_counter() - t0) * 1000
        return FrameDetections(pkt.seq, pkt.timestamp, tuple(objs), self.name, infer_ms)

    def close(self) -> None:
        self.engine.close()
        self._loaded = False

