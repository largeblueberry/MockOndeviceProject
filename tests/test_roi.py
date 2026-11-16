import pytest

from psafe.fsm.roi import Roi

SQUARE = Roi([[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]])
TRIANGLE = Roi([[0.5, 0.0], [1.0, 1.0], [0.0, 1.0]])


@pytest.mark.parametrize("bbox,expected", [
    ((0.4, 0.4, 0.6, 0.6), True),     # 완전히 안쪽
    ((0.0, 0.0, 1.0, 1.0), True),     # ROI 전체를 포함 (박스 꼭짓점은 ROI 안에 없음)
    ((0.0, 0.3, 0.08, 0.8), False),   # 완전히 바깥, 왼쪽
    ((0.02, 0.3, 0.12, 0.8), True),   # 왼쪽 변에 살짝 걸침
    ((0.0, 0.0, 0.1, 0.1), True),     # 꼭짓점에 정확히 닿음 -> 포함 (안전 쪽 판단)
    ((0.95, 0.95, 1.0, 1.0), False),
])
def test_square(bbox, expected):
    assert SQUARE.touches(bbox) is expected


def test_thin_bbox_crossing_without_corners_inside():
    # 위쪽 변을 가로지르는 길고 얇은 박스: 박스 꼭짓점도 ROI 꼭짓점도 서로 안에 없음
    assert SQUARE.touches((0.5, 0.0, 0.51, 0.2))
    # ROI 전체를 가로지르는 가로 막대
    assert SQUARE.touches((0.0, 0.5, 1.0, 0.52))


def test_non_rectangular_roi():
    assert TRIANGLE.touches((0.45, 0.5, 0.55, 0.6))
    assert not TRIANGLE.touches((0.0, 0.0, 0.1, 0.1))   # 왼쪽 위, 삼각형 바깥


def test_needs_three_points():
    with pytest.raises(ValueError):
        Roi([[0, 0], [1, 1]])
