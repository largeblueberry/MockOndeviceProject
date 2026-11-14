"""LatestSlot: a size-1 channel that always holds the newest item (DESIGN.md §3.2).

put() overwrites; get() waits for an item newer than the last one read.
Equivalent to a maxsize=1 queue with drop-oldest, but without the
race between "full -> get_nowait -> put" that a plain queue.Queue has.
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
        self.dropped = 0   # items overwritten before anyone read them
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
        """Block until a new item arrives. Returns None on timeout or close."""
        with self._cond:
            ok = self._cond.wait_for(lambda: self._closed or self._has_new(), timeout)
            if not ok or not self._has_new():
                return None
            assert self._item is not None
            self._last_read_seq = self._item.seq
            return self._item

    def peek(self) -> T | None:
        """Latest item without waiting or marking it read (used by the heartbeat)."""
        with self._cond:
            return self._item

    def close(self) -> None:
        """Wake up all waiters so threads can exit."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def _has_new(self) -> bool:
        return self._item is not None and self._item.seq > self._last_read_seq
