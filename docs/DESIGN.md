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
- **Heartbeat가 Decision 나이를 직접 검사**: Worker가 `predict()` 안에서 멈추면 `on_no_frame()`조차 못 부른다. 이걸 잡는 건 Heartbeat뿐 → `decision_stale` N. 판단 시각(`decided_at`)과 함께 **Y의 근거 프레임 촬영 시각(`timestamp`)도 본다.** Y는 근거 영상보다 `max_decision_age_ms` 넘게 살아남지 않는다 (`expire_decision`, E-10).
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

### 4.2 FrameDetections (Model → FSM)
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
| `count` | int | 그 프레임에서 ROI 안에 있던 사람 수 (`OCCUPIED`일 때만 > 0, 기본 0). 보드 간 통신용 (7.4) |

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
| `predict(frame_packet) -> FrameDetections` | **전처리 + 추론 + 후처리(NMS, 좌표 정규화, 클래스명 통일)** 까지 책임 |
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

### 7.3 통신 프로토콜 (v1, 2026-11-16 확정)
RPi → ESP32, **주기 200ms** (Decision이 바뀌지 않아도 계속 송신 = 하트비트 겸용). USB 시리얼 115200 bps, 8N1.

```
| STX(0xAA) | SEQ(1B) | STATE(1B) | SAFE(1B: 'Y'/'N') | CRC8(1B) |
```

| 필드 | 값 |
|---|---|
| STX | `0xAA` (패킷 시작) |
| SEQ | 송신할 때마다 1씩 증가, 255 다음은 0 |
| STATE | `INIT=0`, `OCCUPIED=1`, `VERIFYING=2`, `CLEAR=3`, `FAULT=4` |
| SAFE | `'Y'`(0x59) 또는 `'N'`(0x4E). `'Y'`는 STATE가 `CLEAR`일 때만 유효 |
| CRC8 | SEQ·STATE·SAFE 3바이트에 대한 CRC-8. 다항식 `0x07`, 초기값 `0x00`, 반사 없음 (검사값: `"123456789"` → `0xF4`) |

수신 규칙 (ESP32):
- 최근 5바이트를 밀어 가며 본다(슬라이딩 윈도). 첫 바이트가 STX이고 CRC가 맞으면 패킷으로 인정한다. 바이트가 쪼개져 오거나 중간부터 들어와도 다시 맞춰진다.
- **무시하는 패킷:** CRC 오류, 규칙 위반(STATE 범위 밖, `CLEAR`가 아닌데 `'Y'`), 직전과 같은 SEQ(같은 패킷 반복은 새 정보가 아님).
- **500ms 동안 유효 패킷 없음 → 강제 N.** 무시가 누적되어도 결국 이 규칙으로 N이 된다.
- 부팅 직후는 항상 N.
- ESP32 → RPi: 상태 보고용 텍스트 줄(`BOOT`, `GATE`, `STAT`)을 같은 시리얼로 보낸다. 판단에는 쓰지 않는다. RPi `serial_out`은 이 줄을 읽어 로그로 남기고(관문 상태와 마지막 송신 판단을 나란히 기록), 점검 도구(`tools/mcucheck.py`)는 이 줄로 동작을 판정한다. ACK는 MVP 범위 밖 (확장 항목).

구현: RPi `src/psafe/outputs/protocol.py`(인코딩·수신 로직), ESP32 `firmware/esp32/psafe_gate/psafe_gate.ino`. **두 파일의 상수는 항상 같아야 한다.**

### 7.4 보드 간 통신 (v0 초안, 2026-11-19)

> 상태: 초안. 임계값(`K`, `confirm_timeout_s` 등)은 팀 합의 후 확정.

#### 7.4.1 구성
```
 [RPi A] NIR 카메라 ─┐                       ┌─► Display (추상화, 7.5)
   (출구 쪽)         │  report (200ms, UDP)  │
                     ├──────────► [RPi C] ───┤
 [RPi B] NIR 카메라 ─┘ ◄──────────  허브·판정 └─► ESP32 ─► LED (프로토콜 v1, 7.3)
   (반대쪽)            control (200ms, UDP)
```
- **A, B**: 기존 파이프라인 그대로 사람을 센다. 판단 결과를 C로 보고한다.
- **C**: 카메라 없음. A·B 보고를 모아 최종 판정(`Verdict`)을 낸다. **별도 서버를 두지 않고 C가 허브를 맡는다.**
- 최종 판단은 사람(주차 관리자)이 한다. 시스템은 판정을 **보여 주기만** 한다. 문은 자동으로 닫지 않는다.

#### 7.4.2 전송 방식 — UDP + JSON

| 항목 | 결정 | 이유 |
|---|---|---|
| 형식 | JSON (UTF-8, 메시지 1개 = UDP 데이터그램 1개) | 사람이 읽을 수 있어 로그·디버깅이 쉽다. 200 B 수준이라 파싱 비용은 무시할 수 있다 |
| 전송 | **UDP** | TCP는 밀린 데이터를 버리지 않고 늦게라도 배달한다. 오래된 보고가 한꺼번에 도착하는 것은 LatestSlot(3.2)으로 막으려던 문제와 같다. UDP는 하나를 잃어도 200 ms 뒤 다음 보고가 온다 |
| 방식 | **명령이 아니라 상태를 주기적으로 보낸다** (양방향 200 ms) | 하트비트가 따로 필요 없고, ACK·재전송·중복 처리도 필요 없다. 프로토콜 v1과 같은 철학 |
| 주소 | 고정 IP + 포트, config에 기록 (C 수신 `5005`, A·B 수신 `5006`) | 자동 탐색(mDNS)은 고장 지점만 늘린다 |
| 네트워크 | 유선 스위치 권장 | Wi-Fi면 손실·지연을 측정한 뒤 `node_timeout_ms`를 다시 정한다 |
| 구현 | Python 표준 `socket` + `json` | 의존성 없음. PC 한 대에서 포트만 바꿔 A·B·C를 모두 띄울 수 있다 (원칙 6) |
| 보안 | MVP 생략 (폐쇄망 가정) | 확장 시 HMAC 필드 추가 |

비교한 대안: HTTP(요청/응답 구조라 주기 보고에 맞지 않고 TCP 문제가 그대로 있다), MQTT(C에 브로커를 설치해야 하므로 사실상 서버가 생긴다. 3대 규모에 과하다), TCP JSON 줄 단위(오래된 데이터가 쌓이고 재연결을 처리해야 한다).

#### 7.4.3 시계 규칙 — 파이끼리 시각을 비교하지 않는다
파이마다 시계가 다르므로(수 초씩 어긋날 수 있다) **A의 시각을 C의 시계와 비교하지 않는다.**
- A·B는 보낼 때 자기 시계로 **"판단 근거 프레임이 몇 ms 전 것인가"(`evidence_age_ms`)** 를 계산해 싣는다.
- C는 받은 시각을 자기 시계로 기록한다.
- `C가 보는 근거 나이 = evidence_age_ms + (C_now − C_수신시각)`. 네트워크 지연(LAN에서 수 ms)은 무시한다.

#### 7.4.4 메시지

**A/B → C: `report`** (200 ms마다, 판단이 바뀌지 않아도 송신)
```json
{"v":1, "type":"report", "node":"A", "boot":"3f2a91", "seq":1234,
 "state":"CLEAR", "safe":true, "count":0, "reason":"clear",
 "evidence_age_ms":120, "confirm_id":7, "confirm":"empty"}
```

| 필드 | 값 |
|---|---|
| `v` | 프로토콜 버전. 지금은 `1` |
| `node` | `"A"` / `"B"` |
| `boot` | 프로세스를 시작할 때마다 새로 만드는 임의 문자열. 재시작으로 `seq`가 0이 되어도 이상이 아니라 재시작으로 구분한다 |
| `seq` | 송신마다 1씩 증가 (같은 `boot` 안에서) |
| `state`, `safe`, `reason` | 그 노드의 최신 `Decision` (4.3) |
| `count` | ROI 안의 사람 수. **`Decision`에 필드 추가 필요** (7.4.7) |
| `evidence_age_ms` | 근거 프레임 나이. 근거 프레임이 없으면(`seq == -1`) `null` |
| `confirm_id` | 마지막으로 받은 C의 `confirm_id`. 없으면 `null` |
| `confirm` | `null`(요청 없음) / `"pending"` / `"empty"` / `"occupied"` / `"fault"` |

**C → A/B: `control`** (200 ms마다, A·B 각각에게)
```json
{"v":1, "type":"control", "node":"C", "boot":"c01d2e", "seq":88, "confirm_id":7}
```
- `confirm_id`: 진행 중이거나 유지 중인 확인 번호. 없으면 `null`. 새 확인을 시작할 때만 값을 바꾼다.

**수신 규칙 (양쪽 공통)**
- JSON 파싱 실패, `v` 불일치, 필드 누락·타입·범위 위반 → 무시하고 카운트
- `safe == true`인데 `state != "CLEAR"` → 규칙 위반으로 무시 (프로토콜 v1과 같은 규칙)
- 같은 `boot`에서 이전보다 작거나 같은 `seq` → 무시 (지연 도착·중복)
- `boot`가 바뀌면 재시작으로 보고 받아들인다. 단 **이미 지나간 `boot`의 메시지는 무시**한다 (재시작 전 프로세스가 보낸 메시지가 늦게 도착하면 옛 판단이 되살아난다)
- 필드 집합은 정확히 일치해야 한다 (빠진 필드·모르는 필드 모두 거부). 형식을 바꾸면 `v`를 올린다. 512 B 초과, `NaN` 등 비표준 JSON도 거부
- 판단에 쓰는 시각은 모두 **받는 쪽 시계**다 (7.4.3)
- **노드별로 마지막 유효 메시지 후 `node_timeout_ms`(500 ms) 동안 유효 메시지가 없으면 그 노드는 무응답**
- 부팅 직후는 모든 노드가 무응답 상태로 시작한다

#### 7.4.5 CONFIRM — 명령 + ACK가 아니라 상태 echo
1. C는 A·B가 모두 `CLEAR`(Y)이고 신선하면 **새 `confirm_id`** 를 발급해 `control`에 계속 싣는다.
2. A·B는 처음 보는 `confirm_id`를 받으면 **받은 시점 이후에 촬영한 프레임만으로** 확인한다. 이전 판단을 재사용하지 않는다.
   - 신선하고 정상인 프레임 `K`장이 모두 사람 0명 → `"empty"`
   - 그 사이 사람 감지 → `"occupied"`, 카메라 이상·stale → `"fault"`
   - 결과가 나올 때까지는 `"pending"`. 결과는 같은 `confirm_id`와 함께 계속 echo한다.
3. C는 **A와 B 모두 현재 `confirm_id`에 대해 `"empty"`** 라고 답했을 때만 `EMPTY`를 낸다.
4. `confirm_timeout_s` 안에 결과가 없으면 `UNKNOWN(confirm_timeout)`으로 보고, 조건이 다시 맞으면 새 `confirm_id`로 재시도한다.
5. `EMPTY`는 A·B가 계속 `CLEAR`·신선함을 유지하는 동안만 지속된다. 어느 한쪽이라도 깨지면 즉시 해제되고, 다시 `EMPTY`를 내려면 **새 확인이 필요하다.**

메시지가 유실되거나 중복돼도 다음 주기에 같은 상태가 다시 오므로, 재전송 로직을 따로 두지 않는다.

#### 7.4.6 C의 판정 (`Verdict`)

| `kind` | 조건 (위에서부터 우선) | 프로토콜 v1 매핑 (→ ESP32) |
|---|---|---|
| `UNKNOWN` | 어느 노드든 무응답, `FAULT`, 근거가 오래됨, 버전 불일치, 확인 시간 초과, C 부팅 직후 | `FAULT` / N |
| `OCCUPIED` | 어느 노드든 `count > 0` 또는 `confirm == "occupied"` | `OCCUPIED` / N |
| `CHECKING` | 둘 다 사람 0명, CONFIRM 진행 중 | `VERIFYING` / N |
| `EMPTY` | 7.4.5의 3번 조건 충족 | `CLEAR` / **Y** |

- **판단 기준은 "합계 == 0"이다.** A·B가 겹쳐 보는 구역에서 같은 사람이 두 번 세어져도 0이 아니게 되므로 안전 쪽 오류다. 진짜 위험은 겹침이 아니라 **두 카메라 모두의 사각지대**다 (현장 사각지대 시험 필요).
- 같은 사람이 두 번 세어질 수 있으므로 **화면에는 합계가 아니라 노드별 인원**을 보여 준다.
- 사람 감지는 즉시 반영하고, `EMPTY`는 확인을 거친 뒤에만 낸다 (비대칭 판단, 원칙 3).
- Aggregator는 FSM처럼 **I/O 없는 순수 로직**으로 만든다: `step(reports, now) -> Verdict`. SyncRunner + FakeClock으로 "B 무응답", "확인 중 A가 사람 감지", "A 재시작" 같은 시나리오를 테스트한다.

#### 7.4.7 기존 구조와의 연결
- **구현됨 (2026-11-19):** `src/psafe/link/` — `messages.py`(형식·검증), `transport.py`(`Transport` 인터페이스, `UdpTransport`, 고장 주입 가능한 `LoopbackNetwork`), `peer.py`(`PeerTracker`). 테스트 `tests/test_link.py`.
- **구현됨 (2026-11-19, A·B 쪽):** `Decision.count`, `safety/confirm.py`(`ConfirmTracker`), `link/node.py`(`NodeLink`), Runner 연결. 테스트 `tests/test_confirm.py`.
  - 설정에 `link:` 절이 있을 때만 켜진다 (`node`, `hub`, `bind`, `hub_node`, `node_timeout_ms`, `confirm_frames`). 없으면 지금까지와 똑같이 단독 실행.
  - 스레드: `link-rx`(새로 추가)가 Control을 받아 현재 요청을 갱신 → `worker`가 판단마다 `ConfirmTracker.step(요청, Decision)` → 판단과 확인 상태를 **한 쌍으로** 게시 → `heartbeat`가 기존 Output과 함께 Report 송신.
  - 요청은 `(C의 boot, confirm_id)`로 구분한다. C가 재시작해 같은 번호를 다시 써도 새 요청이 된다. 요청을 받은 시각은 **처음 받은 시각**으로 고정된다 (매 Control마다 갱신하면 근거 프레임이 영원히 쌓이지 않는다).
  - 하트비트가 판단을 `decision_stale` N으로 바꿔 보낼 때, 확인 결과도 `fault`로 바꾼다. 종료 시 마지막 Report도 `shutdown` + `fault`.
  - **C 쪽 구현 시 주의:** `confirm_id`는 부팅마다 임의 값에서 시작한다. 0부터 시작하면, 재시작 전 번호에 대한 A의 `empty` echo가 늦게 도착했을 때 새 요청에 대한 답으로 오인할 수 있다.
- **A·B:** 새 Output `udp_report` 하나를 추가한다. `send()`가 이미 하트비트(200 ms)마다 호출되므로 구조가 그대로 맞는다. 다만 `control` 수신(`confirm_id`)을 FSM 쪽으로 전달할 경로가 필요하다. 지금 Output은 송신만 하므로 이 부분은 새로 설계한다.
- **계약 변경:** `Decision`에 `count: int`(ROI 안 사람 수)를 추가한다. 4.3과 `core/packets.py`를 함께 고친다.
- **C:** 카메라 파이프라인이 아니라 `UdpReceiver → Aggregator → VerdictOutput들(7.5)` 구조다. 하트비트 스레드가 200 ms마다 최신 `Verdict`를 모든 출력에 전달한다.

### 7.5 C의 출력 — 디스플레이 추상화 + LED

#### 7.5.1 역할 분담
| 출력 | 역할 | C가 죽으면 |
|---|---|---|
| **LED (ESP32 경유)** | **기준 신호.** 관리자가 1차로 보는 것 | ESP32 워치독(500 ms)이 N으로 바꾼다. 초록이 남지 않는다 |
| **디스플레이** | 보조 정보 (노드별 인원, 사유, 확인 진행 상황) | 화면이 멈출 수 있다 → 7.5.3 규칙으로 드러낸다 |

> ⚠️ C의 GPIO로 LED를 직접 켜지 않는다. 프로그램이 죽어도 핀은 마지막 상태(초록)로 남는다. ESP32와 프로토콜 v1(7.3)을 그대로 재사용하고, `Verdict → (STATE, SAFE)` 매핑은 7.4.6 표를 따른다. STATE별 LED 색(노랑 = `VERIFYING` 등)은 펌웨어에서 처리한다.

#### 7.5.2 `Verdict` (Aggregator → 출력)
| 필드 | 타입 | 설명 |
|---|---|---|
| `kind` | `EMPTY` / `OCCUPIED` / `CHECKING` / `UNKNOWN` | 최종 판정 |
| `reason` | str | 예: `confirmed`, `person_A`, `node_B_timeout`, `confirm_timeout` |
| `nodes` | dict[str, NodeView] | 노드별 `count`, `state`, 무응답 여부, 근거 나이(ms) |
| `confirm_id` | int \| None | 현재 확인 번호 |
| `decided_at` | float | 판정 시각 (C의 Clock 기준) |

#### 7.5.3 디스플레이 인터페이스 (기종 미정 → 추상화)
디스플레이 종류(HDMI 모니터, 소형 LCD, 웹 페이지 등)는 아직 정하지 않는다. **"최종 판정을 받아 보여 준다"** 는 역할은 바뀌지 않으므로 인터페이스만 고정한다. 모양은 기존 `Output`(14.3)과 같다.

```
VerdictOutput
  open()                    장치 준비
  show(verdict)             하트비트마다(200 ms) 호출. 바뀔 때만이 아니다. 가볍고 막히지 않아야 한다
  poll() -> bool            선택. 메인 스레드에서 호출 (GUI용). False = 종료 요청
  close()                   종료. 가능하면 "판단 불가" 화면을 남긴다
```

모든 구현체가 지켜야 할 표시 규칙:
1. `kind`를 색과 글자로 함께 표시한다 (색만으로 구분하지 않는다).
2. `OCCUPIED`이면 **노드별 인원**을 표시한다 (합계 아님, 7.4.6).
3. **`UNKNOWN`이면 반드시 사유를 표시한다.**
4. **살아 있음 표시:** 매 `show()`마다 바뀌는 요소(시계, 깜빡이는 점)와 "마지막 갱신 N초 전"을 넣는다. C가 멈추면 관리자가 화면이 멈춘 것을 알 수 있어야 한다.
5. 구현체가 C와 **다른 프로세스나 장치**(브라우저, 별도 MCU의 LCD 등)라면, `show()`가 1초 넘게 오지 않을 때 스스로 화면 전체를 `UNKNOWN(display_stale)`으로 바꾼다.

먼저 만들 구현체: `console`(텍스트), `mock`(테스트용 기록). 실제 디스플레이는 기종이 정해지면 구현체를 하나 추가한다.

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
| `frame_gap` | FSM | 직전 처리 프레임과 촬영 간격 > `max_frame_age_ms`. 그 사이 구역을 아무도 보지 않았으므로 재검증 (E-11) |
| `no_frame` | Runner | 프레임 대기 타임아웃 |
| `detector_error` | Runner | `predict()` 예외 — 루프는 죽지 않고 N |
| `too_dark` / `too_bright` | HealthMonitor | 렌즈 가림, 과노출. 카메라 이상이면 **추론을 건너뛰고** 바로 N (E-12) |
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
  max_frame_age_ms: 500     # 프레임 유효 기간: 이보다 오래된 프레임, 또는 이 시간 동안 프레임 없음 → FAULT
  fault_recover_frames: 5
  roi: [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]
health:
  min_brightness: 10
  max_brightness: 245
  freeze_diff_threshold: 0.5   # 평균 픽셀 차이가 이보다 작으면 "같은 프레임"
  freeze_frames: 1             # 연속 몇 장이면 FAULT (1 = 가장 엄격, 실제 카메라에서 튜닝)
runner:
  mode: sync                # sync | threaded
  clock: auto               # auto(sync→fake, threaded→real) | real | fake
  heartbeat_ms: 200
  max_decision_age_ms: 600  # heartbeat가 보낼 판단(또는 Y의 근거 프레임)이 이보다 오래되면 N
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
│   ├── sources/           # source_api(인터페이스), dummy, video, webcam, picamera
│   ├── detectors/         # detector_api(인터페이스), dummy, yolo_onnx (전처리 포함)
│   ├── safety/            # fsm(상태 머신), roi, health (순수 로직)
│   ├── link/              # 보드 간 통신: messages(Report·Control), transport(UDP·Loopback), peer(수신 추적)
│   ├── outputs/           # output_api(인터페이스), console, viewer, serial_out(ESP32), mock_mcu, protocol(패킷 인코딩)
│   ├── runners.py         # ThreadedRunner, SyncRunner
│   └── __main__.py        # python -m psafe --config config/default.yaml
├── firmware/esp32/psafe_gate/  # ESP32 펌웨어 (패킷 수신 + 워치독 + 출력)
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
| M4.5 | 월 | 시리얼 출력 + 프로토콜 v1 + MockMCU + ESP32 펌웨어 + 점검 도구 | `mcucheck` 7/7 통과 (실기) — 2026-11-16 완료 |
| M5 | M4.5 이후 | RPi 이식, 카메라, 속도 측정, RPi ↔ ESP32 연결 | RPi에서 end-to-end Y/N |

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
| 언어 | Python 3.11 이상 | PC 3.11, RPi는 Debian 13(trixie) 기본 3.13 (2026-11-16 확인) |
| 영상 처리 | `opencv-python`, `numpy` | 캡처(PC), 리사이즈, 시각화, 영상 파일. **RPi는 `opencv-python-headless`** (화면 없음, libGL 불필요) |
| RPi 카메라 | `picamera2` (apt 설치) | Bookworm에선 CSI 카메라를 OpenCV로 직접 못 엶. venv는 `--system-site-packages` |
| 추론 | `onnxruntime` | PC·RPi(aarch64) 동일 코드. 1순위 |
| 추론 (가속 후보) | NCNN | RPi에서 더 빠를 가능성. 측정 후 결정 |
| 학습 / export | `ultralytics` | **PC/Colab 전용.** RPi에는 설치 안 함 (무거움) |
| 설정 | `PyYAML` + `dataclasses` | 의존성 최소화 |
| 시리얼 | `pyserial` | ESP32 통신 |
| 테스트 | `pytest` | 시나리오 테스트 + FSM 단위 테스트 |
| 로그 | 표준 `logging` | 상태 전이·메트릭 |
| ESP32 | Arduino IDE (또는 PlatformIO) | HW 입문자 기준 Arduino 프레임워크 |

### 14.1 모델 준비 (ONNX export)
`ultralytics`(+PyTorch, 1~2GB)는 **변환에만** 필요하므로 Colab에서 한 번 실행하고 `.onnx`만 가져온다.

```python
!pip install -q ultralytics onnx onnxslim
from ultralytics import YOLO
import shutil
for size in (320, 640):
    path = YOLO("yolo11n.pt").export(format="onnx", imgsz=size, opset=12, simplify=True, dynamic=False)
    shutil.move(path, f"yolo11n_{size}.onnx")
from google.colab import files
files.download("yolo11n_320.onnx"); files.download("yolo11n_640.onnx")
```
→ `models/` 에 저장. 파인튜닝 모델도 같은 방식으로 export하면 `YoloOnnxDetector`가 ONNX 메타데이터에서 클래스 이름을 읽는다. 클래스명이 `person`이 아니면 `class_map: { minifig: person }`.

### 14.2 실행 구성 (config/)
| 파일 | 입력 | 모델 | 용도 |
|---|---|---|---|
| `default.yaml` | dummy 시나리오 | dummy | 로직 개발·테스트 |
| `webcam_yolo.yaml` | 웹캠 | YOLO ONNX | 실시간 + 뷰어 (시연 화면) |
| `video_yolo.yaml` | 녹화 클립 | YOLO ONNX | 반복 가능한 모델 비교 |

카메라 점검·클립 녹화: `python -m psafe.tools.camcheck [--record recordings/x.mp4]` — health 임계값(밝기, 프레임 차이)을 **실측**해서 설정에 반영한다.

### 14.3 Output 훅
| 메서드 | 스레드 | 호출 시점 | 용도 |
|---|---|---|---|
| `send(decision)` | heartbeat | 200ms마다 | ESP32, 콘솔 |
| `on_frame(pkt, dets, decision)` | worker | 처리된 프레임마다 | 뷰어, 녹화 (Decision 게시 **후** 호출 → 그리기가 판단을 늦추지 않음) |
| `poll() -> bool` | main | ~30Hz | GUI(imshow/waitKey). False = 종료 요청 |

---

## 15. 미결 사항 (Open Questions)
- [ ] 카메라 기종·대수·설치 위치·조명
- [ ] 시연 대상: 레고 피규어 vs 사람 손 → 파인튜닝 여부 (5.5)
- [ ] 차량 내부 잔류자까지 감지 범위에 포함할지
- [ ] 입차/출차 구분을 FSM에 넣을지 (MVP 이후)
- [ ] ESP32 출력 장치 (LED, 부저, 릴레이 등) → LED는 ESP32 경유로 결정 (7.5). 색·개수 미정
- [ ] 디스플레이 기종 (7.5.3은 인터페이스만 고정)
- [ ] 보드 간 통신 임계값: CONFIRM 프레임 수 `K`, `confirm_timeout_s`, `node_timeout_ms` (7.4)
- [ ] 네트워크: 유선 스위치 vs Wi-Fi (Wi-Fi면 손실·지연 실측 후 `node_timeout_ms` 재결정)
- [ ] A·B가 C의 `confirm_id`를 받아 FSM에 전달하는 경로 (7.4.7)
- [ ] 보조 센서(mmWave, PIR, 초음파) 사용 가능 여부
