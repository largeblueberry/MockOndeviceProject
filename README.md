# psafe — 주차 타워 안전 감지 시스템

주차 타워 입·출차 시, 카메라 영상으로 **주차 구역 안에 사람이 확실히 없는지** 판단하여 기계 동작 허가 신호(Y/N)를 출력하는 시스템이다.

> **핵심 질문:** "지금 이 순간, 주차 구역 안에 사람이 확실히 없는가?"
> 확실하지 않으면 답은 항상 **N**이다.

---

## 문서 안내

| 문서 | 내용 | 대상 |
|---|---|---|
| [`docs/RESEARCH.md`](docs/RESEARCH.md) | 문제의 근거(사고 통계·사례·제도), 기존 방법과 비교, 아이디어 확장 방향 | 주제 배경을 파악할 때 |
| [`docs/DESIGN.md`](docs/DESIGN.md) | 설계 원칙, 아키텍처, 데이터 계약, FSM, HW 구성, 통신 프로토콜, 기술 스택 | 설계 의도를 확인할 때 |
| [`docs/CODE_GUIDE.md`](docs/CODE_GUIDE.md) | 파일 간 연결 관계, 실행 흐름, 주요 설계 패턴, 권장 읽기 순서 | 코드 구조를 처음 볼 때 |
| [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md) | 엣지 케이스 목록(미해결·해결), 실험 기록과 예정 실험 | 알려진 한계와 실측 결과를 확인할 때 |
| [`docs/STRATEGY.md`](docs/STRATEGY.md) | 차별화 전략, 고장 시연 목록, 예상 질문과 답변 근거, 팀 논의 안건 | 발표·시연 방향을 정할 때 |
| [`docs/PROGRESS.md`](docs/PROGRESS.md) | 구현 현황, 다음 작업 | 현재 진행 상황을 확인할 때 |

처음 보는 경우 **RESEARCH → DESIGN → CODE_GUIDE** 순서로 읽는다.

---

## 1. 프로젝트 개요

| 항목 | 내용 |
|---|---|
| 주제 | 입·출차 시 사람이 구역을 완전히 벗어났는지 판단하는 안전 감지 SW/HW |
| 연산 장치 | Raspberry Pi 4 × 3. **A·B**: 카메라 노드 (영상 입력, 추론, 판단), **C**: 허브 (A·B 보고를 모아 최종 판정) |
| 보조 MCU | ESP32 (C에 연결. 판정 수신, LED 구동, 워치독) |
| 센서 | NIR 카메라 2대 (A·B) |
| 시연 환경 | 모형(레고 등) 기반 |
| 최종 출력 | 노드: **Y**(동작 안전) / **N**(동작 불가). C: **비었음 확인 / 사람 있음 / 확인 중 / 판단 불가** → 디스플레이 + LED. 최종 판단은 관리자 |
| 개발 언어 | Python 3.11 이상 (RPi: 3.13) |

문제 배경과 근거는 [`docs/RESEARCH.md`](docs/RESEARCH.md) §0~1, 프로젝트 정의는 [`docs/DESIGN.md`](docs/DESIGN.md) §1을 참고한다.

---

## 2. 설계 원칙

1. **Fail-safe:** 판단이 불확실하면 N. 카메라 끊김, 모델 오류, 지연 초과, 통신 단절은 모두 N이다.
2. **비대칭 판단:** 사람이 감지되면 즉시 N. Y는 일정 시간 동안 연속으로 비어 있음이 확인된 후에만 출력한다.
3. **최신성 우선:** 오래된 프레임으로 판단하지 않는다. 처리가 밀리면 이전 프레임을 버린다.
4. **모듈 교체 가능:** 입력·모델·출력은 인터페이스 뒤에 두고, 설정 파일로 교체한다.
5. **HW 없이 개발 가능:** HW 의존 부품은 모두 가상(Dummy) 구현을 함께 가진다.

상세 내용은 [`docs/DESIGN.md`](docs/DESIGN.md) §2를 참고한다.

---

## 3. 시스템 구조

```
Source ──▶ HealthMonitor ──▶ Detector ──▶ SafetyFSM ──▶ Outputs
(카메라)    (카메라 상태 검사)   (사람 검출)    (Y/N 판정)     (콘솔 · 화면 · ESP32)
```

| 구성 요소 | 역할 | 코드 | 설계 문서 |
|---|---|---|---|
| Source | 프레임 입력 (가상 시나리오, 웹캠, 영상 파일) | [`src/psafe/sources/`](src/psafe/sources/) | DESIGN §6 |
| HealthMonitor | 카메라 이상 감지 (암전, 과노출, 화면 정지) | [`src/psafe/safety/health.py`](src/psafe/safety/health.py) | DESIGN §3.5 |
| Detector | 객체 검출 (가상 모델, YOLO ONNX) | [`src/psafe/detectors/`](src/psafe/detectors/) | DESIGN §5 |
| SafetyFSM | 상태 전이 및 Y/N 판정, 감시 구역(ROI) 판정 | [`src/psafe/safety/`](src/psafe/safety/) | DESIGN §8 |
| Outputs | 판정 결과 출력 | [`src/psafe/outputs/`](src/psafe/outputs/) | DESIGN §14.3 |
| Runner | 부품 실행 루프 (Sync / Threaded) | [`src/psafe/runners.py`](src/psafe/runners.py) | DESIGN §3.4 |

위 파이프라인이 카메라 노드(A·B) 하나의 구조다. 보드 3대는 다음처럼 연결된다.

```
RPi A (카메라) ──Report──▶ ┌────────────┐ ──▶ 디스플레이 (기종 미정)
               ◀─Control── │  RPi C     │
RPi B (카메라) ──Report──▶ │  허브·판정  │ ──▶ ESP32 ──▶ LED (워치독)
               ◀─Control── └────────────┘
   UDP + JSON(+CRC-32), 200 ms마다 양방향 (별도 서버 없음)
```

| 구성 요소 | 역할 | 코드 | 설계 문서 |
|---|---|---|---|
| Link | 보드 간 메시지(Report·Control), UDP 전송, 노드별 수신 추적 | [`src/psafe/link/`](src/psafe/link/) | DESIGN §7.4 |
| ConfirmTracker | A·B: C의 확인 요청을 받은 **뒤에** 찍은 프레임만으로 비었음 재확인 | [`src/psafe/safety/confirm.py`](src/psafe/safety/confirm.py) | DESIGN §7.4.5 |
| Aggregator | C: A·B 보고 → 최종 판정(`Verdict`) | [`src/psafe/safety/aggregator.py`](src/psafe/safety/aggregator.py) | DESIGN §7.4.6 |
| VerdictOutput | C의 출력 인터페이스 (디스플레이·LED) | [`src/psafe/outputs/verdict_api.py`](src/psafe/outputs/verdict_api.py) | DESIGN §7.5 |
| Hub | C 실행 (수신 · 판정 · 출력 · Control 송신) | [`src/psafe/hub.py`](src/psafe/hub.py) | DESIGN §7.4.7 |

파일 간 의존 관계와 실행 흐름은 [`docs/CODE_GUIDE.md`](docs/CODE_GUIDE.md) §1~2에 다이어그램으로 정리되어 있다.

### 3.1 HW 구성

| 장치 | 역할 |
|---|---|
| RPi A, B | 카메라 노드: 영상 입력, 추론, FSM 판단. 판단과 사람 수를 C에 보고 |
| RPi C | 허브: 카메라 없음. 최종 판정, 디스플레이 표시, ESP32로 LED 신호 송신 |
| ESP32 (C에 연결) | 판정 수신, LED 구동, 워치독 (신호가 500 ms 끊기면 강제 N → C가 죽어도 초록이 남지 않음) |

단일 보드 구성(RPi 1대 + ESP32)도 그대로 동작한다. 연결 방식과 통신 프로토콜은 [`docs/DESIGN.md`](docs/DESIGN.md) §7을 참고한다.

---

## 4. 저장소 구성

```
bootcampProject/
├── README.md          본 문서
├── docs/              설계 · 구조 · 진행 · 조사 문서
├── config/            실행 설정 (YAML)
├── scenarios/         가상 시나리오 정의 (YAML)
├── src/psafe/         소스 코드
├── tests/             단위 · 통합 테스트
├── firmware/esp32/    ESP32 펌웨어 (Arduino)
├── models/            ONNX 모델 파일 (git 제외)
└── pyproject.toml     패키지 정의
```

| 경로 | 설명 | 참고 |
|---|---|---|
| [`config/`](config/) | `default.yaml`(가상 입력), `webcam_yolo.yaml`(웹캠 + YOLO), `video_yolo.yaml`(녹화 영상 + YOLO), `node_a.yaml`·`node_b.yaml`·`hub_c.yaml`(다중 보드, PC 한 대 시험용) | DESIGN §9, §14.2 |
| [`scenarios/`](scenarios/) | 진입·퇴장·재진입·ROI 경계·카메라 이상 등 9개 시나리오와 기대 결과. `multi/`는 다중 보드 시연용 | DESIGN §6.1~6.2 |
| [`src/psafe/`](src/psafe/) | 패키지별 역할은 CODE_GUIDE §1.3 | CODE_GUIDE §1.3 |
| [`tests/`](tests/) | 부품별 테스트 대응표는 CODE_GUIDE §7 | CODE_GUIDE §7 |
| `models/` | YOLO 모델 export 방법 | DESIGN §14.1 |

---

## 5. 시작하기

### 5.1 설치

```powershell
py -3.11 -m venv .venv
.venv\Scripts\pip install -e ".[dev,onnx,serial]"
```

Raspberry Pi에서는 설치 후 OpenCV를 화면 없는 버전으로 바꾼다 (`libGL` 없이 동작):

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,onnx,serial]"
.venv/bin/pip uninstall -y opencv-python && .venv/bin/pip install opencv-python-headless
```

YOLO 모델(`models/yolo11n_320.onnx` 등)은 저장소에 포함되지 않는다. 준비 방법은 [`docs/DESIGN.md`](docs/DESIGN.md) §14.1을 참고한다. 가상 입력(`default.yaml`)은 모델 없이 실행된다.

### 5.2 실행

```powershell
.venv\Scripts\python -m psafe                                          # 가상 시나리오 1개
.venv\Scripts\python -m psafe --scenario "scenarios/*.yaml" --check    # 전체 시나리오 검증
.venv\Scripts\python -m psafe --config config/webcam_yolo.yaml         # 웹캠 + YOLO 실시간
.venv\Scripts\python -m psafe.tools.camcheck                           # 카메라 측정
.venv\Scripts\python -m psafe.tools.bench                              # 추론 속도 측정
.venv\Scripts\python -m psafe.tools.mcucheck --list                    # 시리얼 포트 목록
.venv\Scripts\python -m psafe.tools.mcucheck --port COM5               # ESP32 펌웨어 점검
.venv\Scripts\python -m psafe --config config/serial_dummy.yaml        # 가상 판단 → ESP32
```

**다중 보드 (PC 한 대에서 시험):** 터미널 3개에서 각각 실행한다. 설정 파일에 `hub:` 절이 있으면 C로 실행된다. 한 터미널을 Ctrl+C로 끄면 C의 판정이 0.5초 안에 "판단 불가"로 바뀐다 (EXP-009).

```powershell
.venv\Scripts\python -m psafe --config config/hub_c.yaml     # C: 허브 (판정 · 가짜 ESP32)
.venv\Scripts\python -m psafe --config config/node_a.yaml    # A: 카메라 노드 (15~20초에 사람 등장)
.venv\Scripts\python -m psafe --config config/node_b.yaml    # B: 카메라 노드 (계속 비어 있음)
```

실기에서는 설정 파일의 `127.0.0.1`을 각 파이의 IP로 바꾼다 (DESIGN §9). Windows에서 한글 로그를 파일로 저장할 때는 `PYTHONUTF8=1`을 설정한다.

ESP32 펌웨어(`firmware/esp32/psafe_gate/`)는 PlatformIO(`pio run -t upload`, 폴더의 `platformio.ini` 사용) 또는 Arduino IDE로 업로드한다. 프로토콜은 [`docs/DESIGN.md`](docs/DESIGN.md) §7.3을 참고한다.

| 옵션 | 설명 |
|---|---|
| `--config` | 설정 파일 (기본값: `config/default.yaml`) |
| `--mode` | `sync`(즉시 실행) 또는 `threaded`(실시간) |
| `--scenario` | 시나리오 파일 또는 glob 패턴 |
| `--check` | 시나리오의 `expect` 항목 검증 (실패 시 종료 코드 1) |
| `--seconds` | threaded 모드(또는 C)에서 N초 후 종료 |
| `-v` / `-q` | 상세 로그 / 판단 로그 숨김 |

### 5.3 테스트

```powershell
.venv\Scripts\python -m pytest -q          # 기본 테스트
.venv\Scripts\python -m pytest -q -m slow  # 실시간 스레드 테스트
```

---

## 6. 진행 현황

| 단계 | 내용 | 상태 |
|---|---|---|
| M0 | 설계 문서 확정 | 완료 |
| M1 | 데이터 계약, LatestSlot, Clock, 설정 로딩, 인터페이스, SyncRunner | 완료 |
| M2 | 가상 Source · 시나리오 · 가상 Detector(노이즈) | 완료 |
| M3 | FSM · ROI · 카메라 이상 감지, 시나리오 자동 검증 | 완료 |
| M3.5 | ThreadedRunner (capture / worker / heartbeat) | 완료 |
| M4 | YOLO ONNX 연결, 웹캠 · 영상 입력 | 진행 중 |
| M4.5 | 시리얼 출력, 프로토콜 v1, MockMCU, ESP32 펌웨어, 점검 도구 | 완료 (실기 검증 EXP-006) |
| M5 | Raspberry Pi 이식, 카메라, RPi ↔ ESP32 연결 | 진행 중 (RPi 환경·속도 측정·Mock 연결 완료, 카메라 미정) |
| M6 | 다중 보드 통신 (A·B → C, 확인 요청, C 판정·출력) | 완료 (PC 한 대에서 프로세스 3개 실제 UDP 검증 EXP-009) |
| M6.5 | A·B·C 통합 시뮬레이션 (노드마다 다른 시계, 유실·지연·깨짐·재시작 주입) | 완료 (EXP-010: 안전 구멍 1건 발견 → 프로토콜 v2로 수정) |
| M7 | 파이 3대 실기 이식, 실제 네트워크 측정 | 예정 |

다음 작업은 [`docs/PROGRESS.md`](docs/PROGRESS.md) §4, 실험 결과와 알려진 한계는 [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md), 미결 사항은 [`docs/DESIGN.md`](docs/DESIGN.md) §15를 참고한다.

---

## 7. 계약 파일 (인터페이스)

모듈은 아래 계약을 통해서만 서로 연결된다. 계약을 바꾸면 연결된 모든 부품을 다시 확인해야 한다.

| 파일 | 내용 |
|---|---|
| [`src/psafe/core/packets.py`](src/psafe/core/packets.py) | 모듈 간 데이터 형식 (FramePacket, FrameDetections, Decision, State) |
| [`src/psafe/sources/source_api.py`](src/psafe/sources/source_api.py) | Source 인터페이스 |
| [`src/psafe/detectors/detector_api.py`](src/psafe/detectors/detector_api.py) | Detector 인터페이스 |
| [`src/psafe/outputs/output_api.py`](src/psafe/outputs/output_api.py) | Output 인터페이스 (노드) |
| [`src/psafe/link/messages.py`](src/psafe/link/messages.py) | 보드 간 메시지 형식 (Report, Control) |
| [`src/psafe/link/transport.py`](src/psafe/link/transport.py) | 전송 인터페이스 (`Transport`) |
| [`src/psafe/core/verdict.py`](src/psafe/core/verdict.py) | C의 최종 판정 형식 (Verdict, NodeView) |
| [`src/psafe/outputs/verdict_api.py`](src/psafe/outputs/verdict_api.py) | C의 출력 인터페이스 (`VerdictOutput`: 디스플레이·LED) |

이 저장소는 방향성 확인용 mock 프로젝트다 (DESIGN 머리말, §13).
