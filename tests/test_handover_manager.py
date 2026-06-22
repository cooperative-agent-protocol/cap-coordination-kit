"""HandoverManager のテスト — 機体間 Handover サブプロトコル (Ch06 §6.4) の実行時駆動を写経。

CAPCoordination.tla の Handover actions に 1:1 対応する遷移と、P4 (HandoverReceiverOwnership):
IN_PROGRESS 中つねに holder=receiver、を atomic / naive の対比で検証する。ネット不要・純ロジック。
"""

from __future__ import annotations

from cap.v0.core import events_pb2, site_agent_pb2

from cap_coordination_kit import HandoverManager, ReservationManager

_State = events_pb2.ReservationStatus.ReservationState


def _req(reservation_id: str, resource_id: str, holder_id: str):
    return site_agent_pb2.ReservationRequest(
        reservation_id=reservation_id, resource_id=resource_id, holder_id=holder_id)


def _setup_contended(rm: ReservationManager, resource="load:p1", sender="zx200", receiver="mst110cr"):
    """sender が resource を保持し、receiver は要求して DENIED (待機/キュー) の状態を作る。"""
    rm.request(_req(f"{sender}-r", resource, sender), now=1.0)
    st = rm.request(_req(f"{receiver}-q", resource, receiver), now=2.0)
    assert st.state == _State.RESERVATION_STATE_DENIED       # receiver queued behind the sender
    assert rm.holder_of(resource) == sender
    return resource, sender, receiver


def _ownership_held_through_in_progress(ho: HandoverManager) -> bool:
    """P4 witness: IN_PROGRESS に入った瞬間 (BEGIN) に holder=receiver だったか。
    atomic なら True (単一ステップ授受)、naive なら False (free window で holder=NONE)。"""
    for rec in ho.log():
        if rec["action"] == "BEGIN":
            return rec["holder_after"] == rec["receiver"]
    return False


def test_atomic_handover_full_flow_receiver_ownership_holds():
    rm = ReservationManager()
    resource, sender, receiver = _setup_contended(rm)
    ho = HandoverManager(rm, atomic=True)

    assert ho.initiate(sender, resource, receiver, now=3.0) is True
    assert ho.state_of(resource) == "REQUESTED"
    assert ho.ack(resource, now=3.1) is True
    assert ho.state_of(resource) == "ACKNOWLEDGED"
    assert ho.begin(resource, receiver_reservation_id="mst-ho", now=3.2) is True
    assert ho.state_of(resource) == "IN_PROGRESS"
    # P4: the instant IN_PROGRESS opens, the receiver already owns the resource (no free window)
    assert rm.holder_of(resource) == receiver
    assert _ownership_held_through_in_progress(ho) is True
    assert ho.complete(resource, now=3.3) is True
    assert ho.cleanup(resource, now=3.4) is True
    assert ho.state_of(resource) == "NONE"

    # the typed transitions actually fired, in order
    actions = [r["action"] for r in ho.log()]
    assert actions == ["INITIATE", "ACK", "BEGIN", "COMPLETE", "CLEANUP"]
    # authoritative arbiter: no double-grant; the transfer is recorded as HANDOVER (no RELEASED gap)
    assert rm.audit_double_grants() == []
    tx_events = [r["event"] for r in rm.txlog() if r["resource_id"] == resource]
    assert "HANDOVER" in tx_events and "RELEASED" not in tx_events


def test_naive_handover_opens_free_window_receiver_ownership_violated():
    rm = ReservationManager()
    resource, sender, receiver = _setup_contended(rm)
    ho = HandoverManager(rm, atomic=False)

    assert ho.initiate(sender, resource, receiver, now=3.0) is True
    assert ho.ack(resource, now=3.1) is True
    assert ho.begin(resource, receiver_reservation_id="mst-ho", now=3.2) is True
    assert ho.state_of(resource) == "IN_PROGRESS"
    # P4 VIOLATED: IN_PROGRESS opens with the resource UNHELD (the free window)
    assert rm.holder_of(resource) is None
    assert _ownership_held_through_in_progress(ho) is False
    # the receiver must re-grab the freed resource
    assert ho.grant_to_receiver(resource, receiver_reservation_id="mst-ho", now=3.3) is True
    assert rm.holder_of(resource) == receiver
    assert ho.complete(resource, now=3.4) is True
    assert ho.cleanup(resource, now=3.5) is True

    actions = [r["action"] for r in ho.log()]
    assert actions == ["INITIATE", "ACK", "BEGIN", "GRANT_TO_RECEIVER", "COMPLETE", "CLEANUP"]
    # the naive arm leaves a RELEASED in the authoritative log (the free window the atomic path removes)
    tx_events = [r["event"] for r in rm.txlog() if r["resource_id"] == resource]
    assert "RELEASED" in tx_events


def test_preconditions_reject_out_of_order():
    rm = ReservationManager()
    resource, sender, receiver = _setup_contended(rm)
    ho = HandoverManager(rm, atomic=True)
    # ack before initiate, begin before ack, complete before begin -> all rejected
    assert ho.ack(resource, now=3.0) is False
    assert ho.begin(resource, receiver_reservation_id="x", now=3.0) is False
    assert ho.complete(resource, now=3.0) is False
    # initiate fails if the named sender does not actually hold the resource
    assert ho.initiate("c30r", resource, receiver, now=3.0) is False
    assert ho.initiate(sender, resource, receiver, now=3.0) is True
    assert ho.initiate(sender, resource, receiver, now=3.0) is False  # not NONE anymore
