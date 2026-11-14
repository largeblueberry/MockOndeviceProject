"""ROI: does a detection touch the monitored bay? (DESIGN.md §8.3)

Any overlap counts, even a sliver: a person standing on the boundary is
treated as inside. Touching edges also count (fail safe).
Pure Python, no shapely: polygons here have a handful of points.
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
    # collinear / touching cases
    return (
        (d1 == 0 and _on_segment(p1, q1, q2)) or (d2 == 0 and _on_segment(p2, q1, q2))
        or (d3 == 0 and _on_segment(q1, p1, p2)) or (d4 == 0 and _on_segment(q2, p1, p2))
    )


def point_in_polygon(p: Point, poly: list[Point]) -> bool:
    """Ray casting. Points exactly on an edge may go either way; the edge
    intersection test in bbox_overlaps_polygon covers that case."""
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
    # 1) a bbox corner inside the polygon, or 2) a polygon vertex inside the bbox
    if any(point_in_polygon(c, poly) for c in rect):
        return True
    if any(x1 <= px <= x2 and y1 <= py <= y2 for px, py in poly):
        return True
    # 3) edges cross (e.g. a thin bbox crossing a polygon edge with no corner inside)
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
