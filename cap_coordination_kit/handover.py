"""HandoverManager — the machine-to-machine resource Handover sub-protocol (Ch.6 §6.4).

**Drives at runtime** the Handover sub-protocol of `CAPCoordination.tla`. The typed Handover
primitive (P4 = HandoverReceiverOwnership) is verified in TLA+, but the conventional runtime
realised load-slot turnover as "holder releases → next machine reserves" (= a naive
release-then-reacquire), so the verified atomic-transfer primitive had never actually fired. This
class runs it for real on the authoritative ReservationManager, switching between the atomic
transfer and the naive path under one and the same state machine so the two can be contrasted.

State (hoState[r]):  NONE → REQUESTED → ACKNOWLEDGED → IN_PROGRESS → COMPLETED →(cleanup)→ NONE
Receiver (hoReceiver[r]): the receiving machine's holder_id.

  atomic=True  : begin() re-points the holder from sender→receiver in a **single step** via
                 ReservationManager.transfer_grant (TLA `BeginHandoverAtomic`). Throughout
                 IN_PROGRESS, holder=receiver = P4 (ReceiverOwnership) holds, with no free window.
  atomic=False : begin() releases the sender so holder=NONE (a free window opens; TLA
                 `BeginHandoverNaive`), after which grant_to_receiver lets the receiver reacquire
                 (TLA `GrantToReceiver`). At the start of IN_PROGRESS, holder≠receiver — a P4
                 violation is demonstrated.

Pure logic (no dependency beyond the protos and ReservationManager). The caller passes `now`.
Each transition is recorded with a holder snapshot, so the HandoverOracle can verify
ReceiverOwnership directly from the ho transition log rather than from the txlog alone.
"""

from __future__ import annotations

from typing import Any, Callable

from cap.v0.core import site_agent_pb2

from .reservation import ReservationManager

NONE, REQUESTED, ACKNOWLEDGED, IN_PROGRESS, COMPLETED = (
    "NONE", "REQUESTED", "ACKNOWLEDGED", "IN_PROGRESS", "COMPLETED")
HANDOVER_STATES = {NONE, REQUESTED, ACKNOWLEDGED, IN_PROGRESS, COMPLETED}


class HandoverManager:
    """Runtime driver of the machine-to-machine Handover sub-protocol (co-located with the authoritative ReservationManager)."""

    def __init__(self, rm: ReservationManager, *, atomic: bool = True,
                 on_transition: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._rm = rm
        self._atomic = atomic
        self._ho_state: dict[str, str] = {}            # resource_id -> HANDOVER_STATES
        self._ho_receiver: dict[str, str | None] = {}  # resource_id -> receiver holder_id
        self._seq = 0
        self._log: list[dict[str, Any]] = []
        self._on_transition = on_transition

    # ── internal ───────────────────────────────────────────────────────
    def state_of(self, resource_id: str) -> str:
        return self._ho_state.get(resource_id, NONE)

    def receiver_of(self, resource_id: str) -> str | None:
        return self._ho_receiver.get(resource_id)

    def _record(self, action: str, resource_id: str, *, now: float | None,
                sender: str | None = None, receiver: str | None = None) -> None:
        """Record one transition with a holder snapshot. holder_after = the holder the
        ReservationManager records immediately after the transition (the canonical record for
        verifying ReceiverOwnership)."""
        self._seq += 1
        rec = {
            "seq": self._seq, "t_wall": now, "action": action, "resource_id": resource_id,
            "ho_state": self.state_of(resource_id), "receiver": self._ho_receiver.get(resource_id),
            "holder_after": self._rm.holder_of(resource_id, now=now),
            "atomic": self._atomic, "sender": sender,
        }
        self._log.append(rec)
        if self._on_transition is not None:
            try:
                self._on_transition(rec)
            except Exception:  # noqa: BLE001 - witness logging must never break coordination
                pass

    # ── sub-protocol (1:1 with the TLA actions) ─────────────────────────
    def initiate(self, sender: str, resource_id: str, receiver: str, *,
                 now: float | None = None) -> bool:
        """TLA `InitiateHandover(s, r, rcv)`. The holder (sender) begins a transfer to the
        (waiting) receiver. Precondition: hoState[r]=NONE and the resource is held by sender."""
        if self.state_of(resource_id) != NONE:
            return False
        if self._rm.holder_of(resource_id, now=now) != sender:
            return False
        self._ho_state[resource_id] = REQUESTED
        self._ho_receiver[resource_id] = receiver
        self._record("INITIATE", resource_id, now=now, sender=sender, receiver=receiver)
        return True

    def ack(self, resource_id: str, *, now: float | None = None) -> bool:
        """TLA `AckHandover(r)`: REQUESTED → ACKNOWLEDGED."""
        if self.state_of(resource_id) != REQUESTED:
            return False
        self._ho_state[resource_id] = ACKNOWLEDGED
        self._record("ACK", resource_id, now=now)
        return True

    def begin(self, resource_id: str, *, receiver_reservation_id: str,
              now: float | None = None) -> bool:
        """ACKNOWLEDGED → IN_PROGRESS. atomic → TLA `BeginHandoverAtomic` (single-step transfer);
        naive → TLA `BeginHandoverNaive` (release the sender, opening a free window)."""
        if self.state_of(resource_id) != ACKNOWLEDGED:
            return False
        receiver = self._ho_receiver.get(resource_id)
        if receiver is None:
            return False
        if self._atomic:
            st = self._rm.transfer_grant(resource_id, receiver, receiver_reservation_id, now=now)
            if st is None:
                return False
        else:
            # naive: release the holder's grant → holder=NONE (a free window opens).
            sender_rid = self._rm.reservation_id_of(resource_id, now=now)
            if sender_rid is None:
                return False
            self._rm.release(site_agent_pb2.ReservationRelease(
                reservation_id=sender_rid, reason="naive handover release"), now=now)
        self._ho_state[resource_id] = IN_PROGRESS
        self._record("BEGIN", resource_id, now=now, receiver=receiver)
        return True

    def grant_to_receiver(self, resource_id: str, *, receiver_reservation_id: str,
                          now: float | None = None) -> bool:
        """TLA `GrantToReceiver(r)` (naive only): the receiver reacquires the resource during the
        free window. If a third party grabbed it first via GrantResource (holder≠NONE) this does
        not fire = the P4-violation path."""
        if self._atomic or self.state_of(resource_id) != IN_PROGRESS:
            return False
        receiver = self._ho_receiver.get(resource_id)
        if receiver is None or self._rm.holder_of(resource_id, now=now) is not None:
            return False
        self._rm.request(site_agent_pb2.ReservationRequest(
            reservation_id=receiver_reservation_id, resource_id=resource_id, holder_id=receiver),
            now=now)
        self._record("GRANT_TO_RECEIVER", resource_id, now=now, receiver=receiver)
        return True

    def complete(self, resource_id: str, *, now: float | None = None) -> bool:
        """TLA `CompleteHandover(r)`: IN_PROGRESS → COMPLETED. Precondition: holder=receiver."""
        if self.state_of(resource_id) != IN_PROGRESS:
            return False
        receiver = self._ho_receiver.get(resource_id)
        if self._rm.holder_of(resource_id, now=now) != receiver:
            return False
        self._ho_state[resource_id] = COMPLETED
        self._record("COMPLETE", resource_id, now=now, receiver=receiver)
        return True

    def cleanup(self, resource_id: str, *, now: float | None = None) -> bool:
        """TLA `CleanupHandover(r)`: COMPLETED → NONE, hoReceiver → NONE."""
        if self.state_of(resource_id) != COMPLETED:
            return False
        self._ho_state[resource_id] = NONE
        self._ho_receiver[resource_id] = None
        self._record("CLEANUP", resource_id, now=now)
        return True

    # ── witness ─────────────────────────────────────────────────────────
    def log(self) -> list[dict[str, Any]]:
        """A copy of the ho transition log (with holder snapshots)."""
        return list(self._log)
