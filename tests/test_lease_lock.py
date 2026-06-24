"""The lease-lock experiment must reproduce the paper's reported boundary."""
from cap_coordination_kit.lease_lock import build_grid


def test_lease_steals_exactly_where_hold_outlasts_ttl():
    res = build_grid()
    assert res["cells"] == 25
    # a live holder's lock is stolen in exactly the H>T region
    assert res["lease_steals_total"] == 15
    for (h, t), cell in res["grid"].items():
        assert cell["lease_steals"] == (1 if h > t else 0), (h, t, cell)


def test_cap_deny_on_busy_never_steals_and_steal_is_audit_invisible():
    res = build_grid()
    assert res["cap_steals_total"] == 0
    # the lease steal is invisible to the no-double-grant audit (motivates fencing)
    assert res["lease_double_grants_audited_total"] == 0
