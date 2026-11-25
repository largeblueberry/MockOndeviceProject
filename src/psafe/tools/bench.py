"""검출기 추론 속도 측정 (EXP-005). 카메라 없이, 파이프라인과 같은 predict() 경로를 잰다.

    python -m psafe.tools.bench                                  # models/ 아래 320, 640 모델 x 스레드 1/2/4
    python -m psafe.tools.bench --weights models/yolo11n_320.onnx --threads 4 --frames 200
    python -m psafe.tools.bench --weights models/y8n_320/model_edgetpu.tflite --threads 2   # Coral
    python -m psafe.tools.bench --weights models/y8n_320/model_int8.tflite --delegate none  # CPU INT8

predict() = letterbox 전처리 + 추론(ONNX / TFLite·Coral) + 후처리(NMS) 전체 시간을 잰다.
.tflite는 invoke() 시간만 따로 잰 열(inv)을 함께 보여 준다: Coral이 느린지, Pi 전후처리가 느린지 구분용.
입력은 640x480 노이즈 이미지다. 추론 시간은 영상 내용과 거의 무관하다
(검출 개수가 많으면 NMS가 조금 늘어나는 정도).

출력의 "Y까지 최소 시간"은 clear_min_frames(10장) 조건만 따졌을 때 Y가 나올 수 있는
가장 빠른 시간이다. 실제로는 clear_time_s(3초)와 둘 중 긴 쪽이 적용된다.
"""

import argparse
import platform
import statistics
import sys
import time
from pathlib import Path

import numpy as np

from psafe.core.packets import FramePacket
from psafe.detectors.detector_api import Detector
from psafe.detectors.yolo_onnx import YoloOnnxDetector
from psafe.detectors.yolo_tflite import YoloTFLiteDetector

CLEAR_MIN_FRAMES = 10


def make_detector(weights: str, threads: int, delegate: str = "edgetpu") -> Detector:
    if weights.endswith(".tflite"):
        return YoloTFLiteDetector(weights=weights, delegate=delegate, threads=threads)
    return YoloOnnxDetector(weights=weights, threads=threads)


def bench(weights: str, threads: int, frames: int, warmup: int = 5, delegate: str = "edgetpu") -> dict:
    det = make_detector(weights, threads, delegate)
    det.load()
    det.warmup()
    rng = np.random.default_rng(0)
    img = rng.integers(0, 256, (480, 640, 3), dtype=np.uint8)
    times, invokes = [], []
    for i in range(warmup + frames):
        d = det.predict(FramePacket(i, 0.0, "bench", img))
        if i >= warmup:
            times.append(d.infer_ms)
            if isinstance(det, YoloTFLiteDetector):
                invokes.append(det.last_invoke_ms)
    det.close()
    times.sort()
    p50 = statistics.median(times)
    return {
        "weights": Path(weights).name, "input": det.input_size, "threads": threads,
        "mean": statistics.fmean(times), "p50": p50,
        "p95": times[int(0.95 * (len(times) - 1))], "max": times[-1],
        "fps": 1000.0 / p50,
        "invoke_p50": statistics.median(invokes) if invokes else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m psafe.tools.bench")
    ap.add_argument("--weights", nargs="*", help="기본값: models/yolo11n_320.onnx, models/yolo11n_640.onnx")
    ap.add_argument("--threads", type=int, nargs="*", default=[1, 2, 4])
    ap.add_argument("--frames", type=int, default=100)
    ap.add_argument("--delegate", choices=["edgetpu", "none"], default="edgetpu", help=".tflite 전용")
    args = ap.parse_args(argv)

    weights = args.weights or [str(p) for p in sorted(Path("models").glob("yolo11n_*.onnx"))]
    if not weights:
        print("모델 파일이 없습니다 (docs/DESIGN.md §14.1)")
        return 2
    print(f"{platform.machine()} | python {platform.python_version()} | frames {args.frames}")
    print(f"{'model':22s} {'in':>4s} {'thr':>3s} {'mean':>7s} {'p50':>7s} {'p95':>7s} {'max':>7s} {'fps':>6s} {'inv':>6s}  Y까지 최소")
    for w in weights:
        for t in args.threads:
            r = bench(w, t, args.frames, delegate=args.delegate)
            min_y = CLEAR_MIN_FRAMES / r["fps"]
            inv = f"{r['invoke_p50']:6.1f}" if r["invoke_p50"] is not None else f"{'-':>6s}"
            print(f"{r['weights']:22s} {r['input']:4d} {r['threads']:3d} {r['mean']:7.1f} {r['p50']:7.1f} "
                  f"{r['p95']:7.1f} {r['max']:7.1f} {r['fps']:6.1f} {inv}  {min_y:.1f} s", flush=True)
            time.sleep(1.0)   # 연속 측정 사이 발열 완화
    return 0


if __name__ == "__main__":
    sys.exit(main())
