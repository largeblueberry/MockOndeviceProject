"""TFLite 실행 엔진 경계 (docs/CORAL_DESIGN.md §3, docs/edge_tpu/DISCUSSION.md 합의 1).

YoloTFLiteDetector는 TFLite 패키지를 직접 import하지 않고 `TpuEngine`만 쓴다. 그래서
- 실제 실행: LocalTpuEngine (이 파일). 같은 프로세스에서 TFLite Interpreter + (선택) Edge TPU delegate
- 단위 테스트: tests의 FakeTpuEngine (HW, TFLite 패키지 없이)
- 환경 분리가 필요해지면: 자식 프로세스로 보내는 엔진을 같은 경계에 추가
를 Detector 수정 없이 바꿔 끼울 수 있다.

Pi와 Coral 사이에서 실제로 오가는 것은 invoke()의 입력·출력 배열뿐이다 (docs/edge_tpu/troubleshooting.md Q3, Q4).
"""

from __future__ import annotations

import logging
import platform
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Callable, Protocol

import numpy as np

log = logging.getLogger("psafe.detector.tpu")

DELEGATES = ("edgetpu", "none")

# OS별 Edge TPU 런타임 라이브러리 이름 (Coral 문서)
EDGETPU_LIB = {"Linux": "libedgetpu.so.1", "Darwin": "libedgetpu.1.dylib", "Windows": "edgetpu.dll"}


@dataclass(frozen=True, slots=True)
class TensorSpec:
    shape: tuple[int, ...]
    dtype: np.dtype
    scale: float = 0.0       # 양자화 scale. 0이면 양자화되지 않은(float) 텐서
    zero_point: int = 0

    @property
    def quantized(self) -> bool:
        return self.scale != 0.0 and np.issubdtype(self.dtype, np.integer)


class TpuEngine(Protocol):
    """텐서를 넣고 텐서를 받는 것만 안다. 이미지·박스·사람은 모른다."""

    def load(self, model_path: str) -> None: ...
    def input_spec(self) -> TensorSpec: ...
    def output_specs(self) -> tuple[TensorSpec, ...]: ...
    def invoke(self, inp: np.ndarray) -> tuple[np.ndarray, ...]: ...
    def runtime(self) -> tuple[str, str]: ...   # (패키지 이름, 버전), 로그·버전 검사용
    def close(self) -> None: ...


# ---- 인터프리터 패키지 찾기 -------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Provider:
    name: str                                   # 배포 패키지 이름 (버전 조회용)
    interpreter: Callable[..., Any]
    load_delegate: Callable[..., Any]


def _find_provider() -> _Provider:
    """환경마다 TFLite를 주는 패키지가 다르다. 그 차이를 여기 한 곳에 가둔다.

    순서: tflite_runtime (Pi 표준) -> ai_edge_litert (tflite 후속, 최신 Python) -> tensorflow (PC/Colab).
    """
    try:
        from tflite_runtime.interpreter import Interpreter, load_delegate
        return _Provider("tflite-runtime", Interpreter, load_delegate)
    except ImportError:
        pass
    try:
        from ai_edge_litert.interpreter import Interpreter, load_delegate
        return _Provider("ai-edge-litert", Interpreter, load_delegate)
    except ImportError:
        pass
    try:
        import tensorflow as tf
        return _Provider("tensorflow", tf.lite.Interpreter, tf.lite.experimental.load_delegate)
    except ImportError:
        pass
    raise ImportError("no TFLite interpreter found: install tflite-runtime, ai-edge-litert or tensorflow "
                      "(see docs/CORAL_DESIGN.md §2)")


def _dist_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "?"


def _spec(detail: dict) -> TensorSpec:
    """양자화 값은 quantization_parameters(현행)를 먼저 읽고, 없으면 quantization(구식) 필드를 쓴다.
    per-channel(scale 여러 개) 텐서는 우리 역양자화가 다루지 않으므로 조용히 넘기지 않고 거부한다.
    구식 필드는 per-channel일 때 (0.0, 0)이라, 그것만 읽으면 int8을 역양자화 없이 쓰게 된다 (DISCUSSION.md R6-3.1)."""
    shape = tuple(int(d) for d in detail["shape"])
    qp = detail.get("quantization_parameters") or {}
    scales = np.atleast_1d(np.asarray(qp.get("scales", []), dtype=np.float64))
    zps = np.atleast_1d(np.asarray(qp.get("zero_points", []), dtype=np.int64))
    if scales.size > 1:
        raise ValueError(f"tensor '{detail.get('name', '?')}' {shape}: per-channel quantization "
                         f"({scales.size} scales) is not supported for model inputs/outputs")
    if scales.size == 1:
        scale, zp = float(scales[0]), int(zps[0]) if zps.size else 0
    else:
        scale, zp = detail.get("quantization", (0.0, 0))
    return TensorSpec(shape, np.dtype(detail["dtype"]), float(scale), int(zp))


class LocalTpuEngine:
    """같은 프로세스 안의 TFLite Interpreter.

    delegate="edgetpu": Edge TPU delegate를 붙인다. 붙이지 못하면 예외. **CPU로 조용히 대체하지 않는다**
    (CPU INT8은 수백 ms라 stale_frame FAULT만 반복되고 원인이 숨는다. 합의 2).
    delegate="none": CPU 실행. 컴파일 전 INT8 모델을 PC에서 평가·테스트할 때만 쓴다.
    """

    def __init__(self, delegate: str = "edgetpu", threads: int = 2, delegate_lib: str | None = None):
        if delegate not in DELEGATES:
            raise ValueError(f"delegate must be one of {DELEGATES}, got {delegate!r}")
        self.delegate = delegate
        self.threads = threads
        self.delegate_lib = delegate_lib or EDGETPU_LIB.get(platform.system(), "libedgetpu.so.1")
        self._itp = None
        self._provider: _Provider | None = None

    def load(self, model_path: str) -> None:
        self._provider = p = _find_provider()
        delegates = []
        if self.delegate == "edgetpu":
            try:
                delegates.append(p.load_delegate(self.delegate_lib))
            except Exception as e:      # 패키지마다 예외 타입이 달라 (ValueError, OSError, RuntimeError) 넓게 잡는다
                raise RuntimeError(
                    f"Edge TPU delegate '{self.delegate_lib}' failed to load via {p.name}: {e}. "
                    "Is the Coral connected and the Edge TPU runtime installed? (no CPU fallback by design)"
                ) from e
        itp = p.interpreter(model_path=model_path, experimental_delegates=delegates, num_threads=self.threads)
        itp.allocate_tensors()
        self._itp = itp
        self._in = itp.get_input_details()
        self._out = itp.get_output_details()
        if len(self._in) != 1:
            raise ValueError(f"{model_path}: expected 1 input tensor, got {len(self._in)}")
        log.info("tflite loaded %s via %s %s, delegate=%s", model_path, p.name, _dist_version(p.name), self.delegate)

    def input_spec(self) -> TensorSpec:
        return _spec(self._in[0])

    def output_specs(self) -> tuple[TensorSpec, ...]:
        return tuple(_spec(d) for d in self._out)

    def invoke(self, inp: np.ndarray) -> tuple[np.ndarray, ...]:
        assert self._itp is not None, "load() first"
        self._itp.set_tensor(self._in[0]["index"], inp)
        self._itp.invoke()      # 동기 호출: Coral(또는 CPU)이 끝날 때까지 이 스레드가 기다린다
        return tuple(self._itp.get_tensor(d["index"]) for d in self._out)   # get_tensor는 복사본을 준다

    def runtime(self) -> tuple[str, str]:
        if self._provider is None:
            return ("?", "?")
        return (self._provider.name, _dist_version(self._provider.name))

    def close(self) -> None:
        self._itp = None
