# Coral Edge TPU 소프트웨어 설계

2026-11-25 초안. HW(Coral USB) 도착 전, SW 구조와 파이프라인을 먼저 정하기 위한 문서입니다.
조사 근거(변환 방법, 라이브러리 버전 상황, YOLO11 C2PSA 위험)는 [edge_tpu_research.md](edge_tpu_research.md)에 있고, 이 문서는 그 조사 위에서 **"그래서 우리 코드를 어떻게 짤 것인가"**를 다룹니다.

> 표기: ✅ 확인됨 (문서 또는 우리 실험) · ⚠️ 미검증 가정 (Coral 도착 후 확인 필요)

---

## 0. 한 장 요약

```
 OFFLINE: x86 PC / Colab                         RUNTIME: Raspberry Pi (+ Coral USB)
 (model build, once per model)                   (every frame)

 ┌─────────┐  ┌────────────┐  ┌────────────┐        ┌──────────────────────────────────┐
 │ train   │─▶│ export     │─▶│ edgetpu_   │        │ psafe node process (Python 3.12) │
 │ .pt     │  │ INT8       │  │ compiler -s│        │                                  │
 └─────────┘  │ .tflite    │  └─────┬──────┘        │  camera ─▶ health ─▶ Detector ─▶ FSM ─▶ UDP ─▶ hub C
              └─────▲──────┘        │               │                       │          │
   calib images ────┘               ▼               │          ┌────────────▼───────┐  │
   (normal + in-car)     ┌─────────────────────┐    │          │ YoloTFLiteDetector │  │
                         │ eval gate           │    │          │  pre  (Pi CPU)     │  │
                         │  - TPU op mapping   │    │          │  invoke ──USB──────┼──┼──▶ [Coral]
                         │  - INT8 recall      │    │          │  post (Pi CPU)     │  │
                         │    normal / in-car  │    │          └────────────────────┘  │
                         └──────────┬──────────┘    └──────────────────────────────────┘
                                    │ pass                        ▲
                                    ▼                             │ load at start
                         ┌──────────────────────┐   scp           │
                         │ models/<id>/         │─────────────────┘
                         │  model_edgetpu.tflite│
                         │  manifest.json       │  ◀── the contract between the two worlds
                         └──────────────────────┘
```

핵심 결정 다섯 가지:

1. **Coral은 Detector 한 개의 구현 세부사항이다.** FSM, CONFIRM, 허브, 프로토콜은 한 줄도 바꾸지 않는다. config의 `model:`만 바꾼다.
2. **코드는 하나, 환경은 노드별.** Coral이 붙은 Pi는 앱 전체를 Python 3.12 venv에서 돌리고, 나머지는 3.13 그대로. Pi 사이에는 UDP+JSON만 오가므로 Python 버전이 달라도 상관없다.
3. **모델 파일 + `manifest.json`이 오프라인과 런타임 사이의 유일한 계약.** 버전 짝 맞추기는 이 계약을 load() 시점에 검사하는 것으로 해결한다.
4. **조용한 대체 금지.** Coral을 못 쓰면 CPU로 몰래 넘어가지 않고 에러 → 기존 FAULT/timeout 경로로 N.
5. **HW 없이 할 수 있는 것은 지금 다 한다.** Coral 없이도 INT8 `.tflite`는 CPU에서 같은 코드 경로로 돌아가므로, Detector·평가·변환 파이프라인을 미리 완성할 수 있다.

---

## 1. 풀어야 할 문제

| # | 문제 | 왜 문제인가 |
|---|---|---|
| P1 | 실행 위치가 둘로 나뉜다 | Coral은 행렬 연산만 한다. 카메라·전처리·후처리·판단은 Pi가 한다. 둘의 경계를 코드에서 명확히 해야 한다 |
| P2 | 환경 버전이 서로 다르다 | Pi 메인 venv는 Python 3.13인데 Coral용 Python 런타임(`tflite-runtime`)은 3.12까지 ✅. 컴파일 환경(x86)은 또 다르다 |
| P3 | 변환된 모델을 믿을 수 있는가 | INT8 양자화로 차 안 승객 recall이 떨어질 수 있고, 미지원 연산(YOLO11 C2PSA)이 있으면 Coral을 붙여도 안 빨라질 수 있다 ⚠️ |
| P4 | Coral이 고장 나면? | USB 빠짐, delegate 로드 실패, 추론 멈춤. 안전 시스템이므로 어떤 경우에도 "Y(비었음)"가 나가면 안 된다 |

이 문서의 각 절이 이 문제에 대응합니다: P1 → §2, P2 → §3, P3 → §4, P4 → §2.5.

---

## 2. 런타임 설계 (Pi 안에서 일어나는 일)

### 2.1 Pi와 Coral의 역할 분담

```
 one frame, inside the psafe process on the Pi

  Pi CPU                                             Coral (USB)
  ─────────────────────────────────────────────      ─────────────────────
  1. read frame (camera thread)
  2. health check (brightness / freeze)
  3. letterbox  640x480 -> 320x320
  4. BGR->RGB, /255, quantize  -> int8 NHWC
  5. interpreter.invoke() ─── input tensor ───────▶  6. conv layers
                                                        (ops mapped to TPU)
                          ◀── output tensor ──────
  7. (CPU fallback ops, if any after the split)
  8. dequantize -> float
  9. decode boxes + NMS + class_map
 10. FSM step -> Decision -> Report to hub
```

- Coral에는 **가중치가 load() 때 한 번 올라가고**, 매 프레임에는 입력/출력 텐서만 USB로 오갑니다 ✅ (Coral 문서).
- 7번이 생기면(그래프가 TPU/CPU로 나뉘면) 느려집니다. 컴파일 로그로 미리 알 수 있으므로 §4의 평가 게이트에서 거릅니다.
- 3·4·8·9는 지금 ONNX 경로에도 있는 일이라 **기존 코드를 그대로 재사용**합니다. Coral 때문에 새로 생기는 건 4의 quantize, 8의 dequantize, 5의 delegate뿐입니다.

### 2.2 모듈 구조

지금 `yolo_onnx.py` 하나에 "YOLO 전처리/후처리"와 "ONNX 실행"이 같이 들어 있습니다. 이것을 둘로 나눠, YOLO 공통 부분을 두 런타임이 같이 쓰게 합니다.

```
 src/psafe/detectors/
 ├── detector_api.py      Detector (ABC), DETECTORS registry        (unchanged)
 ├── yolo_common.py       NEW  letterbox, decode, nms, names, class_map   <- moved out of yolo_onnx.py
 │                             quantize(x, scale, zp) / dequantize(q, scale, zp)
 ├── yolo_onnx.py         YoloOnnxDetector    "yolo_onnx"   (imports yolo_common; behavior unchanged)
 ├── yolo_tflite.py       NEW  YoloTFLiteDetector  "yolo_tflite"
 │                             options: delegate = "edgetpu" | "none"
 ├── tflite_compat.py     NEW  load_interpreter(path, delegate, threads)
 │                             hides WHICH python package provides the interpreter
 └── dummy.py                                                       (unchanged)

            Detector (ABC)
           /      |       \
   Dummy   YoloOnnx   YoloTFLite ──uses──▶ tflite_compat ──▶ tflite_runtime | ai_edge_litert | tensorflow.lite
              \          /                                        └─ load_delegate("libedgetpu.so.1")
               yolo_common
```

왜 `yolo_tflite`가 `delegate: none`도 받는가:
- **Coral 없이 같은 코드 경로를 PC에서 시험할 수 있습니다.** 컴파일 전 INT8 `.tflite`를 CPU로 돌리면 양자화 정확도까지 똑같이 재현됩니다 (Edge TPU는 INT8 연산을 그대로 수행하므로) ⚠️ 수치가 비트 단위로 같은지는 도착 후 비교.
- 배포 config에서는 항상 `delegate: edgetpu`로 둡니다. `none`은 개발·평가용입니다.

왜 `tflite_compat`를 따로 두는가 (P2의 핵심):
- TFLite 인터프리터를 주는 패키지가 환경마다 다릅니다: Pi 3.12 venv는 `tflite_runtime`, 3.13에서 시도할 B안은 `ai_edge_litert`, PC/Colab은 `tensorflow`. **이 차이를 한 파일 안에 가두면** Detector 코드는 환경을 몰라도 됩니다.
- 순서대로 import를 시도하고, 무엇을 썼는지 로그에 남깁니다. delegate 라이브러리 이름도 OS별로 여기서 고릅니다 (Linux `libedgetpu.so.1`).

### 2.3 `YoloTFLiteDetector` 동작

**load()** — 실패하면 전부 예외 (노드가 시작되지 않음 → 허브는 이 노드를 timeout으로 봄 → EMPTY 불가)

1. `manifest.json`을 읽는다 (§4.2). 없으면 에러.
2. `tflite_compat.load_interpreter(weights, delegate, threads)`
   - `delegate: edgetpu`인데 delegate 로드 실패 → **에러** (CPU로 대체하지 않음).
   - 파일 이름이 `_edgetpu.tflite`인데 `delegate: none` → 에러 (Edge TPU custom op은 CPU에서 못 돈다).
3. 입력 텐서 검사: shape `[1, S, S, 3]`, dtype(int8/uint8/float32), `(scale, zero_point)`를 **모델에서 읽는다** (하드코딩 금지). `S`가 manifest의 `imgsz`와 다르면 에러.
4. 출력 텐서 검사: `(1, 4+nc, N)` 형태인지, `nc`가 manifest의 클래스 수와 같은지. 다르면 에러 (지금 ONNX판과 같은 방식).
5. 출력 박스 좌표 기준 확인: Ultralytics TFLite 출력은 좌표가 0~1로 정규화돼 있을 수 있음 ⚠️ → manifest의 `box_units: "pixel" | "normalized"`로 받고, decode 전에 픽셀 기준으로 맞춘다. 첫 구현 때 ONNX판과 결과를 비교해 확정.
6. 런타임 버전 기록: 인터프리터 패키지 이름·버전, libedgetpu 로드 여부를 로그로 남긴다.

**warmup()** — 0 입력으로 2회 invoke. 첫 invoke는 가중치 전송 때문에 느리므로 반드시 필요.

**predict(pkt)**
```
letterbox -> RGB -> /255 -> quantize(in_scale, in_zp) -> set_tensor -> invoke
-> get_tensor -> dequantize(out_scale, out_zp) -> (normalized? * S) -> decode/NMS -> class_map/classes
-> FrameDetections(..., model=self.name, infer_ms)
```
`infer_ms`는 지금처럼 predict 전체(전처리+추론+후처리)를 잽니다. 추가로 invoke만 따로 잰 값을 metrics 로그에 남겨, "Coral이 느린지, Pi 전후처리가 느린지"를 구분할 수 있게 합니다.

### 2.4 config 예시

```yaml
# config/node_a_tpu.yaml (Coral이 붙은 노드)
model:
  name: yolo_tflite
  options:
    weights: models/y8n_320_incar_v1/model_edgetpu.tflite   # 같은 폴더의 manifest.json을 읽음
    delegate: edgetpu        # 배포는 항상 edgetpu. PC 평가용은 none + 컴파일 전 .tflite
    conf_threshold: 0.20
    threads: 2               # TPU로 안 간 연산·후처리용 CPU 스레드
    # class_map: { occupant: person }
```

ONNX ↔ Coral 전환은 이 블록 교체뿐입니다. FSM 임계값(`person_conf_low` 등)은 **모델이 바뀌면 다시 맞춰야 할 수 있습니다** (INT8은 confidence 분포가 조금 달라짐 ⚠️). 그래서 평가 게이트(§4.3)에서 같은 임계값으로 recall을 봅니다.

### 2.5 고장 처리 (P4)

새 안전 장치를 만들기보다, **이미 있는 장치가 각 고장을 잡는지** 확인하는 방식으로 설계했습니다.

| 고장 | 어떻게 드러나나 | 잡는 장치 (기존) | 결과 |
|---|---|---|---|
| delegate 로드 실패 / Coral 미연결 | load() 예외 | 프로세스 시작 실패 → 허브의 `node_timeout_ms` | 그 노드 timeout → C는 EMPTY 못 냄 ✅ |
| 실행 중 USB 빠짐 (예외 발생) | invoke() 예외 | `process_frame`의 `detector_error` → FSM FAULT | 그 노드 N ✅ |
| 실행 중 invoke가 멈춤 (hang) | 추론 스레드 정지 | 하트비트 스레드의 `max_decision_age_ms` | stale N 계속 전송 ✅ |
| 느려짐 (발열, CPU 대체 연산) | infer_ms 증가 | `max_frame_age_ms` → `stale_frame` FAULT | N, 로그에 원인 ✅ |
| 모델·런타임 짝 불일치 | load() 예외 (opcode 등) | 위 첫 줄과 같음 | timeout ✅ |

USB가 빠졌을 때 예외가 나는지, 멈추는지는 모릅니다 ⚠️. **둘 다 N이 되도록** 이미 덮여 있습니다.

**새로 필요한 것 하나: 복구.** 위 경우 모두 안전하게 N이 되지만, hang은 프로세스를 재시작하기 전까지 영원히 N입니다 (가용성 문제). 제안:
- runner에 `fatal_stale_s` 옵션 추가: 판단이 이 시간(예: 10 s) 넘게 갱신되지 않으면 프로세스를 non-zero로 종료.
- Pi에서는 systemd `Restart=always`로 재시작 → load()가 Coral을 다시 잡음. Coral이 아직 없으면 load 실패 → 재시작 반복 (그동안 허브는 timeout으로 봄).
- Detector 안에서 인터프리터를 재생성하는 복잡한 복구는 MVP에서 하지 않습니다.

---

## 3. 환경 설계 (P2: 버전이 다른 환경을 어떻게 맞추나)

### 3.1 환경은 세 개, 맞춰야 할 경계는 두 개

```
  ┌──────────────────────────┐   file (.tflite + manifest)   ┌──────────────────────────┐
  │ (1) BUILD   x86 / Colab  │ ════════════════════════════▶ │ (2) Coral node  Pi       │
  │  ultralytics, tensorflow │   boundary X:                 │  Python 3.12 venv        │
  │  edgetpu_compiler        │   TF / compiler versions      │  tflite-runtime 2.17.x   │
  └──────────────────────────┘   checked via manifest        │  libedgetpu (same TF)    │
                                                             │  numpy < 2               │
                                                             └────────────┬─────────────┘
                                                                          │ UDP + JSON
                                                              boundary Y: │ protocol v2 only
                                                                          │ (python version irrelevant)
                                                             ┌────────────▼─────────────┐
                                                             │ (3) other nodes / hub    │
                                                             │  Python 3.13 venv        │
                                                             │  onnxruntime, numpy 2    │
                                                             └──────────────────────────┘

           same git commit of psafe runs on (2) and (3)
```

- **경계 X (빌드 ↔ Coral 노드):** 넘어가는 건 파일뿐입니다. 맞춰야 할 것은 (a) `edgetpu_compiler` 버전 ↔ libedgetpu 런타임 버전, (b) 변환에 쓴 TF 버전 ↔ Pi의 tflite-runtime 버전 (변환 쪽이 너무 새것이면 런타임이 연산 버전을 못 읽을 수 있음). → 빌드 때 이 버전들을 manifest에 기록하고, load()에서 런타임 버전과 비교해 경고/에러.
- **경계 Y (노드 ↔ 노드):** 프로토콜 v2(UDP+JSON+CRC)만 오갑니다. Python·numpy 버전이 달라도 영향 없습니다. 이미 설계돼 있어서 할 일 없음.
- **같은 코드가 (2)와 (3)에서 돈다:** 이게 "통일"의 실제 의미입니다. Python을 하나로 맞추는 게 아니라, **코드가 두 환경 모두에서 돌도록 보장**합니다.

### 3.2 "같은 코드, 다른 환경"을 보장하는 장치

| 장치 | 내용 |
|---|---|
| `requires-python = ">=3.11"` | 이미 그렇게 돼 있음 ✅. 3.12와 3.13 모두 허용 |
| 의존성 extras | `pyproject.toml`에 `tflite = ["tflite-runtime"]` 추가 (`onnx` extra와 같은 방식). Coral 노드는 `pip install -e .[tflite]`, 일반 노드는 `.[onnx]` |
| 지연 import | 런타임 패키지는 `load()` 안에서만 import (지금 `yolo_onnx`가 하는 방식). 그래서 onnxruntime 없는 Coral 노드, tflite 없는 일반 노드 모두 `import psafe`가 된다 |
| constraints 파일 | `requirements/tpu-py312.txt`: `tflite-runtime==2.17.1`, `numpy<2`, 맞는 `opencv-python-headless` 버전 고정. Coral 노드 설치 시 `-c`로 사용. libedgetpu .deb 파일명도 주석으로 기록 |
| 두 환경 테스트 | PC에 `.venv-np1`(Python 3.12 + numpy<2)을 하나 더 만들어 `pytest`를 양쪽에서 실행. numpy 1/2 차이로 깨지는 코드를 Pi에 가기 전에 잡는다 |
| `psafe.tools.tpucheck` | 아래 3.3 |

### 3.3 환경 점검 도구 `tpucheck`

`camcheck`, `mcucheck`처럼 Coral 노드 설치 직후 한 번 돌리는 도구입니다. 실패 지점을 한 줄씩 보여 주는 게 목적입니다.

```
$ python -m psafe.tools.tpucheck --weights models/y8n_320_incar_v1/model_edgetpu.tflite
[ok]   python 3.12.x, numpy 1.26.x
[ok]   interpreter: tflite_runtime 2.17.1
[ok]   USB device found (Coral)              <- lsusb 확인
[ok]   load_delegate("libedgetpu.so.1")
[ok]   manifest: built with TF 2.17.x, compiler 16.x   (runtime matches)
[ok]   allocate_tensors: in [1,320,320,3] int8, out [1,84,2100] int8
[ok]   invoke x50: p50 __ ms, p95 __ ms
```

Coral이 없어도 앞의 두 줄과 `--delegate none` 모드는 지금 PC에서 만들고 시험할 수 있습니다.

### 3.4 B안을 위한 자리

`tflite_compat`가 `ai_edge_litert`도 시도하므로, 3.13 메인 venv에서 Coral이 동작하는 게 확인되면 **코드 변경 없이** 3.12 venv를 없앨 수 있습니다. 즉 A안/B안 선택이 코드 구조에 영향을 주지 않도록 설계했습니다.

---

## 4. 모델 빌드 파이프라인 (P3: 변환된 모델을 믿을 수 있는가)

### 4.1 흐름

```
 x86 PC / Colab : tools/build_edgetpu.py  (one command per model)

  weights.pt ─┐
  calib/ ─────┼─▶ [1] export INT8 .tflite ──▶ [2] edgetpu_compiler -s ──▶ [3] parse log
  (normal +   │        (ultralytics,              model_edgetpu.tflite        TPU ops / total
   in-car)    │         imgsz fixed)              compile.log                 first CPU op
              │                │
              │                ▼
  eval set ───┴─────▶ [4] evaluate on CPU  (yolo_tflite, delegate: none, INT8 .tflite)
  (labeled,                 vs float ONNX on the same images
   per scene)               -> recall per scene, missed-person frames
                                   │
                                   ▼
                            [5] gate  ── fail ──▶ stop, report why
                                   │ pass
                                   ▼
                            [6] write models/<id>/ : model_edgetpu.tflite, model_int8.tflite,
                                                     manifest.json, compile.log, eval.json
                                   │
                                   ▼  scp to the Coral Pi   (models/ is not in git)
```

- 4번이 Coral 없이 되는 이유: 컴파일 **전** INT8 `.tflite`(`model_int8.tflite`)는 CPU에서 그대로 돕니다. 정확도 저하는 양자화에서 오므로 이걸로 대부분 측정됩니다.
- 4번은 우리 런타임 코드(`YoloTFLiteDetector`)를 그대로 써서 평가합니다. Ultralytics의 `val`을 쓰면 전후처리가 우리 것과 달라 "평가에선 됐는데 실제론 다른" 상황이 생길 수 있기 때문입니다.

### 4.2 `manifest.json` — 오프라인과 런타임의 계약

```json
{
  "id": "y8n_320_incar_v1",
  "source_weights": "y8n_incar_v1.pt",
  "arch": "yolov8n",
  "imgsz": 320,
  "names": ["person"],
  "box_units": "normalized",
  "quant": "int8",
  "calibration": { "set": "calib_v1", "images": 200, "in_car_ratio": 0.4 },
  "build": { "tensorflow": "2.17.1", "ultralytics": "8.x.y", "edgetpu_compiler": "16.0.x" },
  "compile": { "tpu_ops": 0, "total_ops": 0, "first_cpu_op": null },
  "eval": { "recall_normal": 0.0, "recall_in_car": 0.0, "float_recall_in_car": 0.0 },
  "git_commit": "abc1234",
  "created": "2026-11-25T00:00:00Z"
}
```

(숫자는 자리표시자입니다.)

- 런타임은 `imgsz`, `names`, `box_units`, `build.*`를 load()에서 **검사에 사용**합니다.
- 나머지는 기록용입니다. 나중에 "Pi에 올라간 모델이 어떤 데이터·어떤 버전으로 만들어졌는가"를 파일 하나로 추적할 수 있습니다.
- `.tflite` 안 메타데이터에 의존하지 않고 옆 파일로 두는 이유: Ultralytics·onnx2tf 등 변환 경로마다 메타데이터 방식이 달라서, 우리가 통제하는 형식 하나로 통일하는 편이 단순합니다.

### 4.3 평가 게이트 (배포해도 되는 모델인가)

| 기준 | 내용 | 기준값 |
|---|---|---|
| TPU 매핑 | 컴파일 로그에서 첫 CPU 연산 위치 | head 이전에 CPU로 떨어지면 탈락 (YOLO11 C2PSA 확인용) |
| 차 안 recall 하락 | float 대비 INT8 | 팀이 정함 (예: 하락 ≤ 2%p) |
| 놓친 사람 프레임 | 차 안 장면에서 사람 있는데 검출 0인 프레임 수 | float 대비 늘어나면 calibration 재검토 |
| 일반 장면 recall | 파인튜닝으로 일반 사람 성능이 무너지지 않았는지 | float 기준선과 비교 |

안전 시스템이므로 mAP가 아니라 **"사람이 있는데 못 본 프레임"**을 1순위 지표로 둡니다 (메모리의 모델 전략과 같은 방향).

---

## 5. 테스트 전략

| 테스트 | 어디서 | Coral 필요 |
|---|---|---|
| `quantize`/`dequantize` 왕복, 경계값 (순수 함수) | PC pytest | ❌ |
| `yolo_common` 분리 후 기존 `test_yolo.py` 그대로 통과 (리팩터링 안전망) | PC pytest | ❌ |
| `YoloTFLiteDetector(delegate=none)` vs `YoloOnnxDetector`: 같은 이미지에서 박스·점수 비교 | PC (TF 또는 ai-edge-litert) | ❌ |
| load() 검사: manifest 없음, imgsz 불일치, `_edgetpu` 파일 + `none` → 각각 에러 | PC pytest | ❌ |
| delegate 실패 주입 (fake `tflite_compat`) → load 예외 | PC pytest | ❌ |
| invoke 예외 주입 → `detector_error` FAULT, invoke 지연 주입 → stale N | PC pytest (기존 runner 테스트 방식) | ❌ |
| 양 환경 테스트 (3.13+np2, 3.12+np1) | PC | ❌ |
| `tpucheck` 전 항목, delegate 로드 | Pi | ✅ |
| 추론 지연 p50/p95 (`bench`에 tflite 추가) | Pi | ✅ |
| 30분 연속 실행: 온도, `stale_frame`, FAULT 수 | Pi | ✅ |
| USB 뽑기 실험: 예외인지 hang인지, 노드가 N을 내는지 | Pi | ✅ |

⚠️ Windows PC에서 TFLite 인터프리터(`tensorflow` CPU 또는 `ai-edge-litert`) 설치가 되는지는 확인이 필요합니다. 안 되면 PC 쪽 TFLite 테스트는 WSL이나 Colab에서 돌리고, Windows pytest에서는 skip 처리합니다.

---

## 6. 단계별 할 일

### T0 — 지금 (HW 없음)

1. **컴파일 로그 비교** (가장 먼저): yolo11n / yolov8n 320을 Colab에서 export + `edgetpu_compiler -s`. 결과로 fine-tune 대상 아키텍처 결정. → EXP 항목으로 기록
2. `yolo_common.py` 분리 리팩터링 (동작 변화 없음, 기존 테스트로 확인)
3. `tflite_compat.py` + `YoloTFLiteDetector` (`delegate: none`으로 개발)
4. `manifest.json` 형식 확정, `tools/build_edgetpu.py` (Colab에서 실행)
5. `tools/evalmodel.py`: 장면별 recall, 놓친 사람 프레임. float ONNX vs INT8 tflite
6. `tpucheck` (Coral 없는 항목까지), `bench`에 tflite 지원
7. runner `fatal_stale_s` + systemd 서비스 파일 초안
8. `pyproject` extras, `requirements/tpu-py312.txt`, `.gitignore`에 `*.tflite`·`models/` 추가

### T1 — Coral 도착 직후 (반나절 목표)

1. (10분) B안 시도: 3.13 venv에서 `ai-edge-litert` + delegate. 되면 3.12 venv 생략
2. 안 되면 A안: 3.12 venv + libedgetpu + tflite-runtime 설치 (sudo는 사용자가 직접)
3. `tpucheck` 전 항목 통과
4. `bench`: CPU ONNX 104 ms 대비 end-to-end p50

### T2 — 통합

1. Coral 노드 config로 3-보드 실행, 30분 연속 실행
2. USB 뽑기 실험
3. 같은 장면에서 ONNX 노드 vs Coral 노드 결과 비교 → FSM 임계값 재확인

---

## 7. 결정이 필요한 것 (사용자)

| # | 질문 | 제 의견 |
|---|---|---|
| D1 | Coral을 어느 노드에 붙이나 (Coral 1개) | **실험이 끝날 때까지 정하지 않아도 되도록 설계함** (config만 바꾸면 어느 노드든 가능). 기준을 말하면: 차 안 승객 탐지가 가장 어렵고 중요하므로 **내부 카메라 노드(A 또는 B)**에 붙여 더 큰 입력(예: 640)을 쓸 여유를 확보하는 쪽을 추천. 단 A·B 성능이 비대칭이 된다는 점은 감수 |
| D2 | yolo11n vs yolov8n | T0-1 컴파일 로그 결과로 결정. 로그가 나쁘면 yolov8n |
| D3 | Coral 노드 입력 크기 320 vs 640 | Coral에서 640이 시간 안에 들어오면 차 안 작은 사람에 유리. T1 bench 후 결정 |
| D4 | 평가 게이트 기준값 (recall 하락 허용치) | 팀 판단. 안전 쪽으로 보수적으로 |
| D5 | 모델 파일 배포 방식 | MVP는 scp. git에는 manifest만 남기는 것도 방법 |

---

## 8. 검증되지 않은 가정 (도착 후 확인 목록)

| 가정 | 틀리면 |
|---|---|
| Debian 13 + Python 3.12 + tflite-runtime 2.17.1 + 커뮤니티 libedgetpu가 Pi 4에서 동작 | B안 → C안(Docker) 순서로 |
| Ultralytics edgetpu export 출력이 `(1, 4+nc, N)` 한 개이고 좌표가 0~1 정규화 | load() 검사가 에러를 내므로 바로 드러남 → decode 어댑터 수정 |
| CPU INT8 결과 ≈ Edge TPU 결과 | T2에서 같은 이미지로 비교. 차이가 크면 평가를 Pi에서 다시 |
| USB 빠짐 시 예외 또는 hang 둘 중 하나 | 두 경우 모두 N이 되도록 설계됨. 다른 동작(프로세스 크래시)이어도 허브 timeout으로 N |
| Coral `std` 클럭에서 발열이 CPU 추론보다 낮음 | 방열 대책 없이 `max` 클럭은 쓰지 않음 |
