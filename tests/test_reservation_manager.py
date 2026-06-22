"""Tests for ReservationManager — transcribing the CAP conformance Level 2 (Ch.6 §6.3.3) semantics.

Verifies grant / deny(conflict) / release / same-holder idempotent / expiry. No network, pure logic.
"""

from __future__ import annotations

from cap.v0.core import events_pb2, site_agent_pb2

from cap_coordination_kit import ReservationManager

_State = events_pb2.ReservationStatus.ReservationState


def _req(reservation_id: str, resource_id: str, holder_id: str, *, latest_epoch: int | None = None):
    r = site_agent_pb2.ReservationRequest(
        reservation_id=reservation_id, resource_id=resource_id, holder_id=holder_id)
    if latest_epoch is not None:
        r.requested_window.latest.seconds = latest_epoch
    return r


def test_grant_when_resource_free():
    m = ReservationManager()
    st = m.request(_req("res-1", "loading", "mst110cr"))
    assert st.state == _State.RESERVATION_STATE_GRANTED
    assert st.holder_id == "mst110cr" and st.resource_id == "loading"
    assert m.holder_of("loading") == "mst110cr"


def test_conflict_second_holder_denied():
    m = ReservationManager()
    assert m.request(_req("r1", "loading", "mst110cr")).state == _State.RESERVATION_STATE_GRANTED
    st2 = m.request(_req("r2", "loading", "zx200"))
    assert st2.state == _State.RESERVATION_STATE_DENIED
    assert "mst110cr" in st2.reason
    assert m.holder_of("loading") == "mst110cr"  # original holder is retained


def test_release_frees_resource_for_other():
    m = ReservationManager()
    m.request(_req("r1", "loading", "mst110cr"))
    rel = m.release(site_agent_pb2.ReservationRelease(reservation_id="r1", reason="done"))
    assert rel.state == _State.RESERVATION_STATE_RELEASED
    assert m.holder_of("loading") is None
    # after release another machine can acquire it
    assert m.request(_req("r2", "loading", "zx200")).state == _State.RESERVATION_STATE_GRANTED
    assert m.holder_of("loading") == "zx200"


def test_same_holder_rerequest_is_idempotent_grant():
    m = ReservationManager()
    assert m.request(_req("r1", "loading", "mst110cr")).state == _State.RESERVATION_STATE_GRANTED
    # a re-request by the same holder is GRANTED (not treated as contention)
    assert m.request(_req("r1b", "loading", "mst110cr")).state == _State.RESERVATION_STATE_GRANTED
    assert m.holder_of("loading") == "mst110cr"


def test_release_unknown_is_idempotent():
    m = ReservationManager()
    rel = m.release(site_agent_pb2.ReservationRelease(reservation_id="nope", reason="x"))
    assert rel.state == _State.RESERVATION_STATE_RELEASED  # idempotent


def test_expiry_frees_resource_for_regrant():
    m = ReservationManager()
    # window.latest = epoch 1000; at now=1001 it expires → another machine can acquire it.
    assert m.request(_req("r1", "loading", "mst110cr", latest_epoch=1000)).state == _State.RESERVATION_STATE_GRANTED
    assert m.holder_of("loading", now=999.0) == "mst110cr"   # held before expiry
    assert m.holder_of("loading", now=1001.0) is None        # freed after expiry
    st2 = m.request(_req("r2", "loading", "zx200"), now=1001.0)
    assert st2.state == _State.RESERVATION_STATE_GRANTED
    assert m.holder_of("loading") == "zx200"


def test_active_lists_current_holders():
    m = ReservationManager()
    m.request(_req("r1", "loading", "mst110cr"))
    m.request(_req("r2", "dig-A", "zx200"))
    active = {v.resource_id: v.holder_id for v in m.active()}
    assert active == {"loading": "mst110cr", "dig-A": "zx200"}


# ── P2 witness: the authoritative transaction log (emitted from inside the arbiter) ──
def test_txlog_records_every_transition_with_resolved_holder():
    """request/release are recorded with seq + resolved/prior holder (the canonical P2 witness)."""
    recs: list[dict] = []
    m = ReservationManager(on_transition=recs.append)   # also stream to a sink
    m.request(_req("r1", "loading", "mst110cr"))                       # GRANTED (free)
    m.request(_req("r2", "loading", "zx200"))                          # DENIED (held by peer)
    m.release(site_agent_pb2.ReservationRelease(reservation_id="r1"))  # RELEASED
    m.request(_req("r3", "loading", "zx200"))                          # GRANTED (now free)
    events = [(r["event"], r["requested_holder"], r["prior_holder"], r["resolved_holder"]) for r in m.txlog()]
    assert events == [
        ("GRANTED", "mst110cr", None, "mst110cr"),
        ("DENIED", "zx200", "mst110cr", "mst110cr"),
        ("RELEASED", "mst110cr", "mst110cr", None),
        ("GRANTED", "zx200", None, "zx200"),
    ]
    assert [r["seq"] for r in m.txlog()] == [1, 2, 3, 4]   # monotone seq = total order
    assert recs == m.txlog()                               # the on_transition sink receives the same log


def test_audit_double_grants_empty_under_contention():
    """Under contention the arbiter preserves single-holder exclusivity → audit_double_grants is
    empty (the canonical record that P2 holds).

    An apparent double_grant was an instrumentation artifact of mixing the skill-layer and
    wire-layer log streams; in the RM's authoritative log there is not one GRANTED while another
    holder is active.
    """
    m = ReservationManager()
    m.request(_req("a1", "loading", "mst110cr"))   # mst holds
    for i in range(20):                            # c30r hammers while mst holds → all DENIED
        assert m.request(_req(f"c{i}", "loading", "c30r")).state == _State.RESERVATION_STATE_DENIED
    m.release(site_agent_pb2.ReservationRelease(reservation_id="a1"))
    m.request(_req("c-final", "loading", "c30r"))  # now granted (handoff)
    assert m.audit_double_grants() == []           # never two distinct holders concurrently
    granted = [r for r in m.txlog() if r["event"] == "GRANTED"]
    denied = [r for r in m.txlog() if r["event"] == "DENIED"]
    assert [r["requested_holder"] for r in granted] == ["mst110cr", "c30r"]
    assert len(denied) == 20                       # every contended request recorded, not undercounted


def test_transfer_grant_is_atomic_no_free_window():
    """transfer_grant (TLA BeginHandoverAtomic) retargets the holder in ONE step:
    holder goes sender->receiver with no intervening RELEASED (no free window) and no
    double-hold; the transition is recorded as HANDOVER and audit_double_grants stays empty."""
    m = ReservationManager()
    m.request(_req("zx-r1", "load:p1", "zx200"), now=1.0)        # sender holds
    assert m.request(_req("mst-r1", "load:p1", "mst110cr"), now=2.0).state \
        == _State.RESERVATION_STATE_DENIED                      # receiver queued (DENIED while held)
    st = m.transfer_grant("load:p1", "mst110cr", "mst-ho1", now=3.0)
    assert st is not None and st.state == _State.RESERVATION_STATE_GRANTED
    assert m.holder_of("load:p1") == "mst110cr"                 # receiver now holds
    assert m.audit_double_grants() == []                        # HANDOVER is not a double-grant
    tx = m.txlog()
    ho = [r for r in tx if r["event"] == "HANDOVER" and r["resource_id"] == "load:p1"]
    assert len(ho) == 1 and ho[0]["prior_holder"] == "zx200" and ho[0]["resolved_holder"] == "mst110cr"
    # the atomicity witness: NO RELEASED of the resource precedes the transfer (no free window)
    assert [r for r in tx if r["event"] == "RELEASED" and r["resource_id"] == "load:p1"] == []


def test_transfer_grant_returns_none_when_unheld():
    """begin_atomic precondition: there must be a current holder to hand over from."""
    m = ReservationManager()
    assert m.transfer_grant("load:p1", "mst110cr", "mst-ho1", now=1.0) is None


def test_pending_for_tracks_denied_waiters_fifo():
    """pending_for exposes DENIED-while-held waiters (FIFO, deduped by holder, current holder
    excluded) so the coordinator can route a release as a typed Handover to the next waiter."""
    m = ReservationManager()
    m.request(_req("zx-r", "load:p1", "zx200"), now=1.0)               # sender holds
    assert m.pending_for("load:p1") == []
    m.request(_req("mst-q", "load:p1", "mst110cr"), now=2.0)           # DENIED -> queued
    m.request(_req("c30-q", "load:p1", "c30r"), now=3.0)               # DENIED -> queued (FIFO)
    m.request(_req("mst-q2", "load:p1", "mst110cr"), now=4.0)          # re-deny: dedup, keep latest rid
    assert m.pending_for("load:p1") == [("mst110cr", "mst-q2"), ("c30r", "c30-q")]
    # atomic handover to the first waiter clears it from the queue
    m.transfer_grant("load:p1", "mst110cr", "mst-ho", now=5.0)
    assert m.holder_of("load:p1") == "mst110cr"
    assert m.pending_for("load:p1") == [("c30r", "c30-q")]            # mst cleared, current holder excluded


def test_naive_handover_opens_a_free_window():
    """Contrast: the naive release-then-reacquire path (what the runtime did before the typed
    Handover) leaves the resource momentarily unheld -- the RELEASED->GRANTED gap a third
    machine could exploit. This is the free window transfer_grant eliminates."""
    m = ReservationManager()
    m.request(_req("zx-r1", "load:p1", "zx200"), now=1.0)
    m.release(site_agent_pb2.ReservationRelease(reservation_id="zx-r1"), now=2.0)
    assert m.holder_of("load:p1") is None                       # FREE WINDOW: nobody holds load:p1
    m.request(_req("mst-r1", "load:p1", "mst110cr"), now=3.0)
    tx = m.txlog()
    seq = [r["event"] for r in tx if r["resource_id"] == "load:p1"]
    assert seq == ["GRANTED", "RELEASED", "GRANTED"]            # the gap the atomic path removes
