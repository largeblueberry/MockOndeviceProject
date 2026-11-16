# psafe — 주차 타워 안전 감지 시스템

주차 타워 입·출차 시, 카메라 영상으로 **주차 구역 안에 사람이 확실히 없는지** 판단하여 기계 동작 허가 신호(Y/N)를 출력하는 시스템이다.

> **핵심 질문:** "지금 이 순간, 주차 구역 안에 사람이 확실히 없는가?"
> 확실하지 않으면 답은 항상 **N**이다.

---

## 문서 안내

| 문서 | 내용 | 대상 |
|---|---|---|
| [`docs/RESEARCH.md`](docs/RESEARCH.md) | 문제의 근거(사고 통계·사례·해외 기준), 기존 방법과 비교, 아이디어 확장 방향 | 주제 배경을 파악할 때 |
| [`docs/DESIGN.md`](docs/DESIGN.md) | 설계 원칙, 아키텍처, 데이터 계약, FSM, HW 구성, 통신 프로토콜, 기술 스택 | 설계 의도를 확인할 때 |
| [`docs/CODE_GUIDE.md`](docs/CODE_GUIDE.md) | 파일 간 연결 관계, 실행 흐름, 주요 설계 패턴, 권장 읽기 순서 | 코드 구조를 처음 볼 때 |
| [`docs/PROGRESS.md`](docs/PROGRESS.md) | 구현 현황, 실험 결과, 다음 작업 | 현재 진행 상황을 확인할 때 |

처음 보는 경우 **RESEARCH → DESIGN → CODE_GUIDE** 순서로 읽는다.

---

## 1. 프로젝트 개요

| 항목 | 내용 |
|---|---|
| 주제 | 입·출차 시 사람이 구역을 완전히 벗어났는지 판단하는 안전 감지 SW/HW |
| 연산 장치 | Raspberry Pi 4 (영상 입력, 추론, 판단) |
| 보조 MCU | ESP32 (판단 결과 수신, I/O 제어, 워치독) |
| 센서 | 카메라 |
| 시연 환경 | 모형(레고 등) 기반 |
| 최종 출력 | **Y**(동작 안전) / **N**(동작 불가) |
| 개발 언어 | Python 3.11 |

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
| HealthMonitor | 카메라 이상 감지 (암전, 과노출, 화면 정지) | [`src/psafe/fsm/health.py`](src/psafe/fsm/health.py) | DESIGN §3.5 |
| Detector | 객체 검출 (가상 모델, YOLO ONNX) | [`src/psafe/models/`](src/psafe/models/) | DESIGN §5 |
| SafetyFSM | 상태 전이 및 Y/N 판정, 감시 구역(ROI) 판정 | [`src/psafe/fsm/`](src/psafe/fsm/) | DESIGN §8 |
| Outputs | 판정 결과 출력 | [`src/psafe/outputs/`](src/psafe/outputs/) | DESIGN §14.3 |
| Runner | 부품 실행 루프 (Sync / Threaded) | [`src/psafe/runners.py`](src/psafe/runners.py) | DESIGN §3.4 |

파일 간 의존 관계와 실행 흐름은 [`docs/CODE_GUIDE.md`](docs/CODE_GUIDE.md) §1~2에 다이어그램으로 정리되어 있다.

### 3.1 HW 구성

| 장치 | 역할 |
|---|---|
| Raspberry Pi 4 | 영상 입력, 추론, FSM 판단, 로그·녹화 |
| ESP32 | 판단 결과 수신, 릴레이·LED·부저 제어, 워치독 (신호가 500 ms 끊기면 강제 N) |

연결 방식과 통신 프로토콜은 [`docs/DESIGN.md`](docs/DESIGN.md) §7을 참고한다.

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
├── models/            ONNX 모델 파일 (git 제외)
└── pyproject.toml     패키지 정의
```

| 경로 | 설명 | 참고 |
|---|---|---|
| [`config/`](config/) | `default.yaml`(가상 입력), `webcam_yolo.yaml`(웹캠 + YOLO), `video_yolo.yaml`(녹화 영상 + YOLO) | DESIGN §9, §14.2 |
| [`scenarios/`](scenarios/) | 진입·퇴장·재진입·ROI 경계·카메라 이상 등 9개 시나리오와 기대 결과 | DESIGN §6.1~6.2 |
| [`src/psafe/`](src/psafe/) | 패키지별 역할은 CODE_GUIDE §1.3 | CODE_GUIDE §1.3 |
| [`tests/`](tests/) | 부품별 테스트 대응표는 CODE_GUIDE §7 | CODE_GUIDE §7 |
| `models/` | YOLO 모델 export 방법 | DESIGN §14.1 |

---

## 5. 시작하기

### 5.1 설치

```powershell
py -3.11 -m venv .venv
.venv\Scripts\pip install -e ".[dev,onnx]"
```

YOLO 모델(`models/yolo11n_320.onnx` 등)은 저장소에 포함되지 않는다. 준비 방법은 [`docs/DESIGN.md`](docs/DESIGN.md) §14.1을 참고한다. 가상 입력(`default.yaml`)은 모델 없이 실행된다.

### 5.2 실행

```powershell
.venv\Scripts\python -m psafe                                          # 가상 시나리오 1개
.venv\Scripts\python -m psafe --scenario "scenarios/*.yaml" --check    # 전체 시나리오 검증
.venv\Scripts\python -m psafe --config config/webcam_yolo.yaml         # 웹캠 + YOLO 실시간
.venv\Scripts\python -m psafe.tools.camcheck                           # 카메라 측정
```

| 옵션 | 설명 |
|---|---|
| `--config` | 설정 파일 (기본값: `config/default.yaml`) |
| `--mode` | `sync`(즉시 실행) 또는 `threaded`(실시간) |
| `--scenario` | 시나리오 파일 또는 glob 패턴 |
| `--check` | 시나리오의 `expect` 항목 검증 (실패 시 종료 코드 1) |
| `--seconds` | threaded 모드에서 N초 후 종료 |
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
| M5 | Raspberry Pi 이식, ESP32 연결 | 예정 |

실험 결과와 다음 작업은 [`docs/PROGRESS.md`](docs/PROGRESS.md) §2~4, 미결 사항은 [`docs/DESIGN.md`](docs/DESIGN.md) §15를 참고한다.

---

## 7. 팀 공유 범위

팀에는 **인터페이스와 설계 문서**를 공유하며, 각 모듈은 담당자가 인터페이스 명세에 맞춰 직접 구현한다.

| 공유 대상 | 내용 |
|---|---|
| [`src/psafe/core/packets.py`](src/psafe/core/packets.py) | 모듈 간 데이터 형식 (FramePacket, Detections, Decision, State) |
| [`src/psafe/sources/base.py`](src/psafe/sources/base.py) | Source 인터페이스 |
| [`src/psafe/models/base.py`](src/psafe/models/base.py) | Detector 인터페이스 |
| [`src/psafe/outputs/base.py`](src/psafe/outputs/base.py) | Output 인터페이스 |
| [`docs/`](docs/) | 설계 · 구조 · 조사 문서 |

모듈 분배안은 [`docs/DESIGN.md`](docs/DESIGN.md) §13을 참고한다.
