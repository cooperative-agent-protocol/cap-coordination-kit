"""ReservationManager — the shared-resource reservation authority (Site side).

Services `ReservationRequest`/`ReservationRelease` and returns a `ReservationStatus`.
A single resource (resource_id = zone id / load point / route segment) is GRANTED to at
most one holder at a time; a request while another holder is active is DENIED (contention).
A resource is freed by the holder's release or by window expiry.

Implements the conformance Level 2 (Ch.6 §6.3.3) state transitions:
  REQUESTED → GRANTED   (resource free, or same holder re-requesting)
  REQUESTED → DENIED    (another holder is active = contention)
  GRANTED   → RELEASED  (holder's ReservationRelease)
  GRANTED   → EXPIRED   (past granted_window.latest, evaluated lazily)

Pure logic (no dependency beyond the generated protos), so the conformance semantics can be
exercised directly in unit tests. The caller passes `now` (epoch seconds) for testability;
if omitted, expiry is not evaluated.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cap.v0.core import events_pb2, site_agent_pb2

_S = events_pb2.ReservationStatus
_State = _S.ReservationState


@dataclass(frozen=True)
class ReservationView:
    """Read-only view of an active reservation (for canvas / queries)."""

    reservation_id: str
    resource_id: str
    holder_id: str
    expires_at: float | None


def _ts_to_epoch(ts: Any) -> float | None:
    """protobuf Timestamp → epoch seconds. Unset (seconds=nanos=0) maps to None (= no expiry)."""
    if ts is None:
        return None
    sec = int(getattr(ts, "seconds", 0) or 0)
    nanos = int(getattr(ts, "nanos", 0) or 0)
    if sec == 0 and nanos == 0:
        return None
    return sec + nanos * 1e-9


class ReservationManager:
    """The shared-resource reservation authority. Owned by the Site servicer; arbitrates requests from machines."""

    def __init__(self, on_transition: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._by_id: dict[str, ReservationView] = {}       # reservation_id -> view
        self._holder: dict[str, str] = {}                  # resource_id -> reservation_id (the active GRANT)
        # Authoritative transaction log (the canonical P2 witness). Every request/release/expiry is
        # recorded from inside the arbiter with a monotone seq and the resolved holder. Calls from
        # either client layer -- the skill layer (in-process coord.reserve) and the wire layer (a
        # machine's cooperative_cycle reserving over gRPC) -- pass through this single RM, so this one
        # log lets us check for double grants directly (this, not a skill-side logger.info, is the
        # canonical P2 record). on_transition is an optional sink (e.g. site streaming to JSONL).
        self._seq = 0
        self._txlog: list[dict[str, Any]] = []
        self._on_transition = on_transition
        # resource_id -> FIFO of (holder_id, reservation_id) currently DENIED-while-held
        # (deduped by holder; cleared when that holder is granted). Lets the coordinator see
        # who is waiting so a release can be routed as a typed Handover to the next waiter.
        self._pending: dict[str, list[tuple[str, str]]] = {}

    # ── internal ───────────────────────────────────────────────────────
    def _expire_due(self, now: float | None) -> list[ReservationView]:
        if now is None:
            return []
        expired: list[ReservationView] = []
        for resource_id, rid in list(self._holder.items()):
            v = self._by_id.get(rid)
            if v is not None and v.expires_at is not None and v.expires_at <= now:
                expired.append(v)
                self._holder.pop(resource_id, None)
                self._by_id.pop(rid, None)
                self._emit("EXPIRED", resource_id, rid, v.holder_id, v.holder_id,
                           now=now, expires_at=v.expires_at)
        return expired

    @staticmethod
    def _status(reservation_id: str, resource_id: str, holder_id: str,
                state: int, *, reason: str = "", granted_window: Any = None) -> events_pb2.ReservationStatus:
        st = _S(reservation_id=reservation_id, resource_id=resource_id,
                holder_id=holder_id, state=state, reason=reason)
        if granted_window is not None:
            st.granted_window.CopyFrom(granted_window)
        return st

    def _emit(self, event: str, resource_id: str, reservation_id: str,
              requested_holder: str, prior_holder: str | None, *,
              now: float | None = None, expires_at: float | None = None,
              reason: str = "") -> None:
        """Record one transition to the authoritative log from inside the arbiter (the P2 witness).

        resolved_holder = the holder the RM actually records *after* the transition. Since the RM
        holds at most one holder per resource (a request against an active holder is DENIED), this
        field is the canonical record of single-holder exclusivity. prior_holder = the holder
        *before* the transition (used by the double-grant audit).
        """
        self._seq += 1
        cur = self._holder.get(resource_id)
        resolved = self._by_id[cur].holder_id if (cur and cur in self._by_id) else None
        rec = {
            "seq": self._seq, "t_wall": now, "event": event,
            "resource_id": resource_id, "reservation_id": reservation_id,
            "requested_holder": requested_holder, "prior_holder": prior_holder,
            "resolved_holder": resolved, "expires_at": expires_at, "reason": reason,
        }
        self._txlog.append(rec)
        if self._on_transition is not None:
            try:
                self._on_transition(rec)
            except Exception:  # noqa: BLE001 - witness logging must never break arbitration
                pass

    # ── public API ─────────────────────────────────────────────────────
    def request(self, req: site_agent_pb2.ReservationRequest, *,
                now: float | None = None) -> events_pb2.ReservationStatus:
        """Arbitrate a reservation request. Free / same holder → GRANTED; another holder active → DENIED."""
        self._expire_due(now)
        rid, resource_id, holder_id = req.reservation_id, req.resource_id, req.holder_id
        cur = self._holder.get(resource_id)
        prior_holder = self._by_id[cur].holder_id if (cur is not None and cur in self._by_id) else None
        if prior_holder is not None and prior_holder != holder_id:
            self._emit("DENIED", resource_id, rid, holder_id, prior_holder,
                       now=now, reason=f"held by '{prior_holder}'")
            # remember this waiter (FIFO by FIRST denial; a retry updates its reservation_id
            # in place so a repeatedly-denied waiter keeps its queue position, not moved to back)
            q = self._pending.setdefault(resource_id, [])
            for i, (h, _r) in enumerate(q):
                if h == holder_id:
                    q[i] = (holder_id, rid)
                    break
            else:
                q.append((holder_id, rid))
            return self._status(
                rid, resource_id, holder_id, _State.RESERVATION_STATE_DENIED,
                reason=f"resource '{resource_id}' held by '{prior_holder}'",
            )
        # free, or same holder re-requesting (idempotent) → GRANTED
        view = ReservationView(reservation_id=rid, resource_id=resource_id, holder_id=holder_id,
                               expires_at=_ts_to_epoch(req.requested_window.latest)
                               if req.HasField("requested_window") else None)
        # if the same holder re-requests under a different reservation_id, fold the old grant
        if cur is not None and cur != rid:
            self._by_id.pop(cur, None)
        self._by_id[rid] = view
        self._holder[resource_id] = rid
        self._clear_pending(resource_id, holder_id)
        self._emit("GRANTED", resource_id, rid, holder_id, prior_holder,
                   now=now, expires_at=view.expires_at)
        return self._status(rid, resource_id, holder_id, _State.RESERVATION_STATE_GRANTED,
                            granted_window=req.requested_window if req.HasField("requested_window") else None)

    def release(self, rel: site_agent_pb2.ReservationRelease, *,
                now: float | None = None) -> events_pb2.ReservationStatus:
        """Release a reservation. Returns RELEASED even for an unknown id (idempotent)."""
        v = self._by_id.pop(rel.reservation_id, None)
        if v is None:
            self._emit("RELEASED", "", rel.reservation_id, "", None, now=now,
                       reason=rel.reason or "unknown reservation (idempotent release)")
            return self._status(rel.reservation_id, "", "", _State.RESERVATION_STATE_RELEASED,
                                reason=rel.reason or "unknown reservation (idempotent release)")
        if self._holder.get(v.resource_id) == rel.reservation_id:
            self._holder.pop(v.resource_id, None)
        self._emit("RELEASED", v.resource_id, rel.reservation_id, v.holder_id, v.holder_id,
                   now=now, reason=rel.reason)
        return self._status(v.reservation_id, v.resource_id, v.holder_id,
                            _State.RESERVATION_STATE_RELEASED, reason=rel.reason)

    def transfer_grant(self, resource_id: str, receiver_id: str, receiver_reservation_id: str, *,
                       now: float | None = None, expires_at: float | None = None,
                       ) -> events_pb2.ReservationStatus | None:
        """The authoritative single step of a machine-to-machine Handover (the resource effect of
        TLA `BeginHandoverAtomic`).

        Re-points the holder of resource_id from the current holder (sender) to receiver_id in a
        **single step**: ``self._holder[resource_id]`` moves directly from old_rid to new_rid,
        never passing through unset (a free window) or a double hold. This is the sole point that
        separates an atomic ownership transfer from a naive release-then-reacquire. It is recorded
        on the authoritative log as a ``HANDOVER`` transition (prior_holder=sender,
        resolved_holder=receiver). ``audit_double_grants`` scans only ``GRANTED`` transitions, so
        this legitimate transfer is not flagged (free-window / double-hold checking is done
        separately by `audit_handover`).

        Returns None if the resource is held by no one (the caller must satisfy begin_atomic's
        precondition).
        """
        self._expire_due(now)
        cur = self._holder.get(resource_id)
        sender = self._by_id[cur].holder_id if (cur is not None and cur in self._by_id) else None
        if sender is None:
            return None
        view = ReservationView(reservation_id=receiver_reservation_id, resource_id=resource_id,
                               holder_id=receiver_id, expires_at=expires_at)
        # Single-step re-point: make receiver the holder and fold the sender's grant in the same
        # operation. _holder[resource_id] never goes empty between these two assignments (= no free window).
        self._by_id[receiver_reservation_id] = view
        self._holder[resource_id] = receiver_reservation_id
        if cur is not None and cur != receiver_reservation_id:
            self._by_id.pop(cur, None)
        self._clear_pending(resource_id, receiver_id)
        self._emit("HANDOVER", resource_id, receiver_reservation_id, receiver_id, sender,
                   now=now, expires_at=expires_at)
        return self._status(receiver_reservation_id, resource_id, receiver_id,
                            _State.RESERVATION_STATE_GRANTED)

    def holder_of(self, resource_id: str, *, now: float | None = None) -> str | None:
        """The resource's current holder holder_id (None if unheld)."""
        self._expire_due(now)
        rid = self._holder.get(resource_id)
        v = self._by_id.get(rid) if rid else None
        return v.holder_id if v else None

    def reservation_id_of(self, resource_id: str, *, now: float | None = None) -> str | None:
        """The resource's current holding reservation_id (None if unheld). Used by naive handover
        to release the holder's grant by resource (since release requires a reservation_id)."""
        self._expire_due(now)
        return self._holder.get(resource_id)

    def _clear_pending(self, resource_id: str, holder_id: str) -> None:
        """Remove holder_id from resource_id's waiting queue (on grant/handover completion)."""
        q = self._pending.get(resource_id)
        if q:
            q[:] = [(h, r) for (h, r) in q if h != holder_id]
            if not q:
                self._pending.pop(resource_id, None)

    def pending_for(self, resource_id: str, *, exclude: str | None = None) -> list[tuple[str, str]]:
        """FIFO of (holder_id, reservation_id) waiting on resource_id while DENIED-while-held,
        excluding the current holder (= exclude). Used by the coordinator to route a release as a
        typed Handover to the next waiter (R3)."""
        cur = self.holder_of(resource_id)
        return [(h, r) for (h, r) in self._pending.get(resource_id, [])
                if h != cur and h != exclude]

    def active(self, *, now: float | None = None) -> list[ReservationView]:
        """List of active (GRANTED and not yet expired) reservations (for canvas / queries)."""
        self._expire_due(now)
        return [self._by_id[rid] for rid in self._holder.values() if rid in self._by_id]

    # ── P2 witness audit (direct verification from the authoritative transaction log) ──
    def txlog(self) -> list[dict[str, Any]]:
        """A copy of the authoritative transaction log (the canonical P2 witness)."""
        return list(self._txlog)

    def audit_double_grants(self) -> list[dict[str, Any]]:
        """Detect double grants (P2 violations) — returns transitions that were GRANTED while
        another holder was active.

        Always empty for a correct arbiter (a request against an active holder is always DENIED).
        Reservations from both the skill and wire client layers pass through the single RM, so this
        one log suffices to verify P2 (it lets the canonical side definitively rule out an apparent
        double grant that was in fact an instrumentation artifact of mixing two log streams).
        """
        return [r for r in self._txlog
                if r["event"] == "GRANTED" and r["prior_holder"] not in (None, r["requested_holder"])]
