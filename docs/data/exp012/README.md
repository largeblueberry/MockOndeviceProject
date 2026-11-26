# EXP-012 원본 데이터 (통신 방식 비교)

결과 정리와 판정은 `docs/EXPERIMENTS.md` EXP-012. 이 폴더는 측정 원본이다.
다시 분석: `python -m psafe.tools.commbench analyze <cli_*.csv> <srv_*.csv> [--fault-s 5]`

| 폴더 | 구간 | 조건 |
|---|---|---|
| `laptop_localhost/` | 노트북 안 (Windows 11) | 20 Hz × 10초, udp / http / http-new. 도구 확인용, 판정에 안 씀 |
| `pi_localhost/` | `.22` rpi-syw3480 안 | 20 Hz × 30초, 5가지 방식. 브로커 기본 설정 (TCP_NODELAY 꺼짐) |
| `pi_localhost_nodelay/` | `.22` 안 | 20 Hz × 30초, mqtt0 / mqtt1. 브로커 `set_tcp_nodelay true` |
| `wifi_normal/` | `.10` rpi-lyb3550 → `.22`, Wi-Fi 5 GHz (두 Pi 모두 절전 모드 켜짐) | 5 Hz × 300초, 5가지 방식, 평상시 |
| `fault/` | `.10` → `.22`, Wi-Fi | 5 Hz × 60초, 20초 시점에 5초 고장. `F2_*` 받는 쪽 강제 종료(SIGKILL) 후 재시작, `F3_*` 브로커 강제 종료 후 재시작 |

파일 형식 (파일 이름의 `cli_` = 보내는 쪽, `srv_` = 받는 쪽):

- `cli_*.csv`: `run, proto, seq, t_send_ns, t_echo_ns, status`
  - 시각은 보내는 쪽의 `perf_counter_ns`
  - `status`는 `ok` / `lost` / `error:<예외>` / `skipped`
- `srv_*.csv`: `run, proto, seq, t_send_ns, t_recv_ns, server_boot, bytes`
  - `t_recv_ns`는 받는 쪽 시계라서 `t_send_ns`와 직접 비교하지 않는다. 초과 지연만 쓴다 (EXP-012 측정 방법)
  - `server_boot`가 바뀐 곳이 재시작 지점이다

브로커: mosquitto 2.0.21, `.22`에서 일반 사용자 권한으로 띄운 별도 인스턴스 (포트 1884, `persistence false`).

각 폴더의 `analysis.txt`는 측정 직후의 `analyze` 출력이다. `scripts/`에는 노트북에서 두 Pi를 ssh로 움직인 실행 스크립트(`wifi_normal.sh`, `fault_f23.sh`)가 있다.
