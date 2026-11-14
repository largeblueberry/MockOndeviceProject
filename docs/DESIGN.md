# 주차 타워 안전 감지 시스템 — 설계 문서 (MVP v0.1)

> 작성일: 2026-11-14 · 상태: 초안 (사전 MVP 단계)
> 목적: 프로젝트(2주) 시작 전, SW 큰 틀과 인터페이스를 확정하고 팀에 공유한다.
> 원칙: **이 문서는 "무엇을/왜"를 공유한다. 코드는 공유하지 않는다.** 팀원은 인터페이스 명세를 보고 각 모듈을 직접 구현한다.

---

## 1. 프로젝트 개요

| 항목 | 내용 |
|---|---|
| 주제 | 주차 타워 입·출차 시 사람이 구역을 완전히 벗어났는지 판단하는 안전 감지 모델 및 SW/HW |
| 연산 장치 | Raspberry Pi 4 |
| 보조 MCU | ESP32 |
| 센서 | 카메라 (기종·대수 미정) |
| 시연 환경 | 실물 타워가 아닌 **모형(레고 등)** 기반 시연 |
| 최종 출력 | **Y / N** (Y = 동작 안전, N = 동작 불가). 확장 가능하게 설계 |
| 개발 언어 | Python 기본, 병목 구간만 C로 최적화 (프로파일링 결과로 결정) |
| 기간 | 사전 MVP 1주 + 본 프로젝트 2주 |

### 1.1 핵심 질문
> "지금 이 순간, 주차 구역 안에 사람이 **확실히** 없는가?"

확실하지 않으면 답은 항상 **N** 이다.

---

## 2. 설계 원칙

1. **Fail-safe 우선**: 모르면 N. 카메라 끊김·모델 오류·지연 초과·통신 단절 → 모두 N.
2. **Recall 우선**: 사람을 놓치는 것(False Negative)이 가장 치명적. 오경보(False Positive)는 감수.
3. **비대칭 판단**: 사람 감지 → *즉시* N. 사람 없음 → *T초 연속* 확인 후에만 Y.
4. **모듈 교체 가능성**: 입력·모델·출력은 인터페이스 뒤에 숨기고, 설정 파일로 교체.
5. **최신성 > 완전성**: 오래된 프레임으로 판단하지 않는다. 밀리면 버린다.
6. **HW 없이도 개발 가능**: 모든 HW 의존부는 Mock/Dummy 구현을 함께 가진다.
7. **측정 가능성**: 단계별 지연, FPS, 드롭 수를 항상 기록한다.

---

## 3. 시스템 아키텍처

### 3.1 파이프라인 — 논리 구조 (v0.2)

논리적 흐름은 `INPUT → 전처리 → 모델 → FSM → OUTPUT` 그대로지만, **실행 단위(스레드)는 3개로 묶는다.**

```
 [T1 Capture]            [T2 Worker]                               [T3 Heartbeat]
┌──────────┐  Latest  ┌─────────────────────────────────────┐ Latest ┌──────────────┐
│  Source  │ ───────► │ Detector                │   FSM     │ ─────► │ Outputs      │
│          │   Slot   │ (preprocess→infer→post) │ (+ROI)    │  Slot  │ 200ms 주기 송신│
└──────────┘ (size 1) └─────────────────────────────────────┘        └──────────────┘
 Dummy / Video /         Dummy / YoloOnnx / ...    순수 로직            Console / Serial
 Webcam / PiCamera       ▲ 전처리는 모델 소속        (I/O 없음)          / MockMCU
                         config.yaml 로 구현체 선택

 [Main] Supervisor: 스레드 생존 감시, 메트릭 출력, 종료 처리
```

| 스레드 | 역할 | 왜 분리하나 |
|---|---|---|
| T1 Capture | 카메라에서 계속 프레임을 뽑아 **최신 1장만** 슬롯에 덮어씀 | 카메라 내부 버퍼를 비우지 않으면 오래된 프레임이 나옴 |
| T2 Worker | 최신 프레임 꺼내 추론 → FSM → Decision 갱신 | 가장 느린 구간. 혼자 자기 속도로 돈다 |
| T3 Heartbeat | 200ms마다 최신 Decision을 출력. **Decision이 오래됐으면 N** | 추론이 멈춰도 출력은 살아서 N을 보내야 함 |

**ThreadedRunner 구현 규칙** (`runners.py`)
- **FSM은 Worker 스레드만 소유** → FSM에 락 불필요. Heartbeat는 `DecisionBoard`(락 1개)로 최신 Decision만 읽는다.
- **Heartbeat가 Decision 나이를 직접 검사**: Worker가 `predict()` 안에서 멈추면 `on_no_frame()`조차 못 부른다. 이걸 잡는 건 Heartbeat뿐 → `decision_stale` N.
- **열기 순서: Output → Detector(load·warmup) → Source(카메라 마지막)**. 모델 로딩 동안 쌓인 프레임이 첫 판단부터 stale이 되지 않게.
- **어떤 스레드든 죽으면 run 종료** (`result.error`). 종료 시 **마지막 송신은 항상 N(`shutdown`)**. 재시작은 systemd, 그 사이 공백은 ESP32 워치독이 커버.
- Output 하나의 예외가 다른 Output을 막지 않는다 (각각 try/except).

**v0.1 → v0.2 변경 이유**
- **전처리를 모델 안으로**: 입력 크기·정규화·색 순서는 모델마다 다르다. 전처리를 따로 두면 모델 교체 시 두 곳을 고쳐야 한다.
- **Model→FSM 큐 제거**: FSM은 수 μs 짜리 계산. 큐·스레드를 두면 복잡도만 늘고 이득이 없다.
- **출력을 독립 주기로**: 하트비트는 "추론 결과가 나올 때"가 아니라 "일정 주기로" 나가야 워치독이 의미 있다.

### 3.2 큐 정책 — "순서는 지키되, 밀리면 버린다" → LatestSlot

| 항목 | 정책 | 이유 |
|---|---|---|
| 구조 | 크기 1짜리 **LatestSlot** (put = 덮어쓰기, get = 새 값 올 때까지 대기/타임아웃) | 항상 최신 프레임으로 판단 |
| 순서 | seq 단조 증가 → 역순 불가 | FSM은 시간 순서가 보장되어야 함 |
| 기록 | 덮어쓴 횟수(드롭) 카운트 → 로그 | 추론이 얼마나 밀리는지 확인 |
| 신선도 검사 | FSM에서 `now - timestamp > max_age_ms` 면 Fault | 오래된 데이터로 Y를 내는 것 방지 |

> ⚠️ 왜 엄격한 FIFO(무제한)가 위험한가?
> 카메라 30 FPS, 모델 8 FPS라면 초당 22장씩 큐에 쌓인다. 1분 후엔 1,320장 = **약 165초 전 화면**으로 판단하게 된다.
> 크기 1 슬롯은 "maxsize=1 + drop-oldest 큐"와 같은 개념이다.

### 3.3 동시성 모델 — `threading` (asyncio 사용 안 함)
- OpenCV 캡처, ONNX 추론, 시리얼 쓰기는 모두 **블로킹 호출**. asyncio를 써도 결국 `run_in_executor`(스레드)로 감싸야 하므로 이득 없이 복잡도만 늘어난다.
- OpenCV·ONNX Runtime은 무거운 연산 중 **GIL을 해제** → 스레드로도 캡처와 추론이 실제로 병렬 동작.
- 병목이 Python 코드로 확인되면 → 해당 구간만 NumPy 벡터화 → 그래도 부족하면 `multiprocessing` 또는 C 확장.
- **C 최적화는 프로파일링 후 결정.** 추론 런타임은 이미 C++이므로, 보통 병목은 전·후처리.

### 3.4 두 가지 실행 모드 (같은 부품, 다른 Runner)
| 모드 | 구성 | 용도 |
|---|---|---|
| `ThreadedRunner` | T1/T2/T3 스레드 + 실제 시계 | 실제 동작, 시연 |
| `SyncRunner` | 단일 루프, **가짜 시계(FakeClock)** 로 시간 진행 | 시나리오 테스트. 15초 시나리오를 0.1초에, **매번 같은 결과**로 |

- 모든 컴포넌트는 `time.time()`을 직접 부르지 않고 **주입받은 Clock** 을 쓴다. → 테스트 재현성 확보.
- FSM은 I/O 없는 순수 로직 → `fsm.step(detections, now) -> Decision` 단위 테스트가 쉬움.

### 3.5 장애 감지 (Fault 원인 목록)
| 원인 | 감지 방법 |
|---|---|
| 입력 끊김 | Worker가 `max_frame_age_ms` 동안 새 프레임 못 받음 |
| 추론 지연 / 멈춤 | Decision 나이가 임계 초과 → Heartbeat가 N 송신 |
| 스레드 사망 | 스레드 내 예외 → Fault 보고, Supervisor가 `is_alive()` 감시 |
| 카메라 가림 / 고장 | 프레임 평균 밝기 너무 낮음·높음 (검은 화면, 렌즈 가림) |
| 화면 정지 | 연속 프레임 차이가 거의 0 (드라이버가 같은 프레임 반복) |
| ESP32 측 | 500ms 무신호 → 자체 N (7장) |

### 3.6 용어 정리 — "직렬화"
| 의미 | 영어 | 이 프로젝트에서 |
|---|---|---|
| 순차 처리 | Sequential / Ordering | Stage 간 큐로 순서대로 흘려보냄 (3.2) |
| 데이터 직렬화 | Serialization | 객체 → 바이트 변환. RPi ↔ ESP32 UART 통신 패킷 (7장) |

---

## 4. 데이터 계약 (Stage 간 패킷 형식)

> 팀원 간 **가장 중요한 합의 사항.** 이것만 지키면 각 모듈은 독립 개발 가능.

### 4.1 FramePacket (Source → Detector)
| 필드 | 타입 | 설명 |
|---|---|---|
| `seq` | int | 프레임 일련번호 (단조 증가) |
| `timestamp` | float | 캡처 시각 (monotonic, 초) |
| `source_id` | str | 입력 소스 식별자 (예: `cam0`, `dummy`) |
| `image` | ndarray (H×W×3, BGR, uint8) | 원본 또는 전처리된 이미지 |
| `meta` | dict | 부가 정보. **Dummy 소스는 여기에 정답(ground truth)을 실음** |

### 4.2 Detections (Model → FSM)
| 필드 | 타입 | 설명 |
|---|---|---|
| `seq`, `timestamp` | | FramePacket에서 그대로 전달 |
| `objects` | list[Detection] | 감지 결과 목록 |
| `model_name` | str | 사용된 모델 (로그·비교용) |
| `infer_ms` | float | 추론 시간 |

`Detection = { cls: str, conf: float, bbox: (x1, y1, x2, y2) 정규화 0~1 }`

> bbox는 **정규화 좌표(0~1)** 로 통일 → 모델 입력 해상도가 바뀌어도 FSM·ROI 로직은 그대로.

### 4.3 Decision (FSM → Output)
| 필드 | 타입 | 설명 |
|---|---|---|
| `safe` | bool | **Y(True) / N(False)** — 최종 출력 |
| `state` | str | FSM 현재 상태 (8장) |
| `reason` | str | 판단 근거 (예: `person_in_roi`, `clear_3.0s`, `stale_frame`) |
| `seq`, `timestamp` | | 근거가 된 프레임 |

> 출력은 지금은 Y/N만 사용하지만, `state`/`reason`을 함께 실어 두면 추후 경고등·부저·화면 표시 등으로 확장할 때 Output 모듈만 추가하면 된다.

---

## 5. 모델 추상화 & 라우팅

### 5.1 구조
```
                 ┌─ DummyDetector       (정답 기반 + 노이즈 주입, HW/모델 없이 테스트)
                 ├─ YoloOnnxDetector    (YOLO + ONNX Runtime)
Detector ◄───────┼─ YoloNcnnDetector    (YOLO + NCNN, RPi에서 빠름)
(인터페이스)      ├─ TFLiteDetector
                 └─ (새 모델 추가 시 여기 등록만)
```

### 5.2 Detector 인터페이스 (명세)
| 메서드 | 설명 |
|---|---|
| `load(config)` | 가중치 로드, 런타임 초기화 |
| `warmup()` | 첫 추론 지연 제거용 더미 추론 |
| `predict(frame_packet) -> Detections` | **전처리 + 추론 + 후처리(NMS, 좌표 정규화, 클래스명 통일)** 까지 책임 |
| `close()` | 자원 해제 |

- 클래스명은 내부 표준(`"person"`, `"car"`)으로 통일해서 내보낸다. 파인튜닝 모델이 `"minifig"` 를 내더라도 Detector가 `"person"` 으로 매핑 → FSM 무수정.

### 5.3 레지스트리 + 설정 기반 선택
- 각 구현체는 이름으로 레지스트리(단순 dict)에 등록 (예: `"yolo_onnx"`).
- `config.yaml`의 `model.name` 만 바꾸면 교체 완료 — **파이프라인 코드 수정 없음.**
- "모델 종류"와 "추론 런타임"은 별개 축. RPi에서는 런타임 차이만으로 2~3배 속도 차이가 날 수 있음.

### 5.4 라우팅 (확장 단계, MVP에서는 단일 선택)
| 방식 | 설명 | 안전 관점 |
|---|---|---|
| 조건 라우팅 | 밝기 등 조건에 따라 주간/야간 모델 선택 | 환경 변화 대응 |
| 캐스케이드 | 경량 모델 1차 → 애매하면(conf 중간대) 무거운 모델 재확인 | 속도·정확도 절충 |
| OR 앙상블 | 여러 모델 중 **하나라도** 사람 → 사람 | Recall 극대화 |

### 5.5 ⚠️ 시연 대상 관련 주의 (레고 등)
- COCO 사전학습 모델의 `person` 클래스는 **레고 미니피규어를 잘 인식하지 못할 가능성이 높다.**
- 선택지:
  1. 사람 손/실제 사람을 모형 안에 넣는 방식으로 시연 → 사전학습 모델 그대로 사용
  2. 레고 피규어 데이터 직접 촬영·라벨링 → **파인튜닝** (본 프로젝트 2주 내 충분히 가능한 규모)
- → 모델 교체 구조가 필요한 직접적인 이유. 본 프로젝트 초기에 결정 필요.

---

## 6. 입력(Source) 추상화

| 구현체 | 용도 | 시점 |
|---|---|---|
| `DummySource` | 시나리오 스크립트대로 합성 프레임 생성 + 정답을 `meta`에 실음 | **지금 (HW 없음)** |
| `VideoFileSource` | 녹화 영상 반복 재생 → 재현 가능한 테스트 | 영상 확보 후 |
| `WebcamSource` | PC 웹캠 | 선택 |
| `PiCameraSource` | RPi 카메라 (picamera2) | RPi 수령 후 |

### 6.1 Dummy 시나리오 (HW 없이 FSM·파이프라인 검증)
시나리오 파일로 시간대별 상황을 정의하고, DummySource가 이를 따라 프레임을 생성한다.

```yaml
# scenarios/entry_normal.yaml  (예시 — 형식 제안)
fps: 15
timeline:
  - { from: 0.0, to: 2.0, objects: [] }                         # 빈 구역
  - { from: 2.0, to: 5.0, objects: [car] }                      # 차량 진입
  - { from: 5.0, to: 9.0, objects: [car, person] }              # 운전자 하차
  - { from: 9.0, to: 15.0, objects: [car] }                     # 운전자 퇴장
expect:
  - { at: 7.0,  safe: false }
  - { at: 14.0, safe: true }    # 9.0 + clear_time(3s) 이후
```

`DummyDetector`는 `meta`의 정답을 읽어 Detections를 만들되, **노이즈를 주입**할 수 있게 한다.
- `miss_rate`: 사람을 놓치는 확률 (깜빡임 상황 재현)
- `false_rate`: 없는 사람을 만드는 확률
- `latency_ms`: 인위적 추론 지연 (RPi 속도 흉내 → 큐 드롭 정책 검증)

→ 이것만으로 **FSM 히스테리시스, Fault 처리, 큐 정책, 출력** 전부를 HW 없이 테스트할 수 있다.

시나리오 timeline의 각 구간은 `objects`(실제 구역에 있는 것)와 `camera`(카메라 상태)를 따로 가진다.

| camera | 의미 | 모델이 보는 것 |
|---|---|---|
| `ok` (기본) | 정상 | 실제 그대로 |
| `outage` | 프레임 자체가 안 옴 (케이블 빠짐) | 없음 → 타임아웃 |
| `blackout` | 검은 화면 (렌즈 가림) | **아무것도 없음** (사람이 있어도) |
| `freeze` | 마지막 프레임 반복 (드라이버 멈춤) | **과거 장면** |

> blackout/freeze가 가장 위험하다: 실제로는 사람이 있는데 모델은 "비었다"고 말한다. **Health check(밝기·정지 감지)만이 이를 잡을 수 있다.**
> DummySource는 이미지에 센서 노이즈를 넣는다. 노이즈가 없으면 빈 장면의 정상 프레임끼리도 완전히 같아서 정지 감지를 테스트할 수 없다.

시나리오 파일의 `config:` 블록은 기본 설정 위에 덮어쓴다 (예: flicker는 `miss_rate: 0.3`).

```
python -m psafe --scenario "scenarios/*.yaml" --check     # 전체 시나리오 자동 검증
```

### 6.2 시나리오 목록 (`scenarios/`)
| # | 파일 | 상황 | 기대 결과 |
|---|---|---|---|
| S1 | `entry_normal` | 정상 입차: 하차 → 퇴장 | 퇴장 후 T초 뒤 Y |
| S2 | `reenter` | 검증 중 재진입 | 재진입 즉시 N, 타이머 리셋 |
| S3 | `flicker` | 감지 깜빡임 (miss_rate 30%) | 사람 있는 동안 절대 Y 안 됨 |
| S4 | `camera_outage` | 프레임 2초 끊김 | Fault → N, 복구 후 재검증 |
| S5 | `stale_frames` | 추론 700ms (> 500ms) | 항상 N |
| S6 | `exit_normal` | 정상 출차 | 구역 비면 T초 뒤 Y |
| S7 | `roi_boundary` | ROI 밖 사람 / 경계에 걸친 사람 | 밖은 무시(Y), 걸치면 N |
| S8 | `camera_blackout` | 렌즈 가림 중 사람 진입 | 밝기 이상 → N |
| S9 | `camera_freeze` | 화면 정지 중 사람 진입 | 정지 감지 → N |

---

## 7. HW 구성 및 역할 분담

### 7.1 역할
| 장치 | 역할 |
|---|---|
| **Raspberry Pi 4** | 영상 입력, 전처리, 추론, FSM 판단, 로그·녹화 |
| **ESP32** | 판단 결과 수신, 실제 I/O(릴레이·LED·부저 등), **워치독** |

> 안전 원칙: **ESP32는 RPi의 Y 신호가 "주기적으로" 들어올 때만 동작을 허가한다.** 신호가 끊기면(RPi 다운, 케이블 빠짐) 자동으로 N. → RPi 소프트웨어가 멈춰도 안전.

### 7.2 RPi ↔ ESP32 연결 (기초)
가장 간단한 방법 두 가지:

| 방식 | 연결 | 장점 | 비고 |
|---|---|---|---|
| **USB 시리얼** | ESP32 USB 케이블 → RPi USB 포트 | 배선 0, 전원 공급까지 됨 | **MVP 추천.** RPi에서 `/dev/ttyUSB0` 등으로 보임 |
| GPIO UART | RPi TX(GPIO14) → ESP32 RX, RPi RX(GPIO15) → ESP32 TX, **GND끼리 연결** | 깔끔한 최종 배선 | 둘 다 3.3V 로직이라 직결 가능. TX↔RX **교차** 주의 |

- 통신 속도: 115200 bps (기본값으로 충분 — 우리 패킷은 수십 바이트)
- PC 개발 단계: `MockMCU` (같은 프로토콜을 흉내내는 가짜 ESP32)로 대체

### 7.3 통신 프로토콜 (초안)
RPi → ESP32, **주기 200ms** (Decision이 바뀌지 않아도 계속 송신 = 하트비트 겸용)

```
| STX(0xAA) | SEQ(1B) | STATE(1B) | SAFE(1B: 'Y'/'N') | CRC8(1B) |
```
- ESP32: **500ms 동안 유효 패킷 없음 → 강제 N**
- CRC 오류 패킷은 무시 (무시가 누적되면 결국 타임아웃 → N)
- ESP32 → RPi ACK는 MVP 범위 밖 (확장 항목)

---

## 8. FSM (판단 로직)

### 8.1 상태 정의 (초안)
| 상태 | 의미 | 출력 |
|---|---|---|
| `INIT` | 시작 직후, 워밍업·안정화 대기 | N |
| `OCCUPIED` | ROI 내 사람 감지 중 | N |
| `VERIFYING` | 사람이 사라짐 → T초 연속 무인 확인 중 | N |
| `CLEAR` | T초 연속 무인 확인 완료 | **Y** |
| `FAULT` | 입력 끊김 / stale / 모델 오류 / FPS 저하 | N |

### 8.2 전이
```
           person          person
 INIT ──────────────► OCCUPIED ◄──────────────┐
  │                     │  no person           │
  │ no person           ▼                      │
  └───────────────► VERIFYING ── T초 경과 ──► CLEAR
                        ▲                      │
                        └──── (person 시 OCCUPIED 로)

 [모든 상태] ── 이상 감지 ──► FAULT ── 정상 프레임 K개 연속 ──► VERIFYING
```
- **사람 1프레임 감지 → 즉시 OCCUPIED** (conf ≥ `person_conf_low`, 낮게 설정 → Recall 우선)
- `VERIFYING → CLEAR`: **T초 연속 + 최소 N프레임** 모두 만족 (FPS가 낮을 때 시간만 보고 Y 내는 것 방지)
- `FAULT` 복귀 시 바로 CLEAR가 아니라 **VERIFYING부터** 다시
- `FAULT` 중 사람이 보이면 → `OCCUPIED` (카메라가 사람을 볼 수 있다는 것 자체가 정상 동작의 증거, 어차피 N)
- 클리어 시간은 **프레임 촬영 timestamp 기준** = "카메라가 빈 구역을 본 시간". CPU 처리 시간이 아님
- 시간 비교에는 1µs 허용오차(`TIME_EPS`). 부동소수점(`2.8 - 1.8 = 0.99999…`) 때문에 경계에서 결과가 흔들리지 않게

### 8.2.1 Fault 원인 (reason 값)
| reason | 발생 위치 | 의미 |
|---|---|---|
| `stale_frame` | FSM | `now - 촬영시각 > max_frame_age_ms` |
| `out_of_order` | FSM | seq/timestamp가 역행 |
| `no_frame` | Runner | 프레임 대기 타임아웃 |
| `detector_error` | Runner | `predict()` 예외 — 루프는 죽지 않고 N |
| `too_dark` / `too_bright` | HealthMonitor | 렌즈 가림, 과노출 |
| `frozen_frame` | HealthMonitor | 연속 프레임 픽셀 차이 ≈ 0 (`freeze_frames` 회 연속) |
| `recovering` | FSM | Fault 후 정상 프레임 `fault_recover_frames` 개 대기 중 |

> **Source 구현 규칙**: `FramePacket.timestamp`는 반드시 **촬영 시각**이어야 한다. 읽은 시각을 넣으면 늦게 처리된 프레임의 나이가 짧게 계산되어 `stale_frame` 검사가 무력화된다.

### 8.2.2 안전 불변식 (자동 검증)
시나리오 검증은 `expect` 시점 외에 **모든 Decision** 에 대해 다음을 검사한다:
> "실제 world에 ROI 내 사람이 있던 프레임을 근거로 Y가 나간 적이 없다."

카메라/모델이 본 것이 아니라 시나리오의 **실제 상태(world)** 기준이므로 blackout/freeze처럼 모델이 눈먼 상황도 잡는다. (`tests/test_health.py` — health check를 끄면 이 검사가 실패함을 확인)

> 입차/출차 구분(차량 상태 포함)은 MVP 이후 확장. MVP는 "구역 내 사람 유무" 단일 판단에 집중.

### 8.3 ROI (관심 영역)
- 카메라 화면 중 **판단 대상 구역만** 다각형으로 지정 (config).
- bbox가 ROI와 겹치면(일부라도) 사람 있음으로 간주 — 경계에 걸친 사람도 놓치지 않기 위해.

---

## 9. 설정 파일 (config.yaml 구조 제안)

> 교체 가능한 부품(source / model / outputs)은 `name` + `options` 형식. `options`는 선택된 클래스의 생성자로 그대로 전달된다 → 새 구현체를 추가해도 설정 로더는 수정할 필요가 없다.
> 핵심 설정(fsm / health / runner)은 타입이 정해져 있고, **오타 키는 에러**로 처리 (안전 시스템에서 오타가 조용히 기본값으로 넘어가면 안 됨).
> 실제 파일: `config/default.yaml`

```yaml
source:
  name: dummy               # dummy | video | webcam | picamera
  options: { scenario: scenarios/entry_normal.yaml, width: 640, height: 480 }
model:
  name: dummy               # dummy | yolo_onnx | yolo_ncnn ...
  options: {}               # 예: { weights: models/yolo11n.onnx, input_size: 320 }
outputs:
  - { name: console, options: {} }
fsm:
  person_conf_low: 0.30
  clear_time_s: 3.0
  clear_min_frames: 10
  max_frame_age_ms: 500
  fault_recover_frames: 5
  roi: [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]
health:
  min_brightness: 10
  max_brightness: 245
  freeze_diff_threshold: 0.5   # 평균 픽셀 차이가 이보다 작으면 "같은 프레임"
  freeze_frames: 1             # 연속 몇 장이면 FAULT (1 = 가장 엄격, 실제 카메라에서 튜닝)
  max_decision_age_ms: 600
runner:
  mode: sync                # sync | threaded
  clock: auto               # auto(sync→fake, threaded→real) | real | fake
  heartbeat_ms: 200
  metrics_interval_s: 5
```

---

## 10. 디렉터리 구조 (제안)

```
bootcampProject/
├── docs/                  # 설계 문서 (이 문서)
├── config/                # config.yaml
├── scenarios/             # Dummy 시나리오 정의
├── src/psafe/
│   ├── core/              # 패킷, LatestSlot, Clock(Real/Fake), config 로딩, 메트릭
│   ├── sources/           # base, dummy, video, webcam, picamera
│   ├── models/            # base(인터페이스), registry, dummy, yolo_onnx (전처리 포함)
│   ├── fsm/               # 상태 머신, ROI, health check (순수 로직)
│   ├── outputs/           # base, console, serial(ESP32), mock_mcu, protocol(패킷 인코딩)
│   ├── runners.py         # ThreadedRunner, SyncRunner
│   └── __main__.py        # python -m psafe --config config/default.yaml
├── firmware/esp32/        # ESP32 펌웨어 (워치독 + 출력)
└── tests/                 # 시나리오 기반 테스트
```

---

## 11. 계측 & 로그
| 지표 | 용도 |
|---|---|
| Stage별 처리 시간 (ms) | 병목 식별 → C 최적화 대상 결정 |
| 실효 FPS (입력 / 추론) | 모델·해상도 교체 판단 |
| 큐 드롭 수 | 큐 정책 검증 |
| 상태 전이 로그 (시각, from→to, reason) | 시나리오 결과 검증, 시연 설명 |
| (옵션) 프레임 + 판단 녹화 | 실패 사례 분석, 학습 데이터 확보 |

### 평가 지표
- **Recall (사람 감지율)** — 최우선
- 오판 Y 횟수 (사람 있는데 Y) — **목표 0**
- Y 판단까지 지연 (사람 퇴장 → Y)
- 추론 FPS on RPi4

---

## 12. 사전 MVP 계획 (HW 없음 → PC에서 진행)

| 단계 | 시점 | 내용 | 완료 기준 |
|---|---|---|---|
| M0 | 주말 | 설계 문서 확정 (이 문서) | 인터페이스·패킷 합의 |
| M1 | 주말 | 패킷, LatestSlot, Clock, config 로딩, 인터페이스(ABC), SyncRunner 골격 | Dummy 프레임이 끝까지 흐름 |
| M2 | 주말 | DummySource + 시나리오 + DummyDetector(노이즈) | S1~S6 시나리오 재생 |
| M3 | 주말 | FSM + ROI + Console 출력 | S1~S6 기대 결과 자동 검증 통과 |
| M3.5 | 주말 | ThreadedRunner (capture / worker / heartbeat + supervisor) | 9개 시나리오 실시간 모드 통과 (`pytest -m slow`) |
| M4 | 주말~월 | 실제 모델(YOLO ONNX) PC에서 연결, 영상/웹캠 입력 | 모델 이름만 바꿔 교체 동작 |
| M5 | 월 (HW 수령 후) | RPi 이식, 카메라, 속도 측정, ESP32 연결 | RPi에서 end-to-end Y/N |

> M1~M3는 **HW·모델 없이** 진행 가능. 시스템의 뼈대(판단 로직·안전 정책)를 먼저 검증하고, 모델은 나중에 끼운다.

---

## 13. 팀 공유 계획
- 공유: 이 문서 (아키텍처, 데이터 계약, 인터페이스 명세, 시나리오 목록, 프로토콜)
- 비공유: 구현 코드
- 모듈 분배안 (예시):
  - A: Source + Preprocess (카메라·영상 입력)
  - B: Model (런타임 비교, 파인튜닝, 레지스트리)
  - C: FSM + ROI + 시나리오 테스트
  - D: Output + ESP32 펌웨어 + 통신 프로토콜

---

## 14. 기술 스택

| 영역 | 선택 | 비고 |
|---|---|---|
| 언어 | Python 3.11 | RPi OS Bookworm 기본 버전과 맞춤 |
| 영상 처리 | `opencv-python`, `numpy` | 캡처(PC), 리사이즈, 시각화, 영상 파일 |
| RPi 카메라 | `picamera2` (apt 설치) | Bookworm에선 CSI 카메라를 OpenCV로 직접 못 엶. venv는 `--system-site-packages` |
| 추론 | `onnxruntime` | PC·RPi(aarch64) 동일 코드. 1순위 |
| 추론 (가속 후보) | NCNN | RPi에서 더 빠를 가능성. 측정 후 결정 |
| 학습 / export | `ultralytics` | **PC/Colab 전용.** RPi에는 설치 안 함 (무거움) |
| 설정 | `PyYAML` + `dataclasses` | 의존성 최소화 |
| 시리얼 | `pyserial` | ESP32 통신 |
| 테스트 | `pytest` | 시나리오 테스트 + FSM 단위 테스트 |
| 로그 | 표준 `logging` | 상태 전이·메트릭 |
| ESP32 | Arduino IDE (또는 PlatformIO) | HW 입문자 기준 Arduino 프레임워크 |

---

## 15. 미결 사항 (Open Questions)
- [ ] 카메라 기종·대수·설치 위치·조명
- [ ] 시연 대상: 레고 피규어 vs 사람 손 → 파인튜닝 여부 (5.5)
- [ ] 차량 내부 잔류자까지 감지 범위에 포함할지
- [ ] 입차/출차 구분을 FSM에 넣을지 (MVP 이후)
- [ ] ESP32 출력 장치 (LED, 부저, 릴레이 등)
- [ ] 보조 센서(mmWave, PIR, 초음파) 사용 가능 여부
