"""The sensitivity surface must reproduce the paper's reported boundary exactly."""
from cap_coordination_kit.sensitivity_surface import build_surface


def test_surface_headline_counts():
    res = build_surface()
    # 6 vacate-latency rows x 5 receiver-delay columns.
    assert res["cells"] == 30
    # A plain mutex co-occupies the load point in exactly 20 of 30 cells.
    assert res["naive_co_occupied"] == 20
    # CAP's atomic vacate-before-enter never co-occupies.
    assert res["atomic_co_occupied"] == 0


def test_boundary_is_vacate_gt_delay():
    """Mutex co-occupancy occurs in exactly the cells where the sender is still
    clearing (vacate latency) when the receiver enters (receiver delay)."""
    res = build_surface()
    for (v, d), cell in res["grid"].items():
        mutex_co_occupies = cell["naive_occ"] > 1
        assert mutex_co_occupies == (v > d), (v, d, cell)
        # CAP holds occupancy at one in every cell, on both sides of the boundary.
        assert cell["atomic_occ"] == 1
