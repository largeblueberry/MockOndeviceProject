# 진행 상황 · 결과 정리

> 마지막 저장: 2026-11-16 (월). 다시 시작할 때 이 파일부터 읽기.
> 설계는 `docs/DESIGN.md`, 이 파일은 "무엇을 했고, 무엇을 알게 됐고, 다음에 뭘 하나"를 기록한다.

---

## 1. 지금까지 만든 것

### 1.1 한 줄 요약
카메라 영상으로 사람을 감지해 **"구역이 비었다(Y) / 아니다(N)"** 를 판정하는 파이프라인.
PC 웹캠과 실제 YOLO 모델로 실시간 동작까지 확인했다. ESP32로 Y/N을 보내는 시리얼 출력과 펌웨어를 작성하고 실제 ESP32에서 검증했다 (EXP-006).

```
Source ──▶ HealthMonitor ──▶ Detector ──▶ SafetyFSM ──▶ Outputs
(카메라)    (카메라가 정상인가)  (사람 찾기)    (Y/N 판정)     (콘솔·화면·ESP32)
```

### 1.2 구성 요소

| 영역 | 구현 | 역할 |
|---|---|---|
| **core** | `packets.py` | 모듈 사이에 주고받는 데이터 형식(FramePacket, FrameDetections, Decision, State) — **팀 공유용 인터페이스** |
| | `slot.py` | 최신 프레임 1장만 유지하는 LatestSlot (지연 누적 방지) |
| | `clock.py` | RealClock / FakeClock (시간을 주입해서 테스트가 결정적으로 돌아감) |
| | `config.py`, `registry.py` | YAML 설정 → 부품 조립. 오타 키는 에러 |
| **sources** | dummy, scenario | 가상 장면 재생 (센서 노이즈, 카메라 끊김·암전·정지 흉내) |
| | webcam, video | 실제 웹캠, 녹화 영상 파일 |
| **detectors** | dummy | 감지 누락·오감지·지연을 일부러 넣는 가짜 모델 |
| | yolo_onnx | YOLO11n ONNX (전처리 letterbox, 후처리 NMS를 numpy로 직접 구현) |
| **safety** | fsm.py | 안전 상태 기계 INIT / OCCUPIED / VERIFYING / CLEAR / FAULT |
| | roi.py | 감시 구역(다각형) 판정 — 닿기만 해도 포함 |
| | health.py | 카메라 이상 감지: 너무 어두움 / 너무 밝음 / 화면 정지 / 빈 프레임 |
| **outputs** | console, viewer | 상태 변화 로그, 박스·ROI·Y/N 배너 표시, 녹화 |
| | protocol | RPi → ESP32 패킷 v1 (인코딩, CRC-8, 수신 로직) |
| | serial_out | USB 시리얼로 ESP32에 200ms마다 송신, 끊기면 1초마다 재연결 |
| | mock_mcu | ESP32 없이 같은 수신 로직으로 관문 상태 확인 |
| **firmware** | `esp32/psafe_gate` | 패킷 수신, 500ms 워치독, LED/릴레이 출력, 상태 보고 |
| **runners** | SyncRunner | 가짜 시계로 즉시 실행(테스트용) |
| | ThreadedRunner | 실시간 3스레드: T1 캡처 / T2 추론+판정 / T3 하트비트(200ms) |
| **도구** | `scenario_check.py` | 시나리오 expect 자동 검증 + 안전 불변식 검사 |
| | `tools/camcheck.py` | 카메라 밝기·노이즈·fps 측정, 녹화 |
| | `tools/mcucheck.py` | ESP32 펌웨어 자동 점검 7항목 |
| | `tools/bench.py` | 검출기 추론 속도 측정 (모델·스레드별) |

### 1.3 안전 설계 원칙 (코드에 반영됨)
- **비대칭 판단**: 사람이 1프레임만 보여도 즉시 N. Y는 3초 + 10프레임 연속으로 비어 있어야 함
- **기본값은 N**: 시작, 고장, 종료, 하트비트 끊김 → 전부 N
- **오래된 정보 거부**: 0.5초 넘은 프레임, 순서가 뒤바뀐 프레임은 FAULT
- **카메라를 의심**: 화면이 정지·암전되면 모델이 "아무도 없음"이라 해도 믿지 않음
- **스레드가 죽으면** 전체 종료 + 마지막 송신은 항상 N

### 1.4 검증
- 단위·통합 테스트 98개 통과 (프로토콜·MockMCU·시리얼 출력 12개 포함) (`.venv\Scripts\python -m pytest -q`)
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
.venv\Scripts\python -m psafe.tools.mcucheck --port COM5               # ESP32 점검
.venv\Scripts\python -m psafe --config config/serial_dummy.yaml        # 가상 판단 → ESP32
```
모델 파일(`models/yolo11n_320.onnx`, `_640.onnx`)은 git에 안 올라감 → DESIGN.md §14.1 Colab 코드로 생성.

---

## 2. 실험 결과

실험 기록과 엣지 케이스는 [`docs/EXPERIMENTS.md`](EXPERIMENTS.md)에서 관리한다.

- **EXP-001 (2026-11-14, PC 웹캠):** 캡처·모델 30 fps, 추론 평균 8.1~8.8 ms. 실시간 동작 확인
- **E-01 (미조치, 1순위):** 렌즈를 손으로 가렸는데 Y → EXP-002
- **E-02 (조치함, 재확인 필요):** 정지 장면 `frozen_frame` 오탐 → EXP-003

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

### 4.1 바로 다음
1. ~~EXP-006: ESP32 실기 검증~~ → 완료 (mcucheck 7/7, 파이프라인 연결). 남은 육안 확인 3건은 EXPERIMENTS EXP-006
2. ~~RPi 기본 세팅~~ → 완료 (EXP-007): Python 3.13 venv, 테스트 100개·실시간 9개·시나리오 9개 통과
3. ~~RPi 추론 속도~~ → 완료 (EXP-005): 320·3스레드·15 fps로 결정, 640은 현재 설정으로 사용 불가
4. RPi ↔ ESP32 실기 연결은 생략, Mock(가상 시리얼 + 가짜 ESP32)으로 검증 완료 (EXP-007)
5. RPi 방열판·팬 준비 (E-09), RPi용 설정 파일(`config/rpi_*.yaml`)은 카메라 확정 후 작성

### 4.2 그 다음 (카메라 확정 후, 수업 외 시간)
4. 카메라 Source (기종 미정: CSI / USB / 열화상 후보. 열화상이면 `FramePacket.image` 계약과 Detector 재검토)
5. EXP-002: 렌즈 가림 측정 → `low_contrast` 검사 추가 + `camera_covered` 시나리오 (E-01)
6. EXP-003: 화면 정지 임계값 재확인 (E-02)
7. EXP-004: 새 카메라 기준 health 임계값, E-08 재확인
8. 640 모델 비교, 녹화 클립 + expect 자동 검증

### 4.3 미결 사항 (DESIGN §15)
- 카메라 기종 / 위치 / 조명
- 레고 미니피규어 감지 여부 (안 되면 파인튜닝 또는 사람 손으로 시연)
- 차량 내부 잔류자 포함 여부, 입출차 구분, ESP32 출력 장치, 보조 센서

### 4.4 커밋 안 된 변경
2026-11-16 작업분: 주석 한글화, 파일·패키지 이름 변경(`*_api.py`, `detectors/`, `safety/`), README·CODE_GUIDE·EXPERIMENTS 문서, RESEARCH 출처 정리, 시리얼 출력·프로토콜 v1·MockMCU·ESP32 펌웨어·mcucheck.

---

## 팀 공유 원칙
코드는 그대로 넘기지 않는다. 공유: 인터페이스(`core/packets.py`, 각 `*_api.py`) + `docs/DESIGN.md` + `docs/EXPERIMENTS.md` + 이 파일의 §3 (교훈).
