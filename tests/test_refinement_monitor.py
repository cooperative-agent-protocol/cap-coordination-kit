"""Runtime spec-conformance monitor over the reservation arbiter's transition log.

Checks that the monitor (a) passes clean traces, (b) flags the two spec-guard violations it exists
to catch -- a GRANTED-while-held-by-another (NoDoubleGrant / P2) and a HANDOVER by a non-holder
(P4) -- and (c) works online when registered as the real arbiter's on_transition callback.
"""
from __future__ import annotations

from cap.v0.core import site_agent_pb2

from cap_coordination_kit import ReservationManager
from cap_coordination_kit.refinement_monitor import (
    ReservationConformanceMonitor,
    check_records,
)


def _rec(seq, event, res, holder, prior, resolved, rid="r"):
    return {"seq": seq, "event": event, "resource_id": res, "reservation_id": rid,
            "requested_holder": holder, "prior_holder": prior, "resolved_holder": resolved,
            "expires_at": None, "reason": ""}


def test_clean_trace_conforms():
    recs = [
        _rec(1, "GRANTED", "LP-1", "m1", None, "m1"),
        _rec(2, "DENIED", "LP-1", "m2", "m1", "m1"),
        _rec(3, "RELEASED", "LP-1", "m1", "m1", None),
        _rec(4, "GRANTED", "LP-1", "m2", None, "m2"),
        _rec(5, "RELEASED", "LP-1", "m2", "m2", None),
    ]
    res = check_records(recs)
    assert res.conforming
    assert res.transitions == 5
    assert res.violations == []
    assert res.divergences == []


def test_atomic_handover_conforms():
    recs = [
        _rec(1, "GRANTED", "LP-1", "m1", None, "m1"),
        _rec(2, "DENIED", "LP-1", "m2", "m1", "m1"),
        _rec(3, "HANDOVER", "LP-1", "m2", "m1", "m2"),   # sender m1 holds -> receiver m2, atomic
        _rec(4, "RELEASED", "LP-1", "m2", "m2", None),
    ]
    res = check_records(recs)
    assert res.conforming
    assert res.event_counts.get("HANDOVER") == 1


def test_double_grant_flagged():
    # GRANTED to m2 while LP-1 is still held by m1 -> NoDoubleGrant violation.
    recs = [
        _rec(1, "GRANTED", "LP-1", "m1", None, "m1"),
        _rec(2, "GRANTED", "LP-1", "m2", "m1", "m2"),
    ]
    res = check_records(recs)
    assert not res.conforming
    assert len(res.violations) == 1
    assert "NoDoubleGrant" in res.violations[0]["reason"]


def test_handover_by_non_holder_flagged():
    # HANDOVER whose sender (prior) is not the current holder.
    recs = [
        _rec(1, "GRANTED", "LP-1", "m1", None, "m1"),
        _rec(2, "HANDOVER", "LP-1", "m3", "m2", "m3"),   # sender m2 != holder m1
    ]
    res = check_records(recs)
    assert not res.conforming
    assert any("not the current holder" in v["reason"] for v in res.violations)


def test_spurious_denial_flagged():
    # DENIED on a free resource (should have been granted).
    recs = [_rec(1, "DENIED", "LP-1", "m1", None, None)]
    res = check_records(recs)
    assert not res.conforming
    assert any("spurious denial" in v["reason"] for v in res.violations)


def test_online_via_on_transition_conforms():
    mon = ReservationConformanceMonitor()
    rm = ReservationManager(on_transition=mon.feed)
    rm.request(site_agent_pb2.ReservationRequest(
        reservation_id="a", resource_id="LP-1", holder_id="m1"), now=1.0)
    rm.request(site_agent_pb2.ReservationRequest(
        reservation_id="b", resource_id="LP-1", holder_id="m2"), now=2.0)   # DENIED (held)
    rm.release(site_agent_pb2.ReservationRelease(reservation_id="a", reason="done"), now=3.0)
    rm.request(site_agent_pb2.ReservationRequest(
        reservation_id="c", resource_id="LP-1", holder_id="m2"), now=4.0)   # now GRANTED
    assert mon.result.conforming
    assert mon.result.transitions >= 4
    assert mon.result.event_counts.get("DENIED", 0) >= 1
    assert mon.result.event_counts.get("GRANTED", 0) >= 2
