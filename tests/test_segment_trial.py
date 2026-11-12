"""A second kind of space: the same arbiter and gate on a single-lane segment crossed from both ends."""

from __future__ import annotations

import math

from cap_coordination_kit.segment_trial import run_all
from cap_coordination_kit.zones import SegmentZone

UNSAFE = {("release_early", "mutex_on_done"), ("release_early", "lease_fenced"), ("holder_silent", "lease_fenced")}


def test_a_segment_zone_measures_the_distance_to_the_lane():
    z = SegmentZone.between("road:s", (-20.0, 0.0), (20.0, 0.0), 3.0)
    assert (z.x, z.y, z.radius_m) == (0.0, 0.0, 3.0)
    assert z.distance(5.0, 2.0) == 2.0                      # beside the lane
    assert math.isclose(z.distance(23.0, 4.0), 5.0)          # beyond its end: to the end point
    assert z.distance(-20.0, 0.0) == 0.0


def test_the_schemes_fail_on_the_lane_where_they_fail_at_the_bay():
    for r in run_all():
        assert (r.S1 or r.S2) == ((r.scenario, r.scheme) in UNSAFE), (r.scheme, r.scenario, r.S1, r.S2)
        if r.scenario == "nominal":
            assert r.loads == 4, (r.scheme, r.loads)          # two crossings each way
