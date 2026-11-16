// psafe_gate — RPi의 Y/N 판단을 받아 기계 동작을 허가하는 ESP32 펌웨어 (DESIGN.md §7.3)
//
// 규칙: "RPi로부터 유효한 Y 패킷이 계속 들어올 때만 허가. 그 외에는 전부 N."
//
//   패킷   | STX 0xAA | SEQ | STATE | SAFE | CRC8 |   (5바이트, RPi가 200 ms마다 송신)
//   STATE  INIT=0, OCCUPIED=1, VERIFYING=2, CLEAR=3, FAULT=4
//   SAFE   'Y' 또는 'N'. 'Y'는 STATE가 CLEAR일 때만 유효
//   CRC8   SEQ, STATE, SAFE 3바이트, 다항식 0x07, 초기값 0x00
//
//   무시하는 패킷: CRC 오류, 규칙 위반(STATE 범위 밖, CLEAR가 아닌데 'Y'), 직전과 같은 SEQ
//   워치독: 마지막 유효 패킷 이후 500 ms가 지나면 강제 N
//
// 이 파일의 상수는 src/psafe/outputs/protocol.py와 항상 같아야 한다.
//
// 출력
//   내장 LED (GPIO 2)   켜짐 = Y(허가), 꺼짐 = N
//   외부 LED / 릴레이   아래 PIN_* 에 GPIO 번호를 넣으면 사용 (-1 = 사용 안 함)
//
// 상태 보고 (같은 USB 시리얼로 텍스트 출력, RPi는 읽지 않아도 된다)
//   BOOT N                              부팅 직후
//   GATE Y seq=12                       허가로 바뀜
//   GATE N reason=timeout since_ok=503  N으로 바뀜 (reason: packet_n | timeout)
//   STAT ok=.. bad_crc=.. invalid=.. dup=.. gate=Y|N   1초마다

#include <Arduino.h>

// ---- 핀 설정 ----------------------------------------------------------------
const int PIN_STATUS_LED = 2;    // 대부분의 ESP32 DevKit 내장 LED
const int PIN_GREEN      = -1;   // 예: 25
const int PIN_RED        = -1;   // 예: 26
const int PIN_RELAY      = -1;   // 예: 27 (HIGH = 동작 허가)

// ---- 프로토콜 상수 (protocol.py와 동일) ----------------------------------------
const uint32_t BAUD        = 115200;
const uint8_t  STX         = 0xAA;
const uint8_t  CRC_POLY    = 0x07;
const uint8_t  STATE_CLEAR = 3;
const uint8_t  STATE_MAX   = 4;
const uint32_t WATCHDOG_MS = 500;

// ---- 상태 -------------------------------------------------------------------
uint8_t  win[5];             // 최근 5바이트 (슬라이딩 윈도)
uint8_t  filled = 0;
bool     havePacket = false; // 유효 패킷을 한 번이라도 받았나
bool     lastSafe = false;   // 마지막 유효 패킷의 SAFE
uint8_t  lastSeq = 0;
uint32_t lastOkMs = 0;
bool     gate = false;       // 현재 출력 (true = Y)

uint32_t nOk = 0, nBadCrc = 0, nInvalid = 0, nDup = 0;
uint32_t lastStatMs = 0;

uint8_t crc8(const uint8_t* d, size_t n) {
  uint8_t c = 0;
  for (size_t i = 0; i < n; i++) {
    c ^= d[i];
    for (int b = 0; b < 8; b++) {
      c = (c & 0x80) ? (uint8_t)((c << 1) ^ CRC_POLY) : (uint8_t)(c << 1);
    }
  }
  return c;
}

void writePin(int pin, bool on) {
  if (pin >= 0) digitalWrite(pin, on ? HIGH : LOW);
}

void applyOutputs(bool safe) {
  writePin(PIN_STATUS_LED, safe);
  writePin(PIN_GREEN, safe);
  writePin(PIN_RED, !safe);
  writePin(PIN_RELAY, safe);
}

void onByte(uint8_t b) {
  memmove(win, win + 1, 4);
  win[4] = b;
  if (filled < 5) {
    filled++;
    if (filled < 5) return;
  }
  if (win[0] != STX) return;
  if (crc8(win + 1, 3) != win[4]) {   // 깨진 패킷: 계속 밀어 가며 다음 STX를 찾는다
    nBadCrc++;
    return;
  }
  uint8_t seq = win[1], st = win[2], sf = win[3];
  filled = 0;                          // 5바이트를 소비했다
  bool valid = st <= STATE_MAX && (sf == 'N' || (sf == 'Y' && st == STATE_CLEAR));
  if (!valid) { nInvalid++; return; }
  if (havePacket && seq == lastSeq) { nDup++; return; }
  havePacket = true;
  lastSeq = seq;
  lastSafe = (sf == 'Y');
  lastOkMs = millis();
  nOk++;
}

void setup() {
  int pins[] = {PIN_STATUS_LED, PIN_GREEN, PIN_RED, PIN_RELAY};
  for (int p : pins) if (p >= 0) pinMode(p, OUTPUT);
  applyOutputs(false);                 // 부팅 직후는 항상 N
  Serial.begin(BAUD);
  delay(50);
  Serial.println("BOOT N");
}

void loop() {
  while (Serial.available() > 0) onByte((uint8_t)Serial.read());

  uint32_t now = millis();
  uint32_t sinceOk = now - lastOkMs;   // unsigned 뺄셈이라 millis() 넘침에도 안전
  bool timedOut = !havePacket || sinceOk > WATCHDOG_MS;
  bool safe = havePacket && lastSafe && !timedOut;

  if (safe != gate) {
    gate = safe;
    applyOutputs(gate);
    if (gate) {
      Serial.printf("GATE Y seq=%u\n", lastSeq);
    } else {
      Serial.printf("GATE N reason=%s since_ok=%lu\n",
                    timedOut ? "timeout" : "packet_n", (unsigned long)sinceOk);
    }
  }

  if (now - lastStatMs >= 1000) {
    lastStatMs = now;
    Serial.printf("STAT ok=%lu bad_crc=%lu invalid=%lu dup=%lu gate=%c\n",
                  (unsigned long)nOk, (unsigned long)nBadCrc, (unsigned long)nInvalid,
                  (unsigned long)nDup, gate ? 'Y' : 'N');
  }
}
