"""Conflict zones of other shapes for the same arbiter and entry gate.

The arbiter (``occupancy``) and the gate (``entry_gate``) use a zone only through ``distance(x, y)`` - how far a point
is from the zone's core - and ``radius_m`` - how far from the core the zone reaches: a point is inside while its
distance is below the radius, and a vacancy is confirmed when the inflated distance is at least the radius. A disc
(``occupancy.Zone``) has a point as its core. Here the core is a line segment, which makes a zone of a single-lane
stretch of road (a stadium shape around the segment, ``radius_m`` being half the lane's width plus any clearance).
Nothing else changes, which is the point: the safety argument of the paper does not depend on the zone's shape.

The arbiter compares a zone registered again with the one it has by centre and radius; two segments with the same
midpoint and width would count as the same zone, so give different stretches different resource identifiers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from cap_coordination_kit.occupancy import Zone


@dataclass(frozen=True)
class SegmentZone(Zone):
    """A single-lane segment from (x1, y1) to (x2, y2); ``x``, ``y`` are its midpoint (for the arbiter's records)."""

    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0

    @classmethod
    def between(cls, resource_id: str, a: tuple[float, float], b: tuple[float, float], half_width_m: float,
                compatible: frozenset[str] = frozenset()) -> SegmentZone:
        return cls(resource_id, (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0, float(half_width_m), frozenset(compatible),
                   float(a[0]), float(a[1]), float(b[0]), float(b[1]))

    def distance(self, x: float, y: float) -> float:
        dx, dy = self.x2 - self.x1, self.y2 - self.y1
        n2 = dx * dx + dy * dy
        t = 0.0 if n2 == 0.0 else max(0.0, min(1.0, ((float(x) - self.x1) * dx + (float(y) - self.y1) * dy) / n2))
        return math.hypot(float(x) - (self.x1 + t * dx), float(y) - (self.y1 + t * dy))
