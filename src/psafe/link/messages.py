"""보드 간 메시지 (DESIGN.md §7.4.4). 순수 로직: 소켓도 시계도 만지지 않는다.

    A/B -> C   Report   200 ms마다. 그 노드의 최신 판단 + CONFIRM 진행 상황
    C -> A/B   Control  200 ms마다. 현재 confirm_id

형식: UTF-8 JSON 객체 1개 = UDP 데이터그램 1개. 필드 집합은 정확히 일치해야
한다 (빠진 필드·모르는 필드 모두 거부). 형식을 바꾸면 PROTOCOL_VERSION을 올린다.

규칙 검사는 데이터 클래스 생성 시점(__post_init__)에 한 번만 한다. 그래서
  * 잘못된 메시지는 만들 수조차 없고 (encode 전에 ValueError)
  * decode도 같은 검사를 거친다 (보내는 쪽과 받는 쪽 규칙이 어긋날 수 없다).
"""

import json
import secrets
from dataclasses import dataclass
from typing import Any

from psafe.core.packets import State

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 512          # 실제 Report는 ~250 B. 이보다 크면 해석하지 않는다
MAX_SEQ = 2**53 - 1              # JSON 숫자가 정밀도를 잃지 않는 범위
MAX_COUNT = 999
MAX_TEXT = 64                    # node, boot, reason 길이 상한

CONFIRM_VALUES = ("pending", "empty", "occupied", "fault")


def new_boot_id() -> str:
    """프로세스를 시작할 때마다 새로 만든다. 재시작으로 seq가 0이 되어도
    받는 쪽이 '지연 도착'이 아니라 '재시작'으로 구분하게 해 준다."""
    return secrets.token_hex(4)


# ---- 검사 도우미 ----------------------------------------------------------
# bool은 int의 하위 타입이라 isinstance(True, int)가 True다. JSON의 true가
# seq 자리에 들어오는 것을 막으려고 type()으로 정확히 비교한다.

def _int(name: str, v: Any, lo: int, hi: int) -> None:
    if type(v) is not int or not lo <= v <= hi:
        raise ValueError(f"{name} must be an int in [{lo}, {hi}], got {v!r}")


def _opt_int(name: str, v: Any, lo: int, hi: int) -> None:
    if v is not None:
        _int(name, v, lo, hi)


def _text(name: str, v: Any) -> None:
    if type(v) is not str or not 1 <= len(v) <= MAX_TEXT or not v.isascii() or not v.isprintable():
        raise ValueError(f"{name} must be 1..{MAX_TEXT} printable ASCII chars, got {v!r}")


def _header(node: Any, boot: Any, seq: Any) -> None:
    _text("node", node)
    _text("boot", boot)
    _int("seq", seq, 0, MAX_SEQ)


# ---- 메시지 ---------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Report:
    """A/B -> C. 필드 의미는 DESIGN.md §7.4.4 표."""
    node: str
    boot: str
    seq: int
    state: State
    safe: bool
    count: int                      # ROI 안 사람 수
    reason: str
    evidence_age_ms: int | None     # 보낼 때 기준 근거 프레임 나이 (보내는 쪽 시계). 근거 없으면 None
    confirm_id: int | None          # 마지막으로 받은 C의 confirm_id
    confirm: str | None             # None / pending / empty / occupied / fault

    TYPE = "report"

    def __post_init__(self) -> None:
        _header(self.node, self.boot, self.seq)
        if not isinstance(self.state, State):
            raise ValueError(f"state must be a State, got {self.state!r}")
        if type(self.safe) is not bool:
            raise ValueError(f"safe must be a bool, got {self.safe!r}")
        _int("count", self.count, 0, MAX_COUNT)
        _text("reason", self.reason)
        _opt_int("evidence_age_ms", self.evidence_age_ms, 0, MAX_SEQ)
        _opt_int("confirm_id", self.confirm_id, 0, MAX_SEQ)
        if self.confirm is not None and self.confirm not in CONFIRM_VALUES:
            raise ValueError(f"confirm must be one of {CONFIRM_VALUES} or None, got {self.confirm!r}")

        # 규칙 (프로토콜 v1과 같은 정신: 앞뒤가 안 맞는 메시지는 믿지 않는다)
        if self.safe and self.state != State.CLEAR:
            raise ValueError(f"safe=true is only valid in CLEAR, got {self.state.value}")
        if self.safe and self.evidence_age_ms is None:
            raise ValueError("safe=true needs evidence_age_ms")
        if self.safe and self.count != 0:
            raise ValueError("safe=true with count > 0")
        if self.confirm is not None and self.confirm_id is None:
            raise ValueError("confirm result without confirm_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION, "type": self.TYPE,
            "node": self.node, "boot": self.boot, "seq": self.seq,
            "state": self.state.value, "safe": self.safe, "count": self.count, "reason": self.reason,
            "evidence_age_ms": self.evidence_age_ms,
            "confirm_id": self.confirm_id, "confirm": self.confirm,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Report":
        try:
            state = State(d["state"])
        except (ValueError, TypeError):
            raise ValueError(f"unknown state {d['state']!r}") from None
        return cls(
            d["node"], d["boot"], d["seq"], state, d["safe"], d["count"], d["reason"],
            d["evidence_age_ms"], d["confirm_id"], d["confirm"],
        )


@dataclass(frozen=True, slots=True)
class Control:
    """C -> A/B."""
    node: str
    boot: str
    seq: int
    confirm_id: int | None          # 진행 중이거나 유지 중인 확인 번호. 새 확인을 시작할 때만 바뀐다

    TYPE = "control"

    def __post_init__(self) -> None:
        _header(self.node, self.boot, self.seq)
        _opt_int("confirm_id", self.confirm_id, 0, MAX_SEQ)

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION, "type": self.TYPE,
            "node": self.node, "boot": self.boot, "seq": self.seq,
            "confirm_id": self.confirm_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Control":
        return cls(d["node"], d["boot"], d["seq"], d["confirm_id"])


Message = Report | Control

_TYPES: dict[str, type[Report] | type[Control]] = {Report.TYPE: Report, Control.TYPE: Control}
# 메시지별 필드 집합. to_dict()의 키와 같아야 한다 (테스트가 확인).
_FIELDS = {
    Report.TYPE: frozenset(
        ["v", "type", "node", "boot", "seq", "state", "safe", "count", "reason",
         "evidence_age_ms", "confirm_id", "confirm"]
    ),
    Control.TYPE: frozenset(["v", "type", "node", "boot", "seq", "confirm_id"]),
}


# ---- 직렬화 ---------------------------------------------------------------

def encode(msg: Message) -> bytes:
    data = json.dumps(msg.to_dict(), separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError(f"message too large: {len(data)} B")
    return data


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")   # NaN, Infinity


def decode(data: bytes) -> Message | str:
    """해석한다. 유효하면 Report/Control, 아니면 무시 사유 문자열:
    too_large | bad_json | bad_version | bad_type | bad_fields | invalid
    (parse_window()와 같은 방식: 예외를 던지지 않는다. 받는 쪽은 사유별로 세기만 한다.)"""
    if len(data) > MAX_MESSAGE_BYTES:
        return "too_large"
    try:
        d = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "bad_json"
    if not isinstance(d, dict):
        return "bad_json"
    if d.get("v") != PROTOCOL_VERSION or type(d.get("v")) is not int:
        return "bad_version"
    cls = _TYPES.get(d.get("type")) if isinstance(d.get("type"), str) else None
    if cls is None:
        return "bad_type"
    if frozenset(d) != _FIELDS[cls.TYPE]:
        return "bad_fields"
    try:
        return cls.from_dict(d)
    except (ValueError, TypeError):
        return "invalid"
