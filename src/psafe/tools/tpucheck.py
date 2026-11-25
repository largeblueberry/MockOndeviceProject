"""Coral(Edge TPU) 설치 점검. 설치 직후 한 번 돌려 어디서 막히는지 한 줄씩 보여 준다.

    python -m psafe.tools.tpucheck                                    # 환경만 (모델 없이)
    python -m psafe.tools.tpucheck --weights models/y8n_320/model_edgetpu.tflite
    python -m psafe.tools.tpucheck --weights models/y8n_320/model_int8.tflite --delegate none   # Coral 없이

점검 순서 (앞에서 실패하면 멈춘다. 뒤 항목은 앞 항목을 전제로 하기 때문)
  1. Python, NumPy 버전
  2. TFLite 인터프리터 패키지 (tflite-runtime / ai-edge-litert / tensorflow 중 무엇이 잡히나)
  3. Coral USB 장치가 보이나 (Linux: /sys/bus/usb, 그 외 OS는 건너뜀)          [edgetpu]
  4. Edge TPU delegate 로드 (= tflite 패키지와 libedgetpu가 맞물리나)          [edgetpu]
  5. 모델 load(): manifest, 해시, 컴파일 여부, 입출력 텐서 검사
  6. invoke 반복: p50 / p95 지연

종료 코드: 0 = 모두 통과, 1 = 실패 항목 있음.
"""

from __future__ import annotations

import argparse
import platform
import statistics
import sys
from pathlib import Path

import numpy as np

CORAL_USB_IDS = {("1a6e", "089a"): "Coral (firmware not loaded yet)", ("18d1", "9302"): "Coral (ready)"}


def ok(msg: str) -> None:
    print(f"[ok]   {msg}", flush=True)


def fail(msg: str) -> int:
    print(f"[FAIL] {msg}", flush=True)
    return 1


def skip(msg: str) -> None:
    print(f"[skip] {msg}", flush=True)


def find_coral_usb() -> list[str] | None:
    """Linux에서 연결된 Coral을 찾는다. 다른 OS는 None (확인 불가)."""
    root = Path("/sys/bus/usb/devices")
    if platform.system() != "Linux" or not root.is_dir():
        return None
    found = []
    for dev in root.iterdir():
        try:
            vid = (dev / "idVendor").read_text().strip().lower()
            pid = (dev / "idProduct").read_text().strip().lower()
        except OSError:
            continue
        if (vid, pid) in CORAL_USB_IDS:
            found.append(f"{dev.name} {vid}:{pid} {CORAL_USB_IDS[(vid, pid)]}")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m psafe.tools.tpucheck")
    ap.add_argument("--weights", help="점검할 .tflite (옆에 manifest.json 필요)")
    ap.add_argument("--delegate", choices=["edgetpu", "none"], default="edgetpu")
    ap.add_argument("--delegate-lib", help="기본: OS별 이름 (libedgetpu.so.1 / edgetpu.dll / libedgetpu.1.dylib)")
    ap.add_argument("--frames", type=int, default=50)
    args = ap.parse_args(argv)

    from psafe.detectors import tpu_runtime

    # 1
    ok(f"python {platform.python_version()} ({platform.system()} {platform.machine()}), numpy {np.__version__}")

    # 2
    try:
        prov = tpu_runtime._find_provider()
    except ImportError as e:
        return fail(str(e))
    ok(f"interpreter: {prov.name} {tpu_runtime._dist_version(prov.name)}")

    # 3, 4
    if args.delegate == "edgetpu":
        usb = find_coral_usb()
        if usb is None:
            skip(f"USB device check not supported on {platform.system()}")
        elif not usb:
            return fail("no Coral USB device found (check cable / port; lsusb should show 1a6e:089a or 18d1:9302)")
        else:
            ok("USB: " + "; ".join(usb))

        lib = args.delegate_lib or tpu_runtime.EDGETPU_LIB.get(platform.system(), "libedgetpu.so.1")
        try:
            prov.load_delegate(lib)
        except Exception as e:
            return fail(f"load_delegate('{lib}') via {prov.name}: {e}\n"
                        "       -> Edge TPU runtime missing, or it does not match this TFLite package "
                        "(docs/edge_tpu/DISCUSSION.md R3 pair 1)")
        ok(f"load_delegate('{lib}')")
    else:
        skip("delegate: none (CPU only, Coral not used)")

    if not args.weights:
        skip("no --weights: model checks skipped")
        return 0

    # 5
    from psafe.core.packets import FramePacket
    from psafe.detectors.yolo_tflite import YoloTFLiteDetector

    det = YoloTFLiteDetector(weights=args.weights, delegate=args.delegate, delegate_lib=args.delegate_lib)
    try:
        det.load()
    except Exception as e:
        return fail(f"load {args.weights}: {type(e).__name__}: {e}")
    m = det.manifest
    ok(f"manifest {m.id}: imgsz {m.imgsz}, {len(m.names)} classes, box_units {m.box_units}, "
       f"build TF {m.build_tensorflow or '?'}")
    i, o = det._in, det._out
    ok(f"tensors: in {list(i.shape)} {i.dtype} q{(i.scale, i.zero_point) if i.quantized else ''} | "
       f"out {list(o.shape)} {o.dtype}")

    # 6
    try:
        det.warmup()
        img = np.random.default_rng(0).integers(0, 256, (480, 640, 3), dtype=np.uint8)
        inv, tot = [], []
        for k in range(args.frames):
            d = det.predict(FramePacket(k, 0.0, "tpucheck", img))
            inv.append(det.last_invoke_ms)
            tot.append(d.infer_ms)
    except Exception as e:
        return fail(f"invoke: {type(e).__name__}: {e}")
    finally:
        det.close()
    inv.sort()
    tot.sort()
    ok(f"invoke x{args.frames}: p50 {statistics.median(inv):.1f} ms, p95 {inv[int(0.95 * (len(inv) - 1))]:.1f} ms "
       f"| predict (pre+invoke+post) p50 {statistics.median(tot):.1f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
