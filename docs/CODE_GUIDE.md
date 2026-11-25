# 코드 구조 안내 (CODE_GUIDE)

> 관련 문서: `docs/DESIGN.md` (설계 배경과 원칙)
> 본 문서는 파일 간 연결 관계와 실행 흐름을 설명한다.
> 다이어그램은 Mermaid 형식이다. GitHub에서는 그대로 렌더링되며, VS Code에서는 "Markdown Preview Mermaid Support" 확장이 필요하다.

---

## 0. 범례

| 표시 | 의미 | 대상 파일 |
|---|---|---|
| 🔓 **계약** | 모듈 간 **데이터 형식**과 **인터페이스**를 정의한다. 바꾸면 연결된 부품을 모두 다시 확인해야 한다 | `core/packets.py`, `core/verdict.py`, `sources/source_api.py`, `detectors/detector_api.py`, `outputs/output_api.py`, `outputs/verdict_api.py`, `link/messages.py`, `link/transport.py` |
| 🔨 **구현** | 계약을 따르는 구현 파일. 본 문서는 입력과 출력만 기술한다 | 그 외 전부 |

모든 부품은 계약(🔓)을 통해서만 상호작용한다. 따라서 개별 구현 내용을 몰라도 전체 흐름을 파악할 수 있다.

비슷한 이름과 용어의 구분은 §9 용어 정리를 참고한다.

---

## 1. 전체 구조

### 1.1 런타임 데이터 흐름

프레임 한 장이 입력되어 Y/N 신호로 출력되기까지의 경로다. 화살표에 표기된 타입은 모두 `core/packets.py`에 정의되어 있다.

```mermaid
flowchart LR
    SRC["📷 Source<br/>sources/*"]
    HM["🩺 HealthMonitor<br/>safety/health.py"]
    DET["🧠 Detector<br/>detectors/*"]
    FSM["⚖️ SafetyFSM<br/>safety/fsm.py<br/>(+ safety/roi.py)"]
    OUT["📤 Output<br/>outputs/*"]

    SRC -- "FramePacket" --> HM
    SRC -- "FramePacket" --> DET
    HM -- "fault 사유 (str 또는 None)" --> FSM
    DET -- "FrameDetections" --> FSM
    FSM -- "Decision (Y/N)" --> OUT
```

- **HealthMonitor**와 **Detector**는 같은 프레임을 독립적으로 처리한다. HealthMonitor는 카메라 상태를, Detector는 사람의 존재 여부를 판단한다.
- **SafetyFSM**은 두 결과를 종합하여 최종 판단을 내린다. 카메라 이상이 감지되면 검출 결과와 관계없이 Y를 출력하지 않는다.
- **Output**은 `Decision`만 입력받으며, 사용된 모델이나 카메라 종류에 의존하지 않는다.

### 1.1.1 보드 3대의 데이터 흐름 (다중 보드)

위 흐름이 카메라 노드(A·B) 하나다. 설정에 `link:`가 있으면 노드는 판단을 C에 보고하고, C는 두 노드의 보고를 모아 최종 판정을 낸다. (DESIGN.md §7.4)

```mermaid
flowchart LR
    subgraph NODE["RPi A · B (각각)"]
        FSM2["⚖️ SafetyFSM"] -- "Decision" --> CT["✅ ConfirmTracker<br/>safety/confirm.py"]
        NL["🔗 NodeLink<br/>link/node.py"]
    end
    subgraph HUB["RPi C"]
        PT["PeerTracker<br/>link/peer.py"] --> AG["🧮 Aggregator<br/>safety/aggregator.py"]
        AG -- "Verdict" --> VO["📺 VerdictOutput<br/>outputs/verdict_api.py"]
    end
    FSM2 -- "Decision" --> NL
    CT -- "ConfirmStatus" --> NL
    NL -- "Report (UDP JSON)" --> PT
    AG -- "Control: confirm_id (UDP JSON)" --> NL
    NL -- "ConfirmRequest" --> CT
    VO --> DISP["디스플레이"]
    VO --> ESP["ESP32 → LED"]
```

- **노드(A·B):** FSM은 그대로다. `ConfirmTracker`가 FSM이 낸 `Decision`을 보고, C가 확인을 요청한 **뒤에** 찍은 프레임만으로 비었음을 다시 확인한다.
- **C:** `PeerTracker`가 노드별 최신 유효 보고를 유지하고(중복·지연·재시작·무응답 처리), `Aggregator`가 4가지 판정(`EMPTY` / `OCCUPIED` / `CHECKING` / `UNKNOWN`)을 낸다.
- **양방향 모두 200 ms마다 상태를 보낸다.** 명령과 ACK가 없으므로 메시지가 유실·중복돼도 다음 주기에 같은 상태가 다시 온다.

### 1.2 파일 의존 관계

화살표는 import 방향을 나타낸다. 하위 계층은 상위 계층을 참조하지 않으며, `core`는 다른 패키지를 import하지 않는다.

```mermaid
flowchart BT
    subgraph L0["① core — 공통 기반"]
        packets["🔓 packets.py<br/>데이터 형식"]
        verdict["🔓 verdict.py<br/>C 판정 형식"]
        registry["registry.py<br/>이름→클래스 매핑"]
        clock["clock.py<br/>시계"]
        config["config.py<br/>YAML→설정 객체"]
        slot["slot.py<br/>최신 항목 1개 보관"]
    end

    subgraph L0B["①' link 기반 — 보드 간 통신"]
        lmsg["🔓 link/messages.py<br/>Report · Control"]
        ltr["🔓 link/transport.py<br/>UDP · Loopback"]
        lpeer["link/peer.py<br/>PeerTracker"]
    end

    subgraph L1["② 부품 — 상호 독립"]
        sbase["🔓 sources/source_api.py"]
        mbase["🔓 detectors/detector_api.py"]
        obase["🔓 outputs/output_api.py"]
        vbase["🔓 outputs/verdict_api.py"]
        fsmpkg["safety/<br/>fsm · roi · health · confirm"]
        agg["safety/aggregator.py"]
        simpl["sources/<br/>dummy · webcam · video · scenario"]
        mimpl["detectors/<br/>dummy · yolo_onnx"]
        oimpl["outputs/<br/>console · viewer · serial_out · mock_mcu<br/>(+ protocol)"]
    end

    subgraph L2["③ 조립"]
        app["app.py<br/>build() → Components"]
        lnode["link/node.py<br/>NodeLink"]
    end

    subgraph L3["④ 실행"]
        runners["runners.py<br/>SyncRunner · ThreadedRunner"]
        hub["hub.py<br/>Hub · HubRunner"]
    end

    subgraph L4["⑤ 진입점"]
        main["__main__.py<br/>python -m psafe"]
        scheck["scenario_check.py<br/>--check"]
    end

    sbase --> packets & registry & clock
    mbase --> packets & registry
    obase --> packets & registry
    vbase --> verdict & obase
    fsmpkg --> packets & config
    agg --> verdict & lmsg & lpeer
    lpeer --> lmsg
    simpl --> sbase
    mimpl --> mbase
    oimpl --> obase

    lnode --> lmsg & lpeer & ltr & fsmpkg
    app --> config & clock & sbase & mbase & obase & fsmpkg & lnode & ltr
    runners --> app & slot & packets & fsmpkg
    hub --> config & lmsg & lpeer & ltr & agg & vbase
    main --> app & runners & scheck & hub
    scheck --> app & runners
```

구조상 특징:

1. `sources` · `detectors` · `outputs` · `safety`는 서로 import하지 않는다. 한 부품의 변경이 다른 부품에 영향을 주지 않는다. (예외: `outputs/viewer.py`가 ROI를 그리려고 `safety/roi.py`를 쓴다.)
2. `link/`의 기반 파일(`messages`, `transport`, `peer`)은 `core`만 참조한다. 부품(`safety/aggregator.py`)과 조립 파일(`link/node.py`, `hub.py`)이 이것을 쓴다. `link/node.py`는 `safety/confirm.py`의 타입을 쓰므로 조립 계층에 둔다.
3. 구체 클래스(웹캠, YOLO 등)를 직접 참조하는 코드는 없다. `app.py`·`hub.py`도 레지스트리(`SOURCES`, `DETECTORS`, `OUTPUTS`, `VERDICT_OUTPUTS`)만 참조하며, 구체 클래스는 YAML 설정의 이름으로만 지정된다. (§3.2)
4. `runners.py`는 `Components` 객체만 입력받으며, 부품의 구현 종류(가상/실제)와 무관하게 동작한다.

### 1.3 디렉터리 구성

```
src/psafe/
├── __main__.py          ⑤ 진입점. 인자 해석 → 조립 → 실행
├── scenario_check.py    ⑤ 시나리오 실행 및 expect 검증
├── app.py               ③ 설정으로부터 부품 생성, Components로 묶음
├── runners.py           ④ 부품 실행 루프 (Sync / Threaded) — 카메라 노드
├── hub.py               ④ C(허브) 실행: Hub(수신·왕복 시간·판정·Control 송신), HubRunner
├── sim.py               ④ 다중 보드 통합 시뮬레이션 (MultiSim, 안전 불변식 check_invariant)
│
├── core/                ① 공통 기반
│   ├── packets.py   🔓  FramePacket · Detection(s) · Decision(+count) · State
│   ├── verdict.py   🔓  Verdict · NodeView · VerdictKind (C 판정), to_protocol (→ ESP32)
│   ├── registry.py      이름 → 클래스 매핑
│   ├── config.py        YAML → AppConfig(+link) / HubAppConfig (정의되지 않은 키는 오류)
│   ├── clock.py         RealClock / FakeClock
│   └── slot.py          LatestSlot (스레드 간 최신 프레임 전달)
│
├── sources/             ② 프레임 생성          read() → FramePacket
│   ├── source_api.py 🔓 Source 인터페이스, SOURCES 레지스트리
│   ├── dummy.py         가상 장면 생성 (시나리오 재생)
│   ├── scenario.py      시나리오 YAML 파싱
│   ├── webcam.py        웹캠 입력
│   └── video.py         영상 파일 입력
│
├── detectors/           ② 객체 검출            predict(FramePacket) → FrameDetections
│   ├── detector_api.py 🔓 Detector 인터페이스, DETECTORS 레지스트리
│   ├── dummy.py         노이즈를 주입하는 가상 검출기
│   └── yolo_onnx.py     YOLO (ONNX)
│
├── safety/              ② 판단 (순수 로직, 입출력 없음)
│   ├── fsm.py           SafetyFSM: step(FrameDetections) → Decision
│   ├── roi.py           감시 구역 포함 판정
│   ├── health.py        카메라 상태 검사: check(FramePacket) → 사유 또는 None
│   ├── confirm.py       A·B: ConfirmTracker.step(요청, Decision) → ConfirmStatus
│   └── aggregator.py    C: Aggregator.step(노드별 최신 Report, now) → Verdict
│
├── outputs/             ② 결과 출력            send(Decision) / show(Verdict)
│   ├── output_api.py 🔓 Output 인터페이스, OUTPUTS 레지스트리 (노드)
│   ├── verdict_api.py 🔓 VerdictOutput 인터페이스, VERDICT_OUTPUTS (C: console, decision 브리지)
│   ├── console.py       로그 출력
│   ├── viewer.py        화면 표시 및 녹화
│   ├── protocol.py      RPi → ESP32 패킷 v1 (인코딩, CRC-8, 수신 로직)
│   ├── serial_out.py    USB 시리얼로 ESP32에 송신
│   └── mock_mcu.py      가짜 ESP32 (같은 수신 로직)
│
├── link/                ①'·③ 보드 간 통신 (UDP + JSON)
│   ├── messages.py  🔓  Report · Control, encode / decode (v2: CRC 꼬리, 왕복 시간 echo, 검증 규칙)
│   ├── transport.py 🔓  Transport 인터페이스, UdpTransport, LoopbackNetwork(고장 주입)
│   ├── peer.py          PeerTracker: 노드별 중복·지연·재시작·무응답 판정
│   └── node.py          NodeLink: A·B 쪽 (Control 수신, Report 송신)
│
└── tools/
    ├── camcheck.py      카메라 측정 도구 (파이프라인과 독립)
    ├── mcucheck.py      ESP32 펌웨어 점검 도구 (파이프라인과 독립)
    └── bench.py         검출기 추론 속도 측정

firmware/esp32/psafe_gate/psafe_gate.ino   ESP32 펌웨어 (Arduino)
```

---

## 2. 진입점과 실행 흐름

### 2.1 호출 순서

```mermaid
flowchart TD
    A["<code>python -m psafe --config X.yaml</code>"] --> B["__main__.py<br/>main()"]
    B --> C{"--scenario 지정 여부"}
    C -- "미지정" --> HQ{"X.yaml에<br/>hub: 절?"}
    HQ -- "있음 (C)" --> HB["hub.py<br/>build_hub() → HubRunner.run()"]
    HQ -- "없음 (노드)" --> D["core/config.py<br/>parse_config()<br/>→ AppConfig"]
    C -- "지정" --> S["scenario_check.py<br/>run_scenario()<br/>(시나리오를 설정에 병합)"]
    S --> D
    D --> E["app.py<br/>build(cfg)<br/>→ Components"]
    E --> F{"runner.mode"}
    F -- "sync" --> G["runners.py<br/>SyncRunner.run()"]
    F -- "threaded" --> H["runners.py<br/>ThreadedRunner.run()"]
    G --> I["process_frame()<br/>두 Runner 공통의<br/>프레임 처리 함수"]
    H --> I
    S -. "RunResult로<br/>expect 검증" .-> R["PASS / FAIL"]
```

실행 경로는 입력 종류(웹캠, 시나리오, 테스트)와 관계없이 **설정 로드 → 조립(`build`) → 실행(Runner)** 의 3단계로 동일하다. C(허브)도 같은 3단계이며, 조립은 `build_hub()`, 실행은 `HubRunner`다 (§4.4).

### 2.2 `Components`

`app.py`의 `build()`가 반환하는 객체로, Runner의 유일한 입력이다.

```
Components
├── cfg       : AppConfig        전체 설정
├── clock     : Clock            RealClock 또는 FakeClock
├── source    : Source           🔓 sources/source_api.py
├── detector  : Detector         🔓 detectors/detector_api.py
├── health    : HealthMonitor    safety/health.py
├── fsm       : SafetyFSM        safety/fsm.py
├── outputs   : list[Output]     🔓 outputs/output_api.py (복수 가능)
├── link      : NodeLink | None  link/node.py           (설정에 link:가 있을 때만)
└── confirm   : ConfirmTracker | None  safety/confirm.py (link와 항상 함께)
```

---

## 3. 주요 설계 패턴

### 3.1 인터페이스 (ABC)

`*_api.py`의 클래스는 동작 코드를 거의 포함하지 않는다. 구현체가 지켜야 할 메서드와 반환 타입만 정의한다.

```
Source (인터페이스)     ← Runner가 참조하는 타입
 ├── DummySource
 ├── WebcamSource
 └── VideoFileSource
```

`@abstractmethod`가 붙은 메서드는 모든 구현체가 반드시 구현해야 한다. 구현 파일을 읽기 전에 해당 `*_api.py`를 먼저 확인하면 각 메서드의 역할을 파악하기 쉽다.

### 3.2 레지스트리

설정 파일의 `name` 값을 실제 클래스로 변환하는 과정이다.

```mermaid
sequenceDiagram
    participant Y as config/*.yaml
    participant I as sources/__init__.py
    participant R as SOURCES (Registry)
    participant A as app.py build()

    Note over I,R: ① 모듈 import 시점
    I->>R: @SOURCES.register("dummy") → DummySource 등록
    I->>R: @SOURCES.register("webcam") → WebcamSource 등록
    Note over Y,A: ② 조립 시점
    A->>Y: source.name = "webcam", options = {...}
    A->>R: create("webcam", inject, **options)
    R-->>A: WebcamSource(...) 인스턴스
```

- **등록:** 구현 클래스에 `@SOURCES.register("이름")` 데코레이터를 붙이면 모듈이 import될 때 레지스트리에 등록된다. 각 패키지의 `__init__.py`가 구현 모듈을 import하는 것은 이 때문이며, 새 구현을 추가할 때는 `__init__.py`에도 import를 추가해야 한다.
- **생성:** `create()`는 YAML의 `options`를 생성자 인자로 전달한다. 정의되지 않은 옵션은 생성 시 오류가 발생한다.

설정 항목과 레지스트리의 대응:

| 설정 위치 | 레지스트리 | 등록된 이름 |
|---|---|---|
| `source.name` | `SOURCES` (`sources/source_api.py`) | `dummy`, `webcam`, `video` |
| `model.name` | `DETECTORS` (`detectors/detector_api.py`) | `dummy`, `yolo_onnx` |
| `outputs[].name` | `OUTPUTS` (`outputs/output_api.py`) | `console`, `viewer`, `serial`, `mock_mcu` |
| C 설정의 `outputs[].name` | `VERDICT_OUTPUTS` (`outputs/verdict_api.py`) | `console`, `decision` (기존 Output을 감쌈. 예: `output: serial`) |
| `fsm`, `health`, `runner`, `link` | 해당 없음 (고정 클래스) | `core/config.py`의 `FsmConfig`, `LinkConfig` 등 |
| C 설정의 `hub` | 해당 없음 (고정 클래스) | `core/config.py`의 `HubConfig` |

### 3.3 의존성 주입

`build()`는 실행 중에 결정되는 값(`clock`, `roi`, `person_conf_low`)을 `inject`로 전달한다. 생성자에 **같은 이름의 매개변수가 선언된 경우에만** 해당 값이 전달된다.

```python
class MySource(Source):
    def __init__(self, clock, source_id="cam0", width=640):   # clock: 주입, width: YAML options
```

주입 대상 이름은 YAML `options`에 지정할 수 없다(충돌 시 오류).

### 3.4 Clock 추상화

부품은 `time.time()`을 직접 호출하지 않고, 주입받은 `clock.now()`를 사용한다.

| | RealClock | FakeClock |
|---|---|---|
| 시간 진행 | 실제 시간 | `sleep()` 또는 `advance()` 호출 시에만 진행 |
| 15초 시나리오 실행 시간 | 15초 | 즉시 완료 |
| 재현성 | 실행마다 다를 수 있음 | 항상 동일 |
| 사용처 | ThreadedRunner (실시간 운용) | SyncRunner (테스트, 시나리오) |

시간 조건(예: 3초간 비어 있으면 Y)을 실제 대기 없이 결정적으로 검증할 수 있다.

---

## 4. Runner

### 4.1 `process_frame()`

두 Runner는 판단 로직으로 동일한 함수를 사용한다. 따라서 sync 모드와 threaded 모드의 판단 결과는 일치한다.

```mermaid
sequenceDiagram
    participant RN as Runner
    participant HM as HealthMonitor
    participant DT as Detector
    participant FS as SafetyFSM

    RN->>HM: check(FramePacket)
    HM-->>RN: fault 사유 또는 None
    RN->>DT: predict(FramePacket)
    alt 검출기 예외 발생
        DT--xRN: Exception
        RN->>FS: fault("detector_error")
    else 정상
        DT-->>RN: FrameDetections
        RN->>FS: step(FrameDetections, now, fault)
    end
    FS-->>RN: Decision
```

검출기에서 예외가 발생하면 프로그램을 종료하지 않고 FAULT(N)를 출력한다. 오류 상황에서 N을 기본값으로 하는 원칙은 전체 코드에 동일하게 적용된다.

### 4.2 SyncRunner

단일 루프로 프레임을 순차 처리한다. 테스트와 시나리오 검증에 사용한다.

```
open_all()   Output → Detector → Source 순으로 초기화
             (Source를 마지막에 열어 모델 로딩 시간 동안 프레임이 오래되는 것을 방지)
반복:
    pkt = source.read()                 None이면 종료
    (직전 프레임과의 간격이 제한을 넘으면 on_no_frame() 기록)
    decision = process_frame(pkt)
    outputs.send(decision)
종료: N("shutdown") 송신
```

### 4.3 ThreadedRunner

실시간 운용에 사용한다. 세 개의 작업 스레드와 감독용 메인 스레드로 구성된다.

```mermaid
flowchart LR
    subgraph T1["T1 capture"]
        r["source.read()<br/>카메라 속도"]
    end
    subgraph T2["T2 worker"]
        p["process_frame()<br/>모델 속도"]
    end
    subgraph T3["T3 heartbeat"]
        h["200ms 주기<br/>최신 Decision 송신"]
    end
    subgraph MAIN["main"]
        sup["스레드 감시<br/>Ctrl+C · GUI(poll)"]
    end

    subgraph T4["T4 link-rx (link: 있을 때)"]
        lr["C의 Control 수신<br/>→ 현재 확인 요청"]
    end

    slot[("LatestSlot<br/>최신 프레임")]
    board[("DecisionBoard<br/>최신 판단 + 확인 상태")]
    outs["Outputs<br/>(콘솔 · ESP32 등)"]
    hubc["C (Report)"]

    r -- "put()" --> slot
    slot -- "get()" --> p
    lr -. "request()" .-> p
    p -- "publish(판단, 확인 상태)" --> board
    board -- "snapshot()" --> h
    h -- "send()" --> outs
    h -. "send_report()" .-> hubc
```

| 스레드 | 역할 | 이상 발생 시 |
|---|---|---|
| T1 capture | 카메라 프레임을 `LatestSlot`에 덮어쓴다 | 프레임 중단 → T2 대기 시간 초과 → N |
| T2 worker | 최신 프레임을 판단하여 `DecisionBoard`에 게시한다 | 처리 지연 → 판단이 오래됨 → T3가 N 송신 |
| T3 heartbeat | 판단 변경 여부와 무관하게 주기적으로 송신한다 | 송신 중단 → ESP32 워치독이 N 처리 |
| T4 link-rx | (다중 보드) C의 Control을 받아 현재 확인 요청을 갱신한다 | C 무응답 → 확인 요청 없음 → 확인 초기화. 이 노드의 자체 출력은 영향 없음 |
| main | 작업 스레드 생존을 감시한다 | 스레드 종료 감지 → 전체 종료, 마지막으로 N 송신 |

**LatestSlot을 사용하는 이유:** 일반 큐를 사용하면 모델 처리 속도가 카메라보다 느릴 때 프레임이 누적되어 과거 영상을 기준으로 판단하게 된다. LatestSlot은 크기 1의 버퍼에 항상 최신 프레임을 덮어쓰고, 처리되지 않은 이전 프레임은 폐기한다. (DESIGN.md §3.2)

**판단과 확인 상태를 한 쌍으로 게시하는 이유:** 따로 읽으면 "새 판단 + 옛 확인 결과"가 섞여 C에 보고될 수 있다. heartbeat가 판단을 `decision_stale` N으로 바꿔 보낼 때는 확인 결과도 `fault`로 바꾼다 (worker가 멈춘 동안 `empty`를 계속 echo하면 안 된다).

SyncRunner도 `link:`가 있으면 프레임마다 도착한 Control을 모두 반영하고, 판단마다 Report를 보낸다 (판단 1개 = 하트비트 1회로 본다).

### 4.4 HubRunner (C)

카메라·모델 없이 A·B의 Report를 모아 판정한다. (`hub.py`, DESIGN.md §7.4.6)

| 스레드 | 역할 | 이상 발생 시 |
|---|---|---|
| hub-rx | Report 수신 → decode → `PeerTracker` | 깨진·중복·지난 boot의 메시지는 무시하고 센다 |
| heartbeat | 200 ms마다 `Aggregator.step` → 모든 `VerdictOutput.show` → 각 노드에 Control 송신 | 송신 중단 → ESP32 워치독이 LED N, 노드는 C 무응답으로 확인 초기화 |
| main | 스레드 감시, Ctrl+C, 출력의 `poll()`, 상태 로그 | 종료 시 출력에 `UNKNOWN(shutdown)`, 노드에 `confirm_id` 없는 Control |

`Hub.poll()`(수신 1개)과 `Hub.tick()`(판정 1회)은 함수로 분리되어 있어, 테스트에서 FakeClock + `LoopbackNetwork`로 스레드 없이 돌릴 수 있다.

---

## 5. 권장 읽기 순서

```mermaid
flowchart TD
    S0["0 · 실행 확인"] --> S1
    S1["1 · 계약 🔓<br/>core/packets.py → 각 `*_api.py`"] --> S2
    S2["2 · 조립<br/>__main__.py → app.py → core/registry.py<br/>+ config/default.yaml"] --> S3
    S3["3 · 실행 루프<br/>runners.py: process_frame → SyncRunner"] --> S4
    S4["4 · 판단<br/>DESIGN.md §8 → safety/"] --> S5
    S5["5 · 실시간 처리<br/>ThreadedRunner, core/slot.py, core/clock.py"] --> S6
    S6["6 · 담당 구현체"] --> S7
    S7["7 · 다중 보드<br/>DESIGN.md §7.4 → link/messages.py → safety/confirm.py → safety/aggregator.py → hub.py"]
```

### 0. 실행 확인

```powershell
.venv\Scripts\python -m psafe                                        # 가상 시나리오 1개 실행
.venv\Scripts\python -m psafe --scenario "scenarios/*.yaml" --check  # 전체 시나리오 검증
```

`scenarios/entry_normal.yaml`의 `timeline`과 로그를 대조하면, 사람이 나간 시점(9초)으로부터 `clear_time_s`(3초)가 지난 뒤 Y가 출력되는 것을 확인할 수 있다.

### 1. 계약 🔓

- `core/packets.py`: 파이프라인에서 사용하는 데이터 타입. §1.1의 각 화살표와 대응한다.
- `sources/source_api.py`, `detectors/detector_api.py`, `outputs/output_api.py`: 부품별 인터페이스. `@abstractmethod`가 필수 구현 메서드다.
- `FramePacket`과 `FrameDetections`의 `seq`, `timestamp`는 FSM이 오래되거나 순서가 뒤바뀐 프레임을 거부하는 데 사용된다.
- `Output`의 `send()`는 하트비트마다 호출되고, `on_frame()`은 프레임 처리마다 호출된다.
- `Decision.count`는 ROI 안 사람 수다 (`OCCUPIED`일 때만 > 0). C에 보고할 때 쓴다.

### 2. 조립

- `__main__.py`의 `main()`: §2.1의 호출 순서를 따른다.
- `config/default.yaml`: `source`, `model`, `outputs`, `fsm`, `health`, `runner`의 6개 블록으로 구성된다.
- `app.py`의 `build()`와 `core/registry.py`: §3.2, §3.3 참고.

### 3. 실행 루프

- `runners.py`의 `process_frame()`(§4.1)과 `SyncRunner.run()`(§4.2).
- 종료 시 마지막으로 송신되는 신호는 항상 N(`shutdown`)이다.

### 4. 판단

`DESIGN.md §8`의 상태 정의(INIT / OCCUPIED / VERIFYING / CLEAR / FAULT)를 먼저 참고한다. `safety/` 패키지의 책임은 다음과 같다.

| 파일 | 책임 | 입력 → 출력 |
|---|---|---|
| `health.py` | 카메라 화면 이상 검사 (암전, 과노출, 화면 정지) | `FramePacket` → 사유 문자열 또는 `None` |
| `roi.py` | 검출 박스와 감시 구역의 접촉 여부 판정 | `bbox` → `bool` |
| `fsm.py` | 상태 전이 및 최종 Y/N 결정 | `FrameDetections`, fault → `Decision` |

판단 규칙은 비대칭이다. 사람이 한 프레임이라도 검출되면 즉시 N이 되고, Y는 `clear_time_s` **및** `clear_min_frames` 조건을 모두 만족해야 출력된다. FAULT에서 복구할 때는 반드시 VERIFYING을 거친다.

### 5. 실시간 처리

- `ThreadedRunner` 클래스 docstring과 §4.3.
- `core/slot.py`: `put()`은 덮어쓰고, `get()`은 새 항목이 들어올 때까지 대기한다.
- `core/clock.py`: §3.4.

### 6. 담당 구현체

담당 파트의 `*_api.py` 인터페이스에 맞춰 구현한 뒤, 다음 순서로 확인한다.

1. `config/*.yaml`의 `name`을 구현체의 등록 이름으로 변경한다.
2. `--scenario "scenarios/*.yaml" --check`로 전체 시나리오를 검증한다.
3. 안전 불변식("실제로 사람이 있는데 Y" 0회)이 유지되는지 확인한다. `scenario_check.py`가 자동으로 검사한다.

### 7. 다중 보드

1. DESIGN.md §7.4.2~7.4.6: 왜 UDP인지, 시계 규칙, 메시지, 확인(CONFIRM) 규칙, 판정 우선순위.
2. `link/messages.py`: 메시지 형식과 검증 규칙. 잘못된 메시지는 만들 수도 받을 수도 없다.
3. `link/peer.py`: ESP32 `Receiver`(`outputs/protocol.py`)와 같은 역할의 네트워크판.
4. `safety/confirm.py`, `safety/aggregator.py`: 순수 로직. 상단 docstring의 상태도를 먼저 본다.
5. `hub.py`와 `tests/test_hub.py`의 `test_three_processes_worth_of_components_over_real_udp`: 전체가 이어지는 모습.

---

## 6. 주제별 참조

| 주제 | 참조 위치 |
|---|---|
| 프로그램 진입점 | `__main__.py`, §2.1 |
| 설정 이름과 클래스의 매핑 | `core/registry.py`, 각 패키지의 `__init__.py`, §3.2 |
| 모듈 간 데이터 형식 | 🔓 `core/packets.py` |
| 새 Source / Detector / Output 추가 | 해당 패키지의 🔓 `*_api.py` 상속 → `@register` → `__init__.py`에 import 추가 |
| Y가 출력되지 않는 원인 | `safety/fsm.py`(VERIFYING 조건), 로그의 `reason` 값, DESIGN.md §8.2.1 |
| 카메라 이상으로 인한 FAULT | `safety/health.py`, 설정의 `health` 블록 |
| 감시 구역 변경 | 설정의 `fsm.roi` (0~1 정규화 좌표) |
| 스레드 구성 | `runners.py`의 `ThreadedRunner`, §4.3, DESIGN.md §3.3 |
| 시간 추상화 | `core/clock.py`, §3.4 |
| 시나리오 검증 | `scenario_check.py`, `scenarios/*.yaml`의 `expect` |
| 설정 오류 처리 | `core/config.py` (정의되지 않은 키는 오류로 처리) |
| 보드 간 메시지 형식·검증 규칙 | 🔓 `link/messages.py`, DESIGN.md §7.4.4 |
| C가 "판단 불가"를 내는 원인 | 로그의 `reason` 값(`A_timeout`, `B_stale`, `confirm_timeout` 등), `safety/aggregator.py`, §9.8 |
| 확인(CONFIRM)이 끝나지 않는 원인 | `safety/confirm.py`(요청 이후 프레임만 셈), `link.confirm_frames`, `hub.confirm_timeout_s` |
| 새 디스플레이 추가 | 🔓 `outputs/verdict_api.py`의 `VerdictOutput` 상속 → `@VERDICT_OUTPUTS.register` → C 설정 `outputs`에 이름 지정. 표시 규칙 5개는 같은 파일 docstring |
| 네트워크 고장 흉내 | `link/transport.py`의 `LoopbackNetwork` (`set_fault`, `cut`, `heal`) |
| A·B·C를 한 프로세스에서 시험 | `sim.py`의 `MultiSim` (노드마다 다른 시계, `at()`으로 단절·재시작 예약), `check_invariant` |
| PC 한 대에서 A·B·C 실행 | `config/hub_c.yaml`, `node_a.yaml`, `node_b.yaml` (README §5.2) |

---

## 7. 테스트

각 테스트 함수의 이름은 해당 부품의 기대 동작을 기술한다. 동작 명세로 참고할 수 있다.

| 대상 | 테스트 파일 |
|---|---|
| FSM 상태 전이 | `tests/test_fsm.py` |
| ROI 판정 | `tests/test_roi.py` |
| 카메라 이상 감지 | `tests/test_health.py` |
| LatestSlot | `tests/test_slot.py` |
| 설정 파싱 | `tests/test_config.py` |
| 파이프라인 전체 | `tests/test_pipeline_smoke.py` |
| 시나리오 | `tests/test_scenarios.py`, `scenarios/*.yaml` |
| 실시간 스레드 | `tests/test_threaded.py` (`-m slow`) |
| 보드 간 메시지·전송·수신 추적 | `tests/test_link.py` (깨진 바이트, 고장 주입, localhost UDP) |
| A·B 확인 처리, `Decision.count`, 노드 Runner 연결 | `tests/test_confirm.py` |
| C 판정, ESP32 매핑, 출력 | `tests/test_aggregator.py` |
| C 실행, 설정, A·B·C 실제 UDP 연결 | `tests/test_hub.py` |
| 다중 보드 통합 시뮬레이션 (시계 어긋남, 네트워크 고장, 재시작, 무작위 30종) | `tests/test_multisim.py` |

```powershell
.venv\Scripts\python -m pytest -q          # 기본 테스트
.venv\Scripts\python -m pytest -q -m slow  # 실시간 스레드 테스트
```

---

## 8. 예제

1. **FAULT 발생:** `scenarios/camera_outage.yaml`은 5~7초 구간에 카메라 프레임이 중단되는 시나리오다. 실행 로그에서 `CLEAR → FAULT → VERIFYING → CLEAR` 순서의 상태 변화를 확인할 수 있다.
2. **판정 시간 변경:** `config/default.yaml`의 `fsm.clear_time_s`를 변경한 뒤 `--check`를 실행하면, 시간 조건에 의존하는 expect 항목이 실패한다.
3. **Output 추가:** `outputs/output_api.py`의 `Output`을 상속하고 `@OUTPUTS.register`로 등록한 뒤 `outputs/__init__.py`에 import를 추가하면, YAML의 `outputs`에 이름을 지정하여 사용할 수 있다. `send()`는 판단 변경 여부와 관계없이 하트비트마다 호출된다.

---

## 9. 용어 정리

이름이 비슷하거나 같은 개념을 여러 이름으로 부르는 경우를 정리한다.

### 9.1 Y / N과 상태

| 출력 | `Decision.safe` | `Decision.state` | 비고 |
|---|---|---|---|
| **Y** | `True` | `CLEAR` | Y가 나오는 유일한 상태 |
| **N** | `False` | `INIT`, `OCCUPIED`, `VERIFYING`, `FAULT` | `core/packets.py`의 `unsafe()`는 N 판단을 만드는 단축 함수 |

상태별 `reason` 값:

| 상태 | `reason` | 의미 |
|---|---|---|
| `INIT` | `no_decision_yet` | 시작 직후, 아직 판단 없음 (heartbeat) |
| `OCCUPIED` | `person_in_roi` | 감시 구역 안에 사람 검출 |
| `VERIFYING` | `verifying` | 비어 있음 확인 중 (`clear_time_s`, `clear_min_frames` 미충족) |
| `CLEAR` | `clear` | 비어 있음 확인 완료 |
| `FAULT` | `empty_frame`, `too_dark`, `too_bright`, `frozen_frame` | 카메라 이상 (HealthMonitor) |
| `FAULT` | `stale_frame`, `out_of_order`, `frame_gap`, `no_frame` | 입력 시간 이상 (SafetyFSM, Runner) |
| `FAULT` | `detector_error` | 검출기 예외 (Runner) |
| `FAULT` | `decision_stale` | worker 멈춤으로 판단 또는 Y의 근거 프레임이 오래됨 (heartbeat) |
| `FAULT` | `recovering` | 이상 해소 후 정상 프레임 누적 중. **상태는 아직 FAULT**이며, `fault_recover_frames`장이 쌓이면 VERIFYING으로 넘어간다 |
| `FAULT` | `shutdown` | 프로그램 종료 시 마지막 송신 |

### 9.2 `fault`의 세 가지 쓰임

| 표기 | 종류 | 의미 |
|---|---|---|
| `State.FAULT` | 상태 | FSM 상태 중 하나 |
| `SafetyFSM.fault(reason, ...)` | 메서드 | 지정한 사유로 FAULT 상태에 진입 |
| `SafetyFSM.step(..., fault=...)` | 인자 | HealthMonitor가 반환한 사유 문자열 (`None`이면 정상) |

### 9.3 시각

| 이름 | 의미 |
|---|---|
| `FramePacket.timestamp`, `FrameDetections.timestamp`, `Decision.timestamp` | 프레임 **촬영** 시각 |
| `Decision.decided_at` | 판단을 내린 시각 |
| `now` (인자) | 호출 시점의 `clock.now()` |

모든 시각은 주입된 Clock 기준이며, 프레임 나이는 `now - timestamp`로 계산한다.

**보드 간에는 시각을 비교하지 않는다** (파이마다 시계가 다르다. DESIGN.md §7.4.3).

| 이름 | 누구의 시계 | 의미 |
|---|---|---|
| `Report.evidence_age_ms` | 보내는 노드 | 보낼 때 기준, 판단 근거 프레임이 몇 ms 전 것인가 |
| `Received.received_at` | 받는 쪽 | 메시지를 받은 시각 (`link/peer.py`) |
| `Report.echo_hold_ms` | 보내는 노드 (길이) | 마지막 Control을 받은 뒤 이 Report를 보낼 때까지 |
| `Received.transit_s` | 받는 쪽 (C) | 전달 시간 상한 = 왕복 시간 = (받은 시각 − echo한 Control 보낸 시각) − `echo_hold_ms` |
| `NodeView.evidence_age_ms` | C | `evidence_age_ms + 왕복 시간 + (C의 now − received_at)`. 왕복 시간을 모르면 `None` |
| `ConfirmRequest.since` | 노드 | 이 확인 요청을 **처음** 받은 시각. 이보다 늦게 찍은 프레임만 확인에 쓴다 |

### 9.4 시간 제한 설정

| 설정 | 의미 | 사용처 |
|---|---|---|
| `fsm.max_frame_age_ms` | 프레임 정보의 유효 기간. 도착한 프레임이 이보다 오래되었거나(`stale_frame`), 이 시간 동안 새 프레임이 없으면(`no_frame`) FAULT | `safety/fsm.py`, `runners.py` |
| `runner.max_decision_age_ms` | heartbeat가 보낼 판단의 유효 기간. 초과 시 `decision_stale` | `runners.py` (heartbeat) |
| `runner.heartbeat_ms` | 판단 송신 주기 | `runners.py` (heartbeat) |
| (ESP32) 500 ms | 유효 패킷 수신 한도. 초과 시 ESP32가 강제 N | 펌웨어 (DESIGN §7.3) |
| `link.node_timeout_ms` | 노드: C가 이 시간 동안 조용하면 확인 요청을 버린다 | `link/node.py` |
| `hub.node_timeout_ms` | C: 노드가 이 시간 동안 조용하면 무응답(`*_timeout`) | `hub.py`, `link/peer.py` |
| `hub.max_evidence_age_ms` | C: CLEAR인데 근거 나이가 이보다 크면 `*_stale` | `safety/aggregator.py` |
| `hub.confirm_timeout_s` | C: 확인이 이 시간 안에 안 끝나면 `confirm_timeout` | `safety/aggregator.py` |
| `hub.heartbeat_ms` | C의 판정·Control 송신 주기 | `hub.py` |

### 9.5 버려진 프레임 카운터 (`RunResult`, `DummySource`)

| 이름 | 위치 | 의미 |
|---|---|---|
| `skipped` | Source | 소비자가 느려서 Source가 건너뛴 프레임 |
| `dropped` | LatestSlot | worker가 가져가기 전에 덮어쓰인 프레임 |
| `outage_frames` | DummySource | 카메라 끊김 구간이라 생성되지 않은 프레임 |
| `timeouts` | Runner | 프레임 없이 대기 시간이 끝난 횟수 (`no_frame` 발생 횟수) |

### 9.6 신뢰도 임계값 (2단계)

| 설정 | 단계 | 역할 |
|---|---|---|
| `model.options.conf_threshold` (기본 0.20) | Detector | 후보를 넓게 남긴다. 화면 표시·로그에는 이 기준으로 보인다 |
| `fsm.person_conf_low` (기본 0.30) | SafetyFSM | 이 값 이상인 `person`만 판단에 반영한다 |

### 9.7 이름이 비슷한 항목

| 이름 | 의미 |
|---|---|
| `Detection` | 검출 박스 1개 (`cls`, `conf`, `bbox`) |
| `FrameDetections` | 프레임 1장의 검출 결과 묶음 (`Detection` 여러 개 + `seq`, `timestamp`, 모델 정보) |
| `meta["visible"]` | DummySource: 카메라에 **보이는** 객체. DummyDetector가 이것을 보고한다 |
| `meta["world"]` | DummySource: 구역에 **실제로 있는** 객체(정답). 안전 불변식 검사에 쓴다 |
| `sources/dummy.py` | 가상 **입력** (`source.name: dummy`) |
| `detectors/dummy.py` | 가상 **검출기** (`model.name: dummy`) |
| `scenarios/*.yaml` | 시나리오 데이터 |
| `sources/scenario.py` | 시나리오 파일 파싱 |
| `scenario_check.py` | 시나리오 실행 및 `expect` 검증 |
| `src/psafe/detectors/` | 검출기 코드 |
| `models/` (저장소 루트) | ONNX 모델 파일 (git 제외) |
| `Decision` / `Verdict` | 노드 하나의 판단 (Y/N) / C의 최종 판정 (4가지) |
| `Output` / `VerdictOutput` | 노드의 출력 (`send(Decision)`) / C의 출력 (`show(Verdict)`) |
| `Report` / `Control` | A·B → C 보고 / C → A·B 확인 번호 |
| `ConfirmRequest` / `ConfirmStatus` | 노드가 받은 확인 요청 / 그 요청에 대한 노드의 답 |
| `boot` / `seq` | 프로세스 시작마다 바뀌는 값(재시작 구분) / 송신마다 1씩 증가 |

### 9.8 C의 판정과 `reason`

| `Verdict.kind` | 화면 | `reason` 예 | ESP32 (STATE / SAFE) |
|---|---|---|---|
| `OCCUPIED` | 사람 있음 | `person_A`, `person_B` | `OCCUPIED` / N |
| `UNKNOWN` | 판단 불가 | `A_timeout`(무응답), `A_fault`, `A_init`, `A_no_rtt`(왕복 시간 모름), `A_stale`(근거 오래됨, 왕복 시간 포함), `confirm_fault_B`, `confirm_timeout`, `shutdown` | `FAULT` / N |
| `CHECKING` | 확인 중 | `A_verifying`(노드 FSM 확인 중), `confirming`(확인 요청 진행 중) | `VERIFYING` / N |
| `EMPTY` | 비었음 확인 | `confirmed` | `CLEAR` / **Y** |

우선순위는 위에서부터다. `OCCUPIED`가 `UNKNOWN`보다 위인 이유는 DESIGN.md §7.4.6.

확인 응답(`Report.confirm`) 값: `None`(요청 없음) / `pending`(확인 중) / `empty` / `occupied` / `fault`. `occupied`와 `fault`는 그 `confirm_id` 동안 유지된다.
