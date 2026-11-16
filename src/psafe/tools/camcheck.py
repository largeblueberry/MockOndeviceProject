"""카메라 점검 + 영상 녹화. 모델도 판단도 없이 카메라만 본다.

    python -m psafe.tools.camcheck                       # 실시간 화면 + 통계
    python -m psafe.tools.camcheck --record recordings/empty_bay.mp4

health 검사가 이 카메라에서 보게 될 값을 출력한다. config.yaml의 임계값
(health.min_brightness, freeze_diff_threshold)을 추측이 아닌 측정값으로 정하기 위함이다:
    brightness  평균 밝기 (0..255)
    diff        직전 프레임과의 평균 절대 차이 (정적인 장면에서의 센서 노이즈
                수준. freeze_diff_threshold보다 커야 한다)
키: r = 녹화 시작/정지, q / Esc = 종료.
"""

import argparse
import os
import time
from pathlib import Path

import cv2
import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser(prog="camcheck")
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--fps", type=float, default=30)
    ap.add_argument("--record", help="output .mp4 path; starts recording immediately")
    args = ap.parse_args()

    api = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
    cap = cv2.VideoCapture(args.index, api)
    if not cap.isOpened():
        print(f"cannot open camera {args.index}")
        return 1
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    cap.set(cv2.CAP_PROP_FPS, args.fps)

    writer, rec_path = None, args.record
    prev, n, t_last, fps = None, 0, time.monotonic(), 0.0
    diffs: list[float] = []
    try:
        while True:
            ok, img = cap.read()
            if not ok:
                print("read failed")
                break
            small = img[::4, ::4].astype(np.float32).mean(axis=2)  # HealthMonitor와 같은 방식
            bright = float(small.mean())
            diff = float(np.abs(small - prev).mean()) if prev is not None else float("nan")
            prev = small
            if not np.isnan(diff):
                diffs.append(diff)

            n += 1
            now = time.monotonic()
            if now - t_last >= 1.0:
                fps, n, t_last = n / (now - t_last), 0, now
                lo = min(diffs) if diffs else float("nan")
                print(f"fps {fps:5.1f} | brightness {bright:6.1f} | diff now {diff:5.2f} min {lo:5.2f}")
                diffs.clear()

            if rec_path and writer is None:
                Path(rec_path).parent.mkdir(parents=True, exist_ok=True)
                h, w = img.shape[:2]
                writer = cv2.VideoWriter(rec_path, cv2.VideoWriter_fourcc(*"mp4v"), args.fps, (w, h))
                print(f"recording -> {rec_path}")
            if writer is not None:
                writer.write(img)  # 주석 없는 원본 프레임: 테스트 입력으로 쓴다

            view = img.copy()
            label = f"{fps:.0f} fps  bright {bright:.0f}  diff {diff:.2f}" + ("  REC" if writer else "")
            cv2.putText(view, label, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255) if writer else (255, 255, 255), 2)
            cv2.imshow("camcheck", view)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord("r"):
                if writer is not None:
                    writer.release()
                    print(f"saved {rec_path}")
                    writer, rec_path = None, None
                else:
                    rec_path = f"recordings/clip_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    finally:
        if writer is not None:
            writer.release()
            print(f"saved {rec_path}")
        cap.release()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
