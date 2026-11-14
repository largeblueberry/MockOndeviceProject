import pytest

from psafe.fsm.roi import Roi

SQUARE = Roi([[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]])
TRIANGLE = Roi([[0.5, 0.0], [1.0, 1.0], [0.0, 1.0]])


@pytest.mark.parametrize("bbox,expected", [
    ((0.4, 0.4, 0.6, 0.6), True),     # fully inside
    ((0.0, 0.0, 1.0, 1.0), True),     # contains the whole ROI (no corner inside it)
    ((0.0, 0.3, 0.08, 0.8), False),   # fully outside, left
    ((0.02, 0.3, 0.12, 0.8), True),   # sliver over the left edge
    ((0.0, 0.0, 0.1, 0.1), True),     # touches the corner exactly -> counts (fail safe)
    ((0.95, 0.95, 1.0, 1.0), False),
])
def test_square(bbox, expected):
    assert SQUARE.touches(bbox) is expected


def test_thin_bbox_crossing_without_corners_inside():
    # tall thin box crossing the top edge: no bbox corner inside, no ROI vertex inside
    assert SQUARE.touches((0.5, 0.0, 0.51, 0.2))
    # a horizontal bar crossing the whole ROI
    assert SQUARE.touches((0.0, 0.5, 1.0, 0.52))


def test_non_rectangular_roi():
    assert TRIANGLE.touches((0.45, 0.5, 0.55, 0.6))
    assert not TRIANGLE.touches((0.0, 0.0, 0.1, 0.1))   # top-left, outside the triangle


def test_needs_three_points():
    with pytest.raises(ValueError):
        Roi([[0, 0], [1, 1]])
