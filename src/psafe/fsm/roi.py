"""ROI: 검출 박스가 감시 구역에 닿았는가? (DESIGN.md §8.3)

조금이라도 겹치면 포함으로 본다. 경계에 선 사람도 안쪽으로 취급한다.
변끼리 맞닿기만 해도 포함이다 (안전 쪽 판단).
다각형의 점이 몇 개뿐이므로 shapely 없이 순수 Python으로 구현한다.
"""

from psafe.core.packets import BBox

Point = tuple[float, float]


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _on_segment(p: Point, a: Point, b: Point) -> bool:
    return min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])


def segments_intersect(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    d1, d2 = _cross(q1, q2, p1), _cross(q1, q2, p2)
    d3, d4 = _cross(p1, p2, q1), _cross(p1, p2, q2)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True
    # 한 직선 위에 있거나 맞닿는 경우
    return (
        (d1 == 0 and _on_segment(p1, q1, q2)) or (d2 == 0 and _on_segment(p2, q1, q2))
        or (d3 == 0 and _on_segment(q1, p1, p2)) or (d4 == 0 and _on_segment(q2, p1, p2))
    )


def point_in_polygon(p: Point, poly: list[Point]) -> bool:
    """광선 투사법. 변 위에 정확히 놓인 점은 결과가 어느 쪽이든 될 수 있으며,
    그 경우는 bbox_overlaps_polygon의 변 교차 검사가 처리한다."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def bbox_overlaps_polygon(bbox: BBox, poly: list[Point]) -> bool:
    x1, y1, x2, y2 = bbox
    rect = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    # 1) 박스 꼭짓점이 다각형 안에 있거나, 2) 다각형 꼭짓점이 박스 안에 있음
    if any(point_in_polygon(c, poly) for c in rect):
        return True
    if any(x1 <= px <= x2 and y1 <= py <= y2 for px, py in poly):
        return True
    # 3) 변끼리 교차 (예: 꼭짓점은 밖에 있지만 얇은 박스가 다각형 변을 가로지름)
    rect_edges = list(zip(rect, rect[1:] + rect[:1]))
    poly_edges = list(zip(poly, poly[1:] + poly[:1]))
    return any(segments_intersect(a, b, c, d) for a, b in rect_edges for c, d in poly_edges)


class Roi:
    def __init__(self, polygon: list[list[float]]):
        if len(polygon) < 3:
            raise ValueError("ROI polygon needs at least 3 points")
        self.polygon: list[Point] = [(float(x), float(y)) for x, y in polygon]

    def touches(self, bbox: BBox) -> bool:
        return bbox_overlaps_polygon(bbox, self.polygon)
