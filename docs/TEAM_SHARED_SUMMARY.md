# 조원 공유 자료 정리 (`Downloads\조원공유`)

> 정리일: 2026-11-25  
> 원본 위치: `C:\Users\shin\Downloads\조원공유` (총 7개 파일, 3개 주제)  
> 목적: 조원이 공유한 라즈베리파이 환경 설정·통신 벤치마크·카메라 전송/융합 프로토타입의 내용을 요약하고, 현재 `psafe` 설계([`DESIGN.md`](DESIGN.md), [`CORAL_DESIGN.md`](CORAL_DESIGN.md), [`edge_tpu/DISCUSSION.md`](edge_tpu/DISCUSSION.md))와의 연결점 및 팀 확인 사항을 정리한다.

---

## 0. 파일 구성 한눈에 보기

| 경로 | 크기 | 역할 |
|---|---:|---|
| `setup_rdp.sh` | 40.5 KB | RPi 4B (Debian 13 trixie) 원격 데스크톱(xrdp) + 카메라/개발환경 자동 설치 스크립트 |
| `handoff_prompt.md` | 2.9 KB | 다른 조원 노트북(Claude)에 전달해 여러 대의 Pi에 `setup_rdp.sh`를 적용하는 실행 가이드 |
| `comm_bench/README.md` | 6.8 KB | 22번 ↔ 49번 Pi 간 6종 텍스트 통신 벤치마크 사용법 및 해석 기준 |
| `comm_bench/bench_server.py` | 6.2 KB | TCP(5001) / UDP(5002) / HTTP(5003) / MQTT(1883) 에코 서버 (49번 Pi 실행) |
| `comm_bench/bench_client.py` | 14.1 KB | 동기 왕복 RTT·손실률·처리량 측정 및 CSV 저장 클라이언트 (22번 Pi 실행) |
| `mqtt_cam/ir_sender.py` | 4.1 KB | 22번 Pi(NoIR) → MQTT(`cam/ir`)로 타임스탬프+흑백 JPEG 프레임 송신 |
| `mqtt_cam/fusion_receiver.py` | 11.2 KB | 49번 Pi(RGB)에서 IR 수신 → 시각 매칭·오버레이 합성 → 파일 저장 + HTTP MJPEG 스트리밍 |

### 공유 자료에서 확인된 팀 실제 HW/네트워크 환경
- **보드 & OS:** Raspberry Pi 4B, **Raspberry Pi OS (Debian 13 trixie)**, 계정 `linux`
- **확인된 IP & 역할 (프로토타입 기준):**
  - `192.168.20.22` (22번 Pi): NoIR 카메라 송신 / 벤치마크 클라이언트
  - `192.168.20.49` (49번 Pi): RGB 카메라 + 통합 수신 / Mosquitto 브로커(1883) / 벤치마크 서버
- **카메라 스택:** `rpicam-apps`, **`python3-picamera2`** (CSI 카메라 전제)
- **설치된 통신 패키지:** Mosquitto(49번 Pi, 1883 익명 허용), `paho-mqtt`

---

## 1. 원격 데스크톱 자동 세팅 (`setup_rdp.sh`, `handoff_prompt.md`)

### 핵심 설계
- **SD 카드 재설치 없이** 기존 Debian 13(trixie) 위에 Windows `mstsc`(3389 포트) 접속용 `xrdp` + Raspberry Pi 공식 X11 데스크톱(`rpd-x-core`)을 구성한다.
- 평소에는 콘솔(`multi-user.target`, 자동 로그인 끔)로 부팅하고, RDP 접속 시에만 사용자 전용 `~/startwm.sh`로 X11 GUI를 띄워 로컬 모니터 설정을 건드리지 않는다.
- **안전·운영 장치:**
  - `apt-get -s` 시뮬레이션으로 비-라이브러리 패키지가 함께 삭제되는 설치를 자동 차단 (`apt_install_safe`).
  - 반복 실행 가능(idempotent), 상태 기록(`/var/lib/setup_rdp`) 기반 `--rollback` 지원.
  - VNC(`wayvnc`, `realvnc`)는 기본 설치에서 건드리지 않고, RDP 확인 후 `--purge-vnc`로 별도 정리.
- **RDP 검은 화면·패널 버그 대응 내장:**
  - `LIBGL_ALWAYS_SOFTWARE=1`, RDP 세션 한정 `xcompmgr` 비활성화, Chromium(`/etc/chromium.d/99-rdp-disable-gpu`) 및 VS Code(`~/.vscode/argv.json`) GPU 가속 끔, `rdp-panel-fix`로 작업표시줄 재시작.
- **함께 설치되는 카메라/개발 패키지 (롤백 시에도 유지):**
  - `rpicam-apps`, `python3-picamera2`, `python3-pyqt5`, `python3-opencv`, `python3-numpy`, `fonts-nanum`, `chromium`, `firefox`, `code`, `lxterminal` 등.
  - `linux` 계정을 `video`, `render`, `audio`, `input` 그룹에 추가.

### 주요 실행 모드
```bash
bash ~/setup_rdp.sh              # 설치 및 구성 (일반 사용자로 실행, 내부에서 sudo 호출)
bash ~/setup_rdp.sh --check      # xrdp, 3389 포트, 이미지 로더, 카메라(rpicam-hello), picamera2/cv2 점검
bash ~/setup_rdp.sh --screenshot # mstsc 접속 상태에서 Pi X11 화면 캡처(~/rdp_screen.png) 및 창/모니터 진단
bash ~/setup_rdp.sh --rollback   # 스크립트가 설치·수정한 RDP 설정 되돌리기
bash ~/setup_rdp.sh --purge-vnc  # mstsc 확인 후 VNC 제거
```

### `handoff_prompt.md` 요약
- 다른 조원 노트북의 Claude 채팅에 붙여 넣어, 각자 맡은 Pi에 `scp → ssh 실행 → 재부팅 → mstsc(Xorg) 로그인`을 일관되게 수행하기 위한 운영 프롬프트.
- Windows 줄바꿈(`$'\r'`), 빈 흰 띠 패널, 작업표시줄 누락, 검은 화면 등 이미 겪은 트러블슈팅 절차가 정리되어 있다.

---

## 2. 통신 프로토콜 벤치마크 (`comm_bench/`)

### 목적 및 측정 구조
- 22번 Pi(`bench_client.py`) → 49번 Pi(`bench_server.py`) 간 UTF-8 텍스트 에코의 **왕복 시간(RTT), 손실률, 동기 처리량(Msg/s)**을 6가지 방식으로 비교한다.
- 22번의 단일 `time.perf_counter()`로 왕복을 측정하므로 **두 Pi의 시계를 맞출 필요가 없다.**

### 비교 대상 (6가지 방식 × 4가지 크기: `16B, 256B, 1KB, 8KB`)
| 방식 | 포트/경로 | 특징 |
|---|---|---|
| `tcp` | 5001 (연결 재사용, 4B 길이 헤더, `TCP_NODELAY`) | 저오버헤드, 스트림 재전송 |
| `udp` | 5002 (메시지당 1데이터그램) | 최소 오버헤드, 유실은 타임아웃으로 집계 (8KB는 IP 단편화 영향 가능) |
| `http-keepalive` | 5003 (`POST /echo`, HTTP/1.1 연결 재사용) | HTTP 헤더/파싱 비용 포함 |
| `http-new` | 5003 (`Connection: close`, 요청마다 새 TCP) | TCP 핸드셰이크 비용 포함 |
| `mqtt-qos0` | 1883 Mosquitto (`bench/ping` → `bench/pong`) | 브로커 홉 추가, 발행 확인 없음 |
| `mqtt-qos1` | 1883 Mosquitto (`bench/ping` → `bench/pong`) | 브로커 홉 + PUBACK/재전송 비용 |

### 벤치마크 구현상 눈여겨볼 점
- **공정성 조치:** `paho-mqtt` 기본값과 Mosquitto 기본값이 Nagle 알고리즘을 끄지 않아 MQTT만 수십~100 ms 불리해지는 문제를 막기 위해, 스크립트 소켓(`_set_nodelay`)과 브로커(`/etc/mosquitto/conf.d/lan.conf`에 `set_tcp_nodelay true`) 양쪽에 **`TCP_NODELAY`를 켠다.**
- **응답 검증:** 페이로드 앞 16바이트(`run_id` 8자 + `seq` 8자)로 UDP/MQTT의 지연·중복 응답을 걸러내고, 전체 바이트가 요청과 일치해야 성공으로 센다.
- **출력 지표:** `Avg / Med / p95 / p99 / Max ms`, `RTT loss%`, `Msg/s` (동기 1-in-flight 기준), `Rate loss%` → `~/results/bench_<timestamp>.csv` 저장.

---

## 3. MQTT 카메라 전송 & RGB-IR 융합 프로토타입 (`mqtt_cam/`)

### 데이터 흐름
```text
[22번 Pi: ir_sender.py]                           [49번 Pi: fusion_receiver.py]
 NoIR (fake / Picamera2)                           RGB (fake / Picamera2)
   → Grayscale → JPEG(q=70)                          │
   → 12B 헤더(!dI: time.time, seq) + JPEG            ▼
   ── MQTT QoS 0 (cam/ir, 1883) ──▶ IrBuffer(최근 30장) ──▶ 시각 최근접 매칭(|dt| <= 0.15s)
                                                               │
                                                               ▼
                                                    warpPerspective(선택 H)
                                                    + COLORMAP_INFERNO + addWeighted(0.4)
                                                               │
                                      ┌────────────────────────┼────────────────────────┐
                                      ▼                        ▼                        ▼
                            ~/fusion_out/latest.jpg    HTTP :8080 /stream        --show (cv2.imshow)
                            (1초마다 원자적 덮어쓰기)   (MJPEG 브라우저 실시간)    (RDP 터미널용)
```

### 주요 세부사항
- **카메라 추상화:** `--source fake`(동기화된 궤적의 합성 영상)와 `--source pi`(`Picamera2`)를 지원해 **카메라 도착 전에도 테스트 가능**.
- **프레임 매칭:** `IrBuffer`(deque 30장)에서 현재 RGB 촬영 시각 `t`와 `abs(ir_t - t)`가 가장 작고 `--tolerance`(기본 0.15초) 이하인 IR 프레임을 짝짓는다. 매칭 실패 시 `NO IR MATCH` 빨간 라벨 표시.
- **시각화 편의:** RDP 없이도 노트북 브라우저에서 `http://192.168.20.49:8080`으로 3분할 화면(`RGB | IR | FUSED`)을 바로 볼 수 있는 내장 MJPEG HTTP 서버 포함.

---

## 4. 우리 `psafe` 설계와의 연결점 및 팀 확인 사항

조원 공유 자료와 현재 `psafe` 설계([`DESIGN.md`](DESIGN.md), [`edge_tpu/DISCUSSION.md`](edge_tpu/DISCUSSION.md))를 대조했을 때 아래 4가지가 중요하다.

### ① OS·카메라 제약의 현실화 (Coral 설계와 직결)
- `setup_rdp.sh`와 `handoff_prompt.md`를 보면 팀은 이미 **Debian 13 (trixie)을 유지한 채** `python3-picamera2`(시스템 Python 3.13)로 여러 대의 Pi를 세팅 중이다.
- 즉 [`edge_tpu/DISCUSSION.md`](edge_tpu/DISCUSSION.md) R4/R6에서 논의한 **"Bookworm(Python 3.11)으로 SD카드 새로 굽기"**는 기존 RDP·Pi 세팅과 충돌할 수 있다.
- **확인 필요:** Coral을 붙일 노드 1대만 Bookworm(3.11)으로 갈지, 아니면 Debian 13을 유지하고 **B안(`ai-edge-litert` on 3.13) → 실패 시 `rpicam-vid` 파이프 또는 추론 워커 IPC**로 갈지 팀과 조율해야 한다.

### ② 아키텍처 방향 차이: "영상 전송(중앙 융합)" vs "온디바이스 추론(결과 전송)"
| 항목 | 조원 `mqtt_cam` 프로토타입 | 현재 `psafe` 설계 & `image.png` |
|---|---|---|
| 전송 데이터 | **원본/JPEG 영상** (22번 → 49번) | **추론 결과(인원/판정 JSON)** (`DESIGN.md` §7.4) |
| 통신 프로토콜 | **MQTT (Mosquitto 브로커, TCP 1883)** | **UDP + JSON v2 (브로커 없음, CRC-32 + RTT)** |
| 시각 처리 | `time.time()` 직접 비교 (`|dt| ≤ 0.15s`) | 보드 간 절대 시각 비교 금지 (`evidence_age_ms` + RTT) |

- **주의 (시계 어긋남):** `mqtt_cam`의 `fusion_receiver.py`는 두 Pi의 `time.time()`을 직접 빼서 `0.15s` 이내인지 비교한다. NTP 동기화가 150 ms 이상 어긋나면 `NO IR MATCH`가 지속되므로, 영상 매칭을 쓸 경우 두 Pi 간 시계 동기화(`chrony`/NTP) 확인이 필수다.
- **통신 스택 결정:** `comm_bench`가 이미 준비되어 있으므로, 유선 LAN에서 **UDP vs TCP vs MQTT** RTT·손실률을 실측한 뒤 팀 최종 통신 방식을 하나로 통일하면 좋다.

### ③ `psafe`에 바로 참고·재사용할 수 있는 요소
1. **`Picamera2` 캡처 패턴 (`ir_sender.py:51-66`, `fusion_receiver.py:57-70`):** `create_video_configuration(main={"size": (w, h), "format": "RGB888"}, controls={"FrameRate": fps})` + `RGB888`이 OpenCV BGR 순서와 같다는 점 → 추후 `src/psafe/sources/picamera.py` 작성 시 그대로 참고.
2. **내장 MJPEG HTTP 뷰어 (`fusion_receiver.py:108-167`):** headless RPi(`opencv-python-headless`)에서 `cv2.imshow` 대신 브라우저로 실시간 상태를 보는 데 매우 유용 → `psafe` 디버그 `Output`이나 `VerdictOutput` 후보로 활용 가능.
3. **`setup_rdp.sh --check`의 카메라 점검 패턴:** `rpicam-hello --list-cameras` + `picamera2`/`cv2` import 확인.

### ④ 팀과 맞출 안건 요약
- [ ] Coral 장착 Pi의 OS 정책: Debian 13 유지 vs 해당 보드만 Bookworm(3.11)
- [ ] 최종 데이터 경로: 각 Pi 온디바이스 YOLO 후 결과만 전송(`image.png`·`psafe`) vs 영상 전송 후 49번에서 중앙 융합(`mqtt_cam`)
- [ ] 최종 통신 프로토콜: `comm_bench` 실측 기반 UDP(브로커 없음) vs MQTT(Mosquitto) 확정
