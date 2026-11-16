"""검출기 추론 속도 측정 (EXP-005). 카메라 없이, 파이프라인과 같은 predict() 경로를 잰다.

    python -m psafe.tools.bench                                  # models/ 아래 320, 640 모델 x 스레드 1/2/4
    python -m psafe.tools.bench --weights models/yolo11n_320.onnx --threads 4 --frames 200

predict() = letterbox 전처리 + ONNX 추론 + 후처리(NMS) 전체 시간을 잰다.
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
from psafe.detectors.yolo_onnx import YoloOnnxDetector

CLEAR_MIN_FRAMES = 10


def bench(weights: str, threads: int, frames: int, warmup: int = 5) -> dict:
    det = YoloOnnxDetector(weights=weights, threads=threads)
    det.load()
    det.warmup()
    rng = np.random.default_rng(0)
    img = rng.integers(0, 256, (480, 640, 3), dtype=np.uint8)
    times = []
    for i in range(warmup + frames):
        d = det.predict(FramePacket(i, 0.0, "bench", img))
        if i >= warmup:
            times.append(d.infer_ms)
    det.close()
    times.sort()
    p50 = statistics.median(times)
    return {
        "weights": Path(weights).name, "input": det.input_size, "threads": threads,
        "mean": statistics.fmean(times), "p50": p50,
        "p95": times[int(0.95 * (len(times) - 1))], "max": times[-1],
        "fps": 1000.0 / p50,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m psafe.tools.bench")
    ap.add_argument("--weights", nargs="*", help="기본값: models/yolo11n_320.onnx, models/yolo11n_640.onnx")
    ap.add_argument("--threads", type=int, nargs="*", default=[1, 2, 4])
    ap.add_argument("--frames", type=int, default=100)
    args = ap.parse_args(argv)

    weights = args.weights or [str(p) for p in sorted(Path("models").glob("yolo11n_*.onnx"))]
    if not weights:
        print("모델 파일이 없습니다 (docs/DESIGN.md §14.1)")
        return 2
    print(f"{platform.machine()} | python {platform.python_version()} | frames {args.frames}")
    print(f"{'model':22s} {'in':>4s} {'thr':>3s} {'mean':>7s} {'p50':>7s} {'p95':>7s} {'max':>7s} {'fps':>6s}  Y까지 최소")
    for w in weights:
        for t in args.threads:
            r = bench(w, t, args.frames)
            min_y = CLEAR_MIN_FRAMES / r["fps"]
            print(f"{r['weights']:22s} {r['input']:4d} {r['threads']:3d} {r['mean']:7.1f} {r['p50']:7.1f} "
                  f"{r['p95']:7.1f} {r['max']:7.1f} {r['fps']:6.1f}  {min_y:.1f} s", flush=True)
            time.sleep(1.0)   # 연속 측정 사이 발열 완화
    return 0


if __name__ == "__main__":
    sys.exit(main())
