# Coral 이해하기: 헷갈렸던 지점 정리

2026-11-25. Coral 설계를 논의하면서 헷갈렸던 질문과 답을 순서대로 모았습니다.
설계 본문은 [coral_sw_design.md](coral_sw_design.md), Gemini 안은 [../CORAL_DESIGN.md](../CORAL_DESIGN.md), 둘의 토론은 [DISCUSSION.md](DISCUSSION.md)에 있습니다.

> 표기: ⚠️ = 아직 우리 장비에서 확인하지 않은 내용

---

## Q1. 모델은 Pi에서 도나, Coral에서 도나?

**신경망 계산은 Coral에서 돕니다.** 단, 모델 파일은 Pi에 있고 앞뒤 처리는 Pi가 합니다.

```
Pi SD카드                              Coral USB
models/model_edgetpu.tflite
   │  프로그램 시작(load) 때 1번
   └──── 가중치 + 계산 순서 ─────────▶  내부 메모리(약 8 MB)에 올라감
                                        이후 매 프레임 계산은 Coral이 함
```

| 단계 | 어디서 | 지금(CPU만) |
|---|---|---|
| 카메라 → 320x320 int8 숫자 (전처리) | Pi | Pi |
| **신경망 계산 (모델 본체, 가장 무거움 ≈ 100 ms)** | **Coral** | Pi |
| 숫자 → 박스·점수 → 사람 판정 (후처리, NMS) | Pi | Pi |
| FSM, 허브 통신 | Pi | Pi |

Coral을 붙이면 **가운데 줄 하나만** Coral로 옮겨 갑니다. 그래서 빨라지고 Pi 발열이 줄어듭니다.

- Coral은 전원이 꺼지면 다 잊습니다. 프로그램을 켤 때마다 모델을 다시 올립니다.
- yolov8n·yolo11n INT8은 약 3 MB라 Coral 메모리에 들어갑니다. 8 MB를 넘는 모델은 넘치는 부분을 매 프레임 USB로 다시 보내야 해서 느려집니다. Coral에는 작은 모델이 맞습니다.

## Q2. 그럼 "하나의 SW가 두 HW 위에서 도는" 건가?

**아닙니다. 우리 코드(psafe)는 Pi에서만 돕니다.** Coral에는 OS도, Python도, 우리 코드도 없습니다. Coral은 int8 숫자 배열에 곱셈·덧셈만 아주 빠르게 하는 USB 계산기입니다.

- 비유: 워드는 PC에서 돌고, 프린터는 드라이버가 보낸 데이터만 처리합니다.
- 정확히 말하면 "Pi 위의 프로그램이, Coral 위에 올린 **모델의 계산**을 빌려 쓴다"입니다.

## Q3. Pi와 Coral 사이 인터페이스는 무엇인가?

인터페이스는 세 층입니다. **우리가 직접 쓰는 건 ① 하나뿐입니다.**

```
┌─ Raspberry Pi ──────────────────────────────────────────┐
│  psafe (우리 코드)  YoloTFLiteDetector.predict()          │
│  ════ ① TFLite API ════  ← 우리가 쓰는 건 여기까지          │
│        set_tensor / invoke / get_tensor                  │
│  tflite_runtime  (Google 라이브러리, 설치만 함)             │
│  ──── ② delegate (자동 연결) ────                         │
│  libedgetpu.so   (Coral 드라이버, 설치만 함)                │
└─────────────────────────┼────────────────────────────────┘
                          │ USB 3.0
          ──── ③ Google 내부 프로토콜 (볼 일 없음) ────
┌─ Coral USB ─────────────▼────────────────────────────────┐
│  Edge TPU 칩 + 내부 메모리(약 8 MB)                        │
└──────────────────────────────────────────────────────────┘
```

우리 코드에서 Coral을 쓰는 건 이게 전부입니다:

```python
itp = Interpreter("model_edgetpu.tflite",
                  experimental_delegates=[load_delegate("libedgetpu.so.1")])  # 모델을 Coral에 올림
itp.allocate_tensors()
itp.set_tensor(in_idx, q)     # 입력 int8 배열 넘기기
itp.invoke()                  # Coral 계산 (끝날 때까지 기다림)
out = itp.get_tensor(out_idx) # 출력 int8 배열 받기
```

- "**버전 짝 맞추기**"(tflite_runtime ↔ libedgetpu 같은 TF 버전)는 ② 연결이 맞물리게 하는 문제입니다.
- `invoke()`는 동기 호출입니다. 부르면 Coral이 끝낼 때까지 Pi 스레드가 기다립니다. 우리 코드 입장에서는 ONNX의 `session.run()` 자리를 `invoke()`가 대신하는 것입니다.

## Q4. Coral은 무엇을 받아서 무엇을 돌려주나?

**Coral은 이미지·사람·박스를 모릅니다.** 숫자 배열을 받아서 숫자 배열을 돌려줄 뿐입니다.

| 시점 | Pi → Coral | Coral → Pi |
|---|---|---|
| 시작할 때 1번 (`load`) | 컴파일된 모델 (계산 순서 + 가중치 약 3 MB) | — |
| 매 프레임 (`predict`) | 입력 int8 `[1, 320, 320, 3]` ≈ 300 KB | 출력 int8 `[1, 4+클래스 수, 2100]` |

- 출력 예: COCO 80클래스 → `[1, 84, 2100]` ≈ 176 KB, person 1클래스 → `[1, 5, 2100]` ≈ 10 KB.
- 2100 = 320 입력에서 YOLO가 박스 후보를 내는 칸 수 (40² + 20² + 10²). 각 후보 = 좌표 4개 + 클래스별 점수.
- "사진 → 숫자"(전처리)와 "숫자 → 사람이 있다"(후처리)는 모두 Pi의 일입니다.

한 프레임 흐름:

```
Pi (우리 코드)                                   Coral
카메라 640x480 BGR
 ↓ letterbox → 320x320 → RGB, 0~1 → int8 (양자화)
 ↓ set_tensor + invoke() ── 300 KB int8 ──────▶  신경망 계산
   (Pi 스레드는 기다림)    ◀── int8 출력 ──────
 ↓ get_tensor → int8을 실수로 되돌림 (역양자화)
 ↓ 2100개 후보 중 점수 높은 것 + NMS
 ↓ "person 0.82, 박스(...)"   ← 여기서 처음 의미가 생김
 ↓ FSM → Y/N → 허브로 UDP
```

## Q5. 모델 "전체"가 Coral에서 도나?

**Coral이 지원하는 연산까지만** Coral에서 돕니다. 컴파일러가 모델을 앞에서부터 보다가 **지원 안 되는 연산을 처음 만나면, 그 지점부터 끝까지는 Pi CPU가 계산**합니다. 그래프는 한 번만 나눠집니다.

```
좋은 경우 (yolov8n 예상):   [ conv conv ... conv  head ]   전부 Coral
나쁜 경우 (yolo11n 걱정):   [ conv ... C2PSA | neck  head ]
                                Coral        |   Pi CPU
```

- 둘 다 우리 코드에서는 `invoke()` 한 번입니다. 결과는 같고 **속도만 다릅니다.**
- YOLO11의 C2PSA(attention)가 쓰는 BatchMatMul은 Coral이 지원하지 않습니다. 그래서 neck·head가 통째로 Pi로 넘어갈 수 있습니다 ⚠️.
- 어디서 끊기는지는 `edgetpu_compiler -s` 로그에 나옵니다. HW 없이 Colab에서 먼저 확인할 수 있습니다.

## Q6. 우리 코드에서 바뀌는 건 무엇인가?

| | 지금 (ONNX, CPU) | Coral |
|---|---|---|
| 모델 열기 | `ort.InferenceSession(...)` | `Interpreter(..., delegate=libedgetpu)` |
| 입력 준비 | float32 `[1,3,320,320]` (NCHW) | **int8** `[1,320,320,3]` (NHWC, 축 순서도 다름) |
| 계산 | `session.run()`: Pi CPU | `invoke()`: **Coral** |
| 출력 정리 | float 그대로 | **int8 → float 되돌리기** |
| 박스 고르기, NMS, FSM, 허브 | 그대로 | 그대로 |

새로 짜는 건 "int8로 바꿔 주고, 받은 int8을 되돌리는" 앞뒤 몇 줄 + 모델 여는 방법 정도입니다. 판단 로직은 바꾸지 않고, config의 `model:`만 바꿔 전환합니다.

## Q7. Coral용 모델은 어디서 만들고, Pi는 무엇을 받나?

- 양자화(INT8)와 컴파일(`edgetpu_compiler`)은 **x86 PC나 Colab**에서 합니다. 컴파일러가 x86 전용이기 때문입니다.
- Pi는 결과 파일(`*_edgetpu.tflite`)만 받아서 실행합니다. Pi에서는 컴파일하지 않습니다.
- 컴파일 **전** INT8 `.tflite`는 일반 CPU에서도 돕니다. 그래서 Coral 없이도 INT8 때문에 정확도가 얼마나 떨어지는지를 미리 잴 수 있습니다. 컴파일된 `_edgetpu.tflite`는 Coral 없이는 돌지 않습니다.

## Q8. 버전이 다른 환경을 어떻게 "통일"하나?

Python 버전을 하나로 맞추는 게 아닙니다. **경계마다 맞춰야 할 것만 맞춥니다.**

```
 (1) 빌드: PC/Colab ══ 파일(.tflite) ══▶ (2) Coral 노드 Pi ── UDP+JSON ──▶ (3) 다른 Pi / 허브
     맞출 것: 변환 TF 버전 ↔ Pi tflite_runtime         맞출 것: 프로토콜 v2만
              컴파일러 버전 ↔ Pi libedgetpu            (Python 버전 무관)
```

- (1)↔(2): 넘어가는 건 파일 하나뿐입니다. Python 버전은 상관없고, 위 두 쌍의 버전만 맞으면 됩니다. 만든 환경의 버전을 모델 옆 `manifest.json`에 기록해 두고, Pi가 load할 때 검사하자는 게 Claude 안입니다.
- (2)↔(3): Pi끼리는 UDP+JSON만 오갑니다. Pi마다 Python 버전이 달라도 됩니다.
- **같은 git 코드**가 (2)와 (3)에서 돌도록 보장하는 것이 실제 의미의 "통일"입니다. `pyproject.toml`이 `>=3.11`이라 3.12와 3.13 모두 됩니다.

## Q9. 왜 "Python 3.12 venv"를 제안했나?

거꾸로 따라가면 이렇습니다.

1. Python 버전을 타는 부품은 `tflite_runtime` 하나뿐입니다. 이건 C 확장 모듈이라 Python 버전별 wheel이 필요합니다. `libedgetpu`는 C 라이브러리라 Python 버전과 무관합니다.
2. `tflite_runtime` wheel은 커뮤니티 빌드 기준 3.12까지만 있습니다. Pi의 시스템 Python은 3.13입니다.
3. `pycoral`(3.9까지)은 필요 없습니다. `tflite_runtime` + `load_delegate`로 충분합니다.
4. 조건을 만족하는 버전 중 3.13에 가장 가까운 것이 3.12입니다. `uv`로 sudo 없이 따로 설치할 수 있고, 시스템 Python은 건드리지 않습니다.
5. venv를 분리하면 `numpy<2` 같은 제약이 기존 환경을 오염시키지 않습니다.

이 조합은 커뮤니티 보고 하나에 기댄 것이라 ⚠️ 우리 장비에서 확인이 필요합니다.

## Q10. 그런데 3.12 venv 안에 구멍이 있다 (Gemini가 지적)

내부 카메라는 **NoIR**입니다. NoIR은 보통 Raspberry Pi 카메라 모듈(CSI)이고, CSI 카메라용 `picamera2`는 apt 패키지라 **시스템 Python 3.13에 묶여 있습니다.** 그래서 내부 카메라 노드에서는 "앱 전체를 3.12로 실행"이 안 될 수 있습니다.

선택지 ([DISCUSSION.md](DISCUSSION.md)에서 논의 중):

| 안 | 내용 |
|---|---|
| OS 재설치 | Bookworm(Python 3.11)으로 바꾸면 공식 패키지가 다 맞아서 환경이 하나가 됨 |
| USB 카메라 | `picamera2`가 필요 없으니 3.12 venv 하나로 끝남 |
| `rpicam-vid` 파이프 | CSI여도 시스템 명령으로 영상을 받아 OpenCV로 읽으면 3.12 하나로 유지 가능 ⚠️ |
| 프로세스 분리 | 카메라·앱은 3.13, 추론만 3.12 별도 프로세스. 복잡도 증가 |

**먼저 확인할 것: NoIR 카메라가 CSI인지 USB인지, Pi OS를 새로 설치할 수 있는지.**

## Q11. Coral이 고장 나면 어떻게 되나?

새 안전 장치보다, 이미 있는 장치가 잡는지를 확인했습니다. 모든 경우에 결과는 **N**입니다.

| 고장 | 어디서 끊기나 (Q3 그림) | 잡는 장치 |
|---|---|---|
| 드라이버 없음 / 버전 짝 불일치 | ② 연결 실패 → 모델 열 때 에러 | 노드가 시작 안 됨 → 허브 timeout |
| 실행 중 USB 빠짐 (예외) | ③ 끊김 → `invoke()` 예외 | `detector_error` → FAULT |
| 실행 중 USB 빠짐 (멈춤) | ③ 끊김 → `invoke()`가 안 돌아옴 | heartbeat가 `max_decision_age_ms` 넘으면 stale N |
| 느려짐 (CPU로 넘어간 연산, 발열) | Q5의 나쁜 경우 | `max_frame_age_ms` → `stale_frame` FAULT |

- **CPU로 조용히 넘어가지 않습니다.** Coral을 못 쓰면 에러를 냅니다. 몰래 CPU로 돌면 수백 ms가 걸려 FAULT만 반복되고, 원인이 숨기 때문입니다.
- 멈춤(hang)의 경우 N은 계속 나가지만 프로세스가 스스로 재시작하지 않습니다. 그래서 "판단이 일정 시간 넘게 갱신 안 되면 종료 → systemd 재시작"을 추가하자고 제안했습니다 ([DISCUSSION.md](DISCUSSION.md) R1-5).
- USB가 빠졌을 때 예외가 나는지 멈추는지는 모릅니다 ⚠️. 둘 다 N이 되도록 되어 있습니다.

---

## 한 줄 요약

> 우리 코드는 Pi에서만 돈다. 모델 파일은 Pi에 있다가 켤 때 Coral로 올라가고, 신경망 계산만 Coral이 한다. Pi는 int8 배열을 만들어 `invoke()`로 넘기고, 돌려받은 int8 배열을 해석해 사람 여부를 판단한다.
