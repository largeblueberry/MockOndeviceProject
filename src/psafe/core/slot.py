"""LatestSlot: 항상 최신 항목 1개만 담는 크기 1 채널 (DESIGN.md §3.2).

put()은 덮어쓰고, get()은 마지막으로 읽은 것보다 새로운 항목을 기다린다.
"오래된 것 버리기"를 하는 maxsize=1 큐와 같지만, 일반 queue.Queue에서 생기는
"가득 참 -> get_nowait -> put" 사이의 경쟁 상태가 없다.
"""

import threading
from typing import Generic, Protocol, TypeVar


class HasSeq(Protocol):
    seq: int


T = TypeVar("T", bound=HasSeq)


class LatestSlot(Generic[T]):
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._item: T | None = None
        self._last_read_seq = -1
        self._closed = False
        self.dropped = 0   # 아무도 읽기 전에 덮어쓰인 항목 수
        self.put_count = 0

    def put(self, item: T) -> None:
        with self._cond:
            if self._item is not None and item.seq <= self._item.seq:
                raise ValueError(f"seq must increase: {item.seq} after {self._item.seq}")
            if self._item is not None and self._item.seq > self._last_read_seq:
                self.dropped += 1
            self._item = item
            self.put_count += 1
            self._cond.notify_all()

    def get(self, timeout: float | None = None) -> T | None:
        """새 항목이 올 때까지 대기한다. 시간 초과나 close 시 None을 반환한다."""
        with self._cond:
            ok = self._cond.wait_for(lambda: self._closed or self._has_new(), timeout)
            if not ok or not self._has_new():
                return None
            assert self._item is not None
            self._last_read_seq = self._item.seq
            return self._item

    def peek(self) -> T | None:
        """대기하지 않고, 읽음 표시도 하지 않고 최신 항목을 본다 (하트비트용)."""
        with self._cond:
            return self._item

    def close(self) -> None:
        """대기 중인 스레드를 모두 깨워 종료할 수 있게 한다."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def _has_new(self) -> bool:
        return self._item is not None and self._item.seq > self._last_read_seq
