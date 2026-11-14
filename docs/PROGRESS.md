# 진행 상황 · 결과 정리

> 마지막 저장: 2026-11-14 (토) 밤. 다시 시작할 때 이 파일부터 읽기.
> 설계는 `docs/DESIGN.md`, 이 파일은 "무엇을 했고, 무엇을 알게 됐고, 다음에 뭘 하나"를 기록한다.

---

## 1. 지금까지 만든 것

### 1.1 한 줄 요약
카메라 영상으로 사람을 감지해 **"구역이 비었다(Y) / 아니다(N)"** 를 판정하는 파이프라인.
PC 웹캠과 실제 YOLO 모델로 실시간 동작까지 확인했다. 소스 약 2,200줄, 테스트 약 840줄.

```
Source ──▶ HealthMonitor ──▶ Detector ──▶ SafetyFSM ──▶ Outputs
(카메라)    (카메라가 정상인가)  (사람 찾기)    (Y/N 판정)     (콘솔·화면·[예정] ESP32)
```

### 1.2 구성 요소

| 영역 | 구현 | 역할 |
|---|---|---|
| **core** | `packets.py` | 모듈 사이에 주고받는 데이터 형식(FramePacket, Detections, Decision, State) — **팀 공유용 인터페이스** |
| | `slot.py` | 최신 프레임 1장만 유지하는 LatestSlot (지연 누적 방지) |
| | `clock.py` | RealClock / FakeClock (시간을 주입해서 테스트가 결정적으로 돌아감) |
| | `config.py`, `registry.py` | YAML 설정 → 부품 조립. 오타 키는 에러 |
| **sources** | dummy, scenario | 가상 장면 재생 (센서 노이즈, 카메라 끊김·암전·정지 흉내) |
| | webcam, video | 실제 웹캠, 녹화 영상 파일 |
| **models** | dummy | 감지 누락·오감지·지연을 일부러 넣는 가짜 모델 |
| | yolo_onnx | YOLO11n ONNX (전처리 letterbox, 후처리 NMS를 numpy로 직접 구현) |
| **fsm** | machine.py | 안전 상태 기계 INIT / OCCUPIED / VERIFYING / CLEAR / FAULT |
| | roi.py | 감시 구역(다각형) 판정 — 닿기만 해도 포함 |
| | health.py | 카메라 이상 감지: 너무 어두움 / 너무 밝음 / 화면 정지 / 빈 프레임 |
| **outputs** | console, viewer | 상태 변화 로그, 박스·ROI·Y/N 배너 표시, 녹화 |
| **runners** | SyncRunner | 가짜 시계로 즉시 실행(테스트용) |
| | ThreadedRunner | 실시간 3스레드: T1 캡처 / T2 추론+판정 / T3 하트비트(200ms) |
| **도구** | `scenario_check.py` | 시나리오 expect 자동 검증 + 안전 불변식 검사 |
| | `tools/camcheck.py` | 카메라 밝기·노이즈·fps 측정, 녹화 |

### 1.3 안전 설계 원칙 (코드에 반영됨)
- **비대칭 판단**: 사람이 1프레임만 보여도 즉시 N. Y는 3초 + 10프레임 연속으로 비어 있어야 함
- **기본값은 N**: 시작, 고장, 종료, 하트비트 끊김 → 전부 N
- **오래된 정보 거부**: 0.5초 넘은 프레임, 순서가 뒤바뀐 프레임은 FAULT
- **카메라를 의심**: 화면이 정지·암전되면 모델이 "아무도 없음"이라 해도 믿지 않음
- **스레드가 죽으면** 전체 종료 + 마지막 송신은 항상 N

### 1.4 검증
- 단위·통합 테스트 86개 통과 (`.venv\Scripts\python -m pytest -q`)
- 실시간 스레드 테스트 9개 (`pytest -m slow`)
- 가상 시나리오 9개 sync / threaded 둘 다 통과:
  entry_normal, exit_normal, reenter, flicker, roi_boundary, stale_frames, camera_outage, camera_blackout, camera_freeze
- 모든 시나리오에서 **"실제로 사람이 있는데 Y" = 0회** (안전 불변식)

### 1.5 실행 방법
```powershell
.venv\Scripts\python -m psafe                                          # 가상 시나리오 1개 (카메라 X)
.venv\Scripts\python -m psafe --scenario "scenarios/*.yaml" --check    # 전체 시나리오 검증
.venv\Scripts\python -m psafe --config config/webcam_yolo.yaml         # 웹캠 + YOLO 실시간
.venv\Scripts\python -m psafe.tools.camcheck                           # 카메라 측정
```
모델 파일(`models/yolo11n_320.onnx`, `_640.onnx`)은 git에 안 올라감 → DESIGN.md §14.1 Colab 코드로 생성.

---

## 2. 실험 결과 (2026-11-14, PC 웹캠)

### 2.1 성능
| 항목 | 결과 |
|---|---|
| 카메라 | 640x480 @ 30 fps |
| 처리량 | 캡처 30 / 모델 30 fps (버린 프레임 0) |
| 추론 시간 | 평균 **8.1~8.8 ms** (yolo11n, 입력 320, CPU) |
| 하트비트 | 200ms 주기, 지연 0회 |
| 사람 감지 | 화면 진입 약 1초 내 OCCUPIED, 얼굴을 지갑으로 가려도 OCCUPIED 유지 |
| 퇴장 → Y | 빈 화면 약 3초 후 `[Y] CLEAR` 확인 |

### 2.2 카메라 측정값
| 장면 | 프레임 차이(diff) |
|---|---|
| 가만히 앉아 있음 | 0.25 ~ 0.28 |
| 빈 배경 | 0.20 ~ 0.25 |
| 움직임 | 최대 ~58 |
| 렌즈 손으로 가림 | 0.95 ~ 9 (fps 30 → 20으로 하락) |

### 2.3 발견한 문제와 조치
| # | 문제 | 원인 | 조치 | 상태 |
|---|---|---|---|---|
| 1 | 가만히 있으면 `frozen_frame` 오탐 → Y가 안 나옴 | 정지 판정 기준 0.5가 이 웹캠 노이즈(0.20)보다 높음. 2프레임만 보고 판단 | `freeze_diff_threshold` 0.5→**0.05**, `freeze_frames` 1→**15**(0.5초) | 수정함, **재실행 확인 필요** |
| 2 | ⚠️ **렌즈를 손으로 가렸는데 Y** (21:18:24~33) | 손 틈으로 빛이 새고 자동 게인이 노이즈를 키움 → too_dark도, frozen도 안 걸림 → 모델 "사람 없음" → FSM "비었음" | 미조치. 로그에 bright/contrast 범위 출력 추가함 | **다음 작업 1순위** |

---

## 3. 의의 — 이번 주에 알게 된 것

1. **설계가 실제로 돌아간다.** 가상 데이터로 만든 구조가 부품만 바꿔(설정 파일 한 개) 실제 웹캠·실제 모델에서 그대로 동작했다. 인터페이스를 먼저 정한 효과.
2. **성능은 걱정할 단계가 아니다.** PC에서 8ms. RPi 4에서 5~10배 느려져도 10 fps 이상 기대 → 안전 판단에는 충분. 지금 C 최적화는 불필요.
3. **진짜 위험은 모델이 아니라 카메라다.** 사람은 잘 잡았다. 문제는 전부 "카메라가 제대로 보고 있나"에서 나왔다.
   - 기준값을 너무 엄격하게 → 정상인데 계속 N (가용성 문제, 안전하지만 쓸모없음)
   - 가림을 못 잡음 → 안 보이는데 Y (**안전 문제**)
   - 이 프로젝트의 핵심 과제는 "사람 감지"보다 **"감지할 수 없는 상황을 알아채는 것"** 이다.
4. **임계값은 책상에서 정할 수 없다.** 0.5는 그럴듯했지만 틀렸다. 카메라마다 측정해야 한다 → RPi 카메라로 바뀌면 다시 잰다. 그래서 측정 도구(camcheck, 상태 줄의 bright/contrast/diff)가 중요하다.
5. **가상 시나리오의 한계.** camera_blackout(완전 암전)은 테스트했지만 "손으로 가림(빛 새는 반암전 + 노이즈)"은 시나리오에 없었다. 실제 실험이 시나리오를 늘려 준다.

---

## 4. 다음 할 일

### 4.1 바로 다음 (재개 시)
1. 웹캠 재실행: ① 가만히 10초 ② 빈 배경 10초 ③ 렌즈 가림 10초 → 구간별 bright / contrast / diff 기록
2. 측정값 보고 `low_contrast` 검사 추가 (health.py) + `camera_covered` 시나리오와 테스트 추가
3. 문제 1 수정(0.05 / 15)이 실제로 오탐을 없앴는지 확인

### 4.2 그 다음
4. 640 모델과 속도·감지율 비교
5. 녹화 클립 + expect 파일로 실제 영상 자동 검증
6. ESP32 연동: `outputs/serial` + CRC8 + 200ms 하트비트, MockMCU로 테스트
   - 프레임 `| STX 0xAA | SEQ | STATE | SAFE 'Y'/'N' | CRC8 |`, ESP32는 500ms 무신호 시 강제 N
   - MVP는 USB 시리얼 115200
7. ESP32 펌웨어 (`firmware/esp32/`, Arduino IDE)
8. RPi 이식 (월요일 HW 수령 후): picamera2 source, RPi 카메라로 측정값 다시 잡기

### 4.3 미결 사항 (DESIGN §15)
- 카메라 기종 / 위치 / 조명
- 레고 미니피규어 감지 여부 (안 되면 파인튜닝 또는 사람 손으로 시연)
- 차량 내부 잔류자 포함 여부, 입출차 구분, ESP32 출력 장치, 보조 센서

### 4.4 커밋 안 된 변경
M3.5 이후 작업분(DESIGN.md, outputs/base.py 등) + 오늘 수정(health.py 측정값 로깅, runners.py 상태 줄, webcam_yolo.yaml 기준값, 이 파일).

---

## 팀 공유 원칙
코드는 그대로 넘기지 않는다. 공유: 인터페이스(`core/packets.py`, 각 `base.py`) + `docs/DESIGN.md` + 이 파일의 §2~3 (실험 결과와 교훈).
