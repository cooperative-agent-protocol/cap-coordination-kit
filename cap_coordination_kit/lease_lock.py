"""Lease+TTL lock vs CAP deny-on-busy: the live-holder lease-steal hazard.

A realistic alternative to CAP's reservation arbiter is a lease + time-to-live
lock (Chubby / ZooKeeper style) -- the default a practitioner reaches for. This
module provides that lock and a deterministic experiment isolating the arbiter
(no kinematics, no LLM), showing the hazard such a lock admits and CAP does not:

  When a holder keeps a resource longer than its lease TTL while a contender is
  waiting, the lease lock RECLAIMS the lock and re-grants it to the contender
  *even though the original holder never released and is still acting* -- a
  "lease steal" of a live holder's lock. CAP's deny-on-busy arbiter never times
  out a live holder, so it records zero steals.

  The steal is INVISIBLE to a no-double-grant audit (the reclaim frees the slot
  first, so the re-grant has no prior holder); that is exactly why CAP carries a
  monotonic grant epoch (fencing), so a stale holder whose lease was reassigned
  cannot still drive the machine.

Run:  python3 -m cap_coordination_kit.lease_lock
"""
from __future__ import annotations

import time as _time
from typing import Any, Callable

from cap.v0.core import events_pb2, site_agent_pb2

from .reservation import ReservationManager

_G = events_pb2.ReservationStatus.RESERVATION_STATE_GRANTED
RES = "loading:excavation-1:zx200"
HOLD = [4, 6, 8, 10, 12]      # ticks the holder keeps the resource before releasing
TTL = [2, 4, 6, 8, 10]        # lease time-to-live (ticks)


class LeaseReservationManager(ReservationManager):
    """Lease+TTL lock: a grant carries a fixed TTL and is reclaimed when it
    elapses, possibly re-granting a live holder's lock to a contender (a lease
    steal). Records each steal. Clock is injectable for deterministic runs."""

    def __init__(self, ttl_s: float = 5.0, clock: Callable[[], float] | None = None) -> None:
        super().__init__()
        self._ttl = float(ttl_s)
        self._clock = clock if clock is not None else _time.time
        self._granted_wall: dict[str, float] = {}
        self._released_rids: set[str] = set()
        self.lease_steals: list[dict[str, Any]] = []
        self.lease_expiries: list[dict[str, Any]] = []

    def _reclaim_expired(self, resource_id: str, now_w: float) -> str | None:
        cur = self._holder.get(resource_id)
        if cur is None or cur not in self._by_id:
            return None
        gw = self._granted_wall.get(resource_id)
        if gw is None or (now_w - gw) <= self._ttl:
            return None
        victim = self._by_id[cur].holder_id
        live = cur not in self._released_rids
        self._holder.pop(resource_id, None)
        self._by_id.pop(cur, None)
        self._granted_wall.pop(resource_id, None)
        self._emit("EXPIRED", resource_id, cur, victim, victim, now=now_w,
                   reason=f"lease ttl {self._ttl}s elapsed (holder live={live})")
        self.lease_expiries.append({"resource_id": resource_id, "holder": victim, "live": live})
        return victim if live else None

    def request(self, req: site_agent_pb2.ReservationRequest, *,
                now: float | None = None) -> events_pb2.ReservationStatus:
        now_w = self._clock()
        victim = self._reclaim_expired(req.resource_id, now_w)
        st = super().request(req, now=now_w)
        if st.state == _G:
            if victim is not None and req.holder_id != victim:
                self.lease_steals.append({"resource_id": req.resource_id,
                                          "victim": victim, "thief": req.holder_id})
            self._granted_wall[req.resource_id] = now_w
        return st

    def release(self, rel: site_agent_pb2.ReservationRelease, *,
                now: float | None = None) -> events_pb2.ReservationStatus:
        self._released_rids.add(rel.reservation_id)
        v = self._by_id.get(rel.reservation_id)
        if v is not None:
            self._granted_wall.pop(v.resource_id, None)
        return super().release(rel, now=now if now is not None else self._clock())


def _req(rid: str, holder: str) -> site_agent_pb2.ReservationRequest:
    return site_agent_pb2.ReservationRequest(reservation_id=rid, resource_id=RES, holder_id=holder)


def _run(make_mgr, hold: int) -> dict:
    vt = [0.0]
    mgr = make_mgr(vt)
    mgr.request(_req("A-res", "A"), now=vt[0])
    for t in range(1, hold + 1):
        vt[0] = float(t)
        mgr.request(_req(f"B-{t}", "B"), now=vt[0])
    vt[0] = float(hold)
    mgr.release(site_agent_pb2.ReservationRelease(reservation_id="A-res"), now=vt[0])
    return {"steals": len(getattr(mgr, "lease_steals", [])),
            "double_grants_audited": len(mgr.audit_double_grants())}


def build_grid() -> dict:
    def lease(ttl):
        return lambda vt: LeaseReservationManager(ttl_s=ttl, clock=lambda: vt[0])

    def cap(_ttl):
        return lambda vt: ReservationManager()

    grid = {}
    lease_total = cap_total = lease_audit = 0
    for h in HOLD:
        for t in TTL:
            lr, cr = _run(lease(t), h), _run(cap(t), h)
            grid[(h, t)] = {"lease_steals": lr["steals"], "cap_steals": cr["steals"]}
            lease_total += lr["steals"]
            cap_total += cr["steals"]
            lease_audit += lr["double_grants_audited"]
    return {"grid": grid, "lease_steals_total": lease_total, "cap_steals_total": cap_total,
            "lease_double_grants_audited_total": lease_audit, "cells": len(grid)}


def main() -> int:
    res = build_grid()
    print("Lease-lock live-holder steals (1 = a live holder's lock was reassigned):")
    print("  hold\\ttl  " + "  ".join(f"{t:>2d}" for t in TTL))
    for h in HOLD:
        cells = "   ".join(str(res["grid"][(h, t)]["lease_steals"]) for t in TTL)
        print(f"  {h:>7d}    {cells}")
    print(f"  lease steals total={res['lease_steals_total']}/{res['cells']} cells; "
          f"CAP deny-on-busy steals={res['cap_steals_total']}; "
          f"no-double-grant audit flags={res['lease_double_grants_audited_total']} "
          f"(steal invisible to the audit -> motivates the grant-epoch fence).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
