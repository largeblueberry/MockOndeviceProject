# 진행 상황 · 결과 정리

> 마지막 저장: 2026-11-19 (목). 다시 시작할 때 이 파일부터 읽기.
> 설계는 `docs/DESIGN.md`, 이 파일은 "무엇을 했고, 무엇을 알게 됐고, 다음에 뭘 하나"를 기록한다.

---

## 1. 지금까지 만든 것

### 1.1 한 줄 요약
카메라 영상으로 사람을 감지해 **"구역이 비었다(Y) / 아니다(N)"** 를 판정하는 파이프라인.
PC 웹캠과 실제 YOLO 모델로 실시간 동작까지 확인했다. ESP32로 Y/N을 보내는 시리얼 출력과 펌웨어를 작성하고 실제 ESP32에서 검증했다 (EXP-006).
2026-11-19: 파이 3대 구성(카메라 노드 A·B → 허브 C)의 **보드 간 통신**을 만들고, PC 한 대에서 프로세스 3개를 실제 UDP로 연결해 검증했다 (EXP-009).

```
카메라 노드 (A, B 각각)
Source ──▶ HealthMonitor ──▶ Detector ──▶ SafetyFSM ──▶ Outputs
(카메라)    (카메라가 정상인가)  (사람 찾기)    (Y/N 판정)     (콘솔·화면·ESP32)
                                               │
                                               └─ Report (UDP) ──▶ 허브 C ──▶ 디스플레이 + LED(ESP32)
                                                  ◀── Control ──  (최종 판정 4가지)
```

### 1.2 구성 요소

| 영역 | 구현 | 역할 |
|---|---|---|
| **core** | `packets.py` | 모듈 사이에 주고받는 데이터 형식(FramePacket, FrameDetections, Decision(+사람 수 `count`), State) — **계약** |
| | `verdict.py` | C의 최종 판정 형식(Verdict: 비었음 확인 / 사람 있음 / 확인 중 / 판단 불가), ESP32 매핑 |
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
| | confirm.py | A·B: C가 확인을 요청한 **뒤에** 찍은 프레임만으로 비었음 재확인 |
| | aggregator.py | C: A·B 보고 → 최종 판정. 사람 > 판단 불가 > 확인 중 > 비었음 순 |
| **link** | messages | 보드 간 메시지 Report·Control, 프로토콜 v2 (UDP 데이터그램 1개 = JSON 1개, CRC-32, 왕복 시간 echo, 엄격한 검증) |
| | transport | UDP 전송 + 가짜 네트워크(LoopbackNetwork: 유실·지연·중복·깨짐·단절 주입) |
| | peer | 노드별 중복·지연 도착·재시작·무응답(500 ms) 판정 |
| | node | A·B 쪽: C의 확인 요청 수신, 200 ms마다 Report 송신 |
| **hub** | `hub.py` | C 실행: 수신(+왕복 시간) → 판정 → 디스플레이·LED 출력 → Control 송신 |
| **sim** | `sim.py` | A·B·C 통합 시뮬레이션: 노드마다 다른 시계, 네트워크 고장 주입, 안전 불변식 검사 |
| **outputs** | console, viewer | 상태 변화 로그, 박스·ROI·Y/N 배너 표시, 녹화 |
| | protocol | RPi → ESP32 패킷 v1 (인코딩, CRC-8, 수신 로직) |
| | serial_out | USB 시리얼로 ESP32에 200ms마다 송신, 끊기면 1초마다 재연결 |
| | mock_mcu | ESP32 없이 같은 수신 로직으로 관문 상태 확인 |
| | verdict_api | C의 출력 인터페이스 `VerdictOutput` (디스플레이 기종 미정 → 추상화). console, 기존 ESP32 출력을 감싸는 `decision` 브리지 |
| **firmware** | `esp32/psafe_gate` | 패킷 수신, 500ms 워치독, LED/릴레이 출력, 상태 보고 |
| **runners** | SyncRunner | 가짜 시계로 즉시 실행(테스트용) |
| | ThreadedRunner | 실시간 3스레드: T1 캡처 / T2 추론+판정 / T3 하트비트(200ms). 다중 보드면 T4 link-rx 추가 |
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
- **보드 간에는 시각을 비교하지 않음**: 근거 나이는 보내는 쪽이 자기 시계로 재서 싣고, 받는 쪽은 받은 시각을 자기 시계로 잰다
- **명령이 아니라 상태를 주기적으로**: C ↔ A·B 모두 200 ms마다 상태를 보냄 → ACK·재전송 없이 유실·중복을 견딤
- **C가 죽어도 초록이 남지 않음**: LED는 C의 GPIO가 아니라 ESP32(워치독)를 거친다

### 1.4 검증
- 단위·통합 테스트 222개 통과 (2026-11-19 기준. 보드 간 통신 포함: `test_link`, `test_confirm`, `test_aggregator`, `test_hub`, `test_multisim` 38) (`.venv\Scripts\python -m pytest -q`)
- 실시간 스레드 테스트 9개 (`pytest -m slow`)
- PC 한 대 A·B·C 프로세스 3개 실제 UDP 실험 (EXP-009): 예상 9개 모두 일치. 프로토콜 v2로 재실행해도 같은 결과
- 다중 보드 통합 시뮬레이션 (EXP-010): 시계 어긋남·네트워크 고장·재시작·무작위 30종에서 "사람 있는데 EMPTY" 0회
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
# 다중 보드 (터미널 3개)
.venv\Scripts\python -m psafe --config config/hub_c.yaml               # C
.venv\Scripts\python -m psafe --config config/node_a.yaml              # A
.venv\Scripts\python -m psafe --config config/node_b.yaml              # B
```
모델 파일(`models/yolo11n_320.onnx`, `_640.onnx`)은 git에 안 올라감 → DESIGN.md §14.1 Colab 코드로 생성.

---

## 2. 실험 결과

실험 기록과 엣지 케이스는 [`docs/EXPERIMENTS.md`](EXPERIMENTS.md)에서 관리한다.

- **EXP-001 (2026-11-14, PC 웹캠):** 캡처·모델 30 fps, 추론 평균 8.1~8.8 ms. 실시간 동작 확인
- **E-01 (미조치, 1순위):** 렌즈를 손으로 가렸는데 Y → EXP-002
- **E-02 (조치함, 재확인 필요):** 정지 장면 `frozen_frame` 오탐 → EXP-003
- **EXP-010 (2026-11-19, 통합 시뮬레이션):** 안전 구멍 1건(E-21: 네트워크 지연이 근거 나이에 안 잡혀 지연 2.5초에서 사람이 있는데 EMPTY), 가용성 버그 1건(E-20: 깨진 메시지 하나로 노드 영구 무시) 발견 → 프로토콜 v2로 수정. 시계 오프셋을 바꿔도 결과 동일 (E-17 해결)
- **EXP-009 (2026-11-19, PC 한 대 A·B·C):** 첫 "비었음 확인" 4.53초, 사람 등장 → C "사람 있음" 0.25초, B 강제 종료 → C "판단 불가" 0.52초, B 재시작 → 재확인 후 4.52초. 발견한 문제 3건(집계 버그, 사유 표시 누락, 테스트 시간표 경쟁) 조치

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
6. **(2026-11-19) 통신의 어려움은 형식이 아니라 시간이다.** JSON 형식은 금방 정했다. 실제로 막아야 했던 것은 전부 시간 문제였다: 파이끼리 시계가 다름, 재시작 전 메시지가 늦게 도착함, 같은 확인 요청이 반복될 때 기준 시각을 갱신하면 확인이 영원히 안 끝남, 멈춘 worker가 옛 '비었음'을 계속 보냄. 모두 테스트로 고정했다.
7. **PC 한 대 실험은 시계 문제를 숨긴다.** 세 프로세스가 같은 시계를 쓰므로 시각을 잘못 비교하는 코드가 있어도 드러나지 않는다. 노드마다 시계를 일부러 어긋나게 한 시뮬레이션이 필요하다 (M6.5).
8. **"무시해도 된다"는 가정이 안전 구멍이었다.** "LAN 지연은 수 ms"라는 가정 때문에 근거 나이에서 전달 시간이 빠졌다. 실제 실험(localhost)에서는 지연이 0이라 드러나지 않았고, 시뮬레이션에 지연을 넣고 안전 불변식으로 검사하자 바로 위반이 나왔다. 가정은 검사로 바꿔야 한다 (지금은 왕복 시간으로 잰다).
9. **시뮬레이터는 불변식 검사기가 있어야 쓸모가 있다.** 판정 로그를 사람이 보는 것만으로는 2.5초 동안의 잘못된 EMPTY를 놓치기 쉽다. 정답(world)과 판정을 자동으로 대조하고, 검사기 자체도 가짜 위반으로 시험했다.

---

## 4. 다음 할 일

### 4.1 바로 다음
1. ~~EXP-006: ESP32 실기 검증~~ → 완료 (mcucheck 7/7, 파이프라인 연결). 남은 육안 확인 3건은 EXPERIMENTS EXP-006
2. ~~RPi 기본 세팅~~ → 완료 (EXP-007): Python 3.13 venv, 테스트 100개·실시간 9개·시나리오 9개 통과
3. ~~RPi 추론 속도~~ → 완료 (EXP-005): 320·3스레드·15 fps로 결정, 640은 현재 설정으로 사용 불가
4. RPi ↔ ESP32 실기 연결은 생략, Mock(가상 시리얼 + 가짜 ESP32)으로 검증 완료 (EXP-007)
5. RPi 방열판·팬 준비 (E-09), RPi용 설정 파일(`config/rpi_*.yaml`)은 카메라 확정 후 작성
6. ~~다중 보드 통신 (M6)~~ → 완료 (2026-11-19, EXP-009): 메시지·전송·수신 추적, A·B 확인 처리, C 판정·출력 인터페이스, C 실행
7. ~~M6.5: A·B·C 통합 시뮬레이션~~ → 완료 (EXP-010). E-20·E-21 발견 → 프로토콜 v2
8. 임계값 확정: 확인 프레임 수 `K`(`confirm_frames`, 임시 10), `confirm_timeout_s`(임시 3.0), `max_evidence_age_ms`(임시 1000) — 실기 측정 후

### 4.2 그 다음 (카메라 확정 후, 수업 외 시간)
4. 카메라 Source (기종 미정: CSI / USB / 열화상 후보. 열화상이면 `FramePacket.image` 계약과 Detector 재검토)
5. EXP-002: 렌즈 가림 측정 → `low_contrast` 검사 추가 + `camera_covered` 시나리오 (E-01)
6. EXP-003: 화면 정지 임계값 재확인 (E-02)
7. EXP-004: 새 카메라 기준 health 임계값, E-08 재확인
8. 640 모델 비교, 녹화 클립 + expect 자동 검증
9. M7: 파이 3대 + 스위치로 다중 보드 이식. 설정의 `127.0.0.1`을 각 파이 IP로 바꾸고, 실제 네트워크 손실·지연 측정 (Wi-Fi면 `node_timeout_ms` 재결정). 확인 항목은 EXPERIMENTS **EXP-011**
10. 다중 보드 미해결 엣지 케이스 (EXPERIMENTS §1.1): **E-22 공통 사각지대(안전)**, **E-26 메시지 인증 없음(안전·보안)**, E-23 확인 반복, E-24 짧은 끊김에도 재확인, E-25 왕복 시간 과대 추정, E-27 LED 색 구분, E-28 시뮬레이션이 다루지 않는 조건

### 4.3 미결 사항 (DESIGN §15)
- 카메라 기종 / 위치 / 조명
- 레고 미니피규어 감지 여부 (안 되면 파인튜닝 또는 사람 손으로 시연)
- 차량 내부 잔류자 포함 여부, 입출차 구분, 보조 센서
- 디스플레이 기종 (인터페이스만 고정), LED 색·개수 (STATE별 색은 펌웨어 수정 필요)
- 네트워크: 유선 스위치 vs Wi-Fi
- Report에 카메라 상태 수치를 넣을지 → 보류 (HW에서 받을 수 있는지 모름)

### 4.4 커밋 안 된 변경
없음 (2026-11-19 작업분은 브랜치 `feat/multiboard-sim-v2`에 커밋. main에 합치려면 `git checkout main && git merge --ff-only feat/multiboard-sim-v2`).

---

## 작업 원칙
이 저장소는 방향성 확인용 mock 프로젝트다. SW 설계(인터페이스·프로토콜)는 직접 정하고, 결정과 근거는 `docs/DESIGN.md`, 실측은 `docs/EXPERIMENTS.md`에 남긴다.
