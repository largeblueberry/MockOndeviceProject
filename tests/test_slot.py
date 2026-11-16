import threading
from dataclasses import dataclass

import pytest

from psafe.core.slot import LatestSlot


@dataclass
class Item:
    seq: int


def test_get_returns_latest_and_counts_drops():
    s = LatestSlot()
    for i in range(5):
        s.put(Item(i))
    assert s.get(timeout=0).seq == 4
    assert s.dropped == 4          # 0..3은 읽히지 않고 덮어쓰임


def test_get_does_not_return_same_item_twice():
    s = LatestSlot()
    s.put(Item(0))
    assert s.get(timeout=0).seq == 0
    assert s.get(timeout=0.01) is None


def test_no_drop_when_consumer_keeps_up():
    s = LatestSlot()
    for i in range(3):
        s.put(Item(i))
        assert s.get(timeout=0).seq == i
    assert s.dropped == 0


def test_seq_must_increase():
    s = LatestSlot()
    s.put(Item(3))
    with pytest.raises(ValueError):
        s.put(Item(3))


def test_peek_does_not_consume():
    s = LatestSlot()
    s.put(Item(0))
    assert s.peek().seq == 0
    assert s.get(timeout=0).seq == 0


def test_get_wakes_on_put_from_other_thread():
    s = LatestSlot()
    threading.Timer(0.05, lambda: s.put(Item(7))).start()
    assert s.get(timeout=2).seq == 7


def test_close_wakes_waiter():
    s = LatestSlot()
    threading.Timer(0.05, s.close).start()
    assert s.get(timeout=2) is None
