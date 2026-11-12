"""The same five schemes on a second kind of space: a single-lane stretch of haul road, crossed from both ends.

``scheme_trial`` runs the schemes at a loading bay (a disc around the loader). Here the conflict zone is a
single-lane segment (``zones.SegmentZone``): two carriers park off the road at opposite ends, and each in turn asks
for the segment, drives along it and out at the far end, and releases it; then they come back. The arbiter, the
entry gate, the carriers' drive and the oracles are those of ``scheme_trial`` - only the zone's shape and the
carriers' errands differ - so the run shows the schemes' logic carried to another geometry. Deterministic.

    python -m cap_coordination_kit.segment_trial

Scenarios: nominal; release_early (the holder's client releases while the carrier is on the segment); holder_silent
(the holder's link is cut for 60 s while it is on the segment; it stops, and goes on when the link returns).
S1: both carriers on the segment at once (a head-on meeting on a single lane); S2: a carrier on the segment that the
arbiter accounts neither as holder nor in custody.
"""

from __future__ import annotations

import argparse
import math
import tempfile
from dataclasses import dataclass, field

from cap.v0.core import site_agent_pb2

from cap_coordination_kit.entry_gate import EntryGate
from cap_coordination_kit.occupancy import PERMIT_REASON, OccupancyArbiter, OccupancyJournal, Scheme, VacancyEnvelope
from cap_coordination_kit.scheme_trial import (
    ACCEL,
    DELAY_S,
    DT,
    LEASE_T,
    PERMIT_T,
    SCHEMES,
    V_MAX,
    Carrier,
    Clock,
    TrialResult,
    Unicycle,
    _drive_to,
    _write,
)
from cap_coordination_kit.zones import SegmentZone

SEG = "road:segment-1"
A_END, B_END, HALF_W = (-20.0, 0.0), (20.0, 0.0), 3.0       # the lane's ends; half its width plus clearance
SCENARIOS = ("nominal", "release_early", "holder_silent")


@dataclass
class Errand:
    """A carrier's crossings: the parking spots it drives between and the road points it passes."""
    parks: list[tuple[float, float]]
    legs: list[list[tuple[float, float]]] = field(default_factory=list)
    leg: int = 0
    wp: int = 0


def run_trial(scheme_name: str, scenario: str, *, horizon_s: float = 900.0,
              custody_until_lapse: bool = True) -> TrialResult:
    clock = Clock()
    scheme = Scheme.named(scheme_name, permit_ttl_s=PERMIT_T, lease_ttl_s=LEASE_T,
                          custody_until_lapse=custody_until_lapse)
    zone = SegmentZone.between(SEG, A_END, B_END, HALF_W)
    envelope = VacancyEnvelope(eps_m=0.5, v_max_mps=V_MAX, latency_s=0.2, d_brake_m=V_MAX ** 2 / (2 * ACCEL),
                               max_pose_age_s=1.0)
    arb = OccupancyArbiter(scheme, clock=clock, journal=OccupancyJournal(tempfile.mkdtemp(prefix="cap-seg-"),
                                                                          sync=False), envelope=envelope)
    arb.set_zone(zone)
    res = TrialResult(scheme=scheme_name, scenario=scenario)
    carriers: dict[str, Carrier] = {}
    errands: dict[str, Errand] = {}
    heard: dict[str, float] = {}
    t0 = clock.t
    west_a, east_a, west_b, east_b = (-32.0, 7.0), (32.0, 7.0), (-32.0, -7.0), (32.0, -7.0)
    # a starts in the west and crosses east and back; b starts in the east and crosses west and back
    plans = {"a": ([west_a, east_a, west_a], 0.0), "b": ([east_b, west_b, east_b], math.pi)}

    def legs_of(parks: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
        out = []
        for p, q in zip(parks, parks[1:]):
            near, far = ((-24.0, 0.0), (24.0, 0.0)) if q[0] > p[0] else ((24.0, 0.0), (-24.0, 0.0))
            out.append([near, far, q])
        return out

    def ask_permit(c: Carrier) -> None:
        if not c.link_up:
            return
        t_req = clock.t
        st = arb.request(site_agent_pb2.ReservationRequest(
            reservation_id=f"{c.name}-permit-{int(clock.t * 10)}", resource_id=SEG, holder_id=c.name,
            reason=PERMIT_REASON))
        heard[c.name] = clock.t
        if st.state == 1 and st.reason == PERMIT_REASON:
            c.gate.note_permit(SEG, int(st.grant_epoch), t_req)

    for name, (parks, yaw) in plans.items():
        gate = EntryGate(machine_id=name, mode=scheme.gate, permit_ttl_s=scheme.permit_ttl_s, rho=scheme.rho,
                         delta_entry_s=4.0, link_lease_s=scheme.lease_ttl_s, clock=clock,
                         act_delay_s=DELAY_S, decel_mps2=ACCEL, period_s=2 * DT, resend_s=1.0)
        gate.set_zones([zone])
        c = Carrier(name=name, body=Unicycle(parks[0][0], parks[0][1], yaw), gate=gate, stage=parks[0],
                    load_pose=parks[0])
        gate.request_permit = (lambda _rid, c=c: ask_permit(c))
        carriers[name] = c
        errands[name] = Errand(parks=parks, legs=legs_of(parks))

    def request(c: Carrier) -> bool:
        if not c.link_up or clock.t < c.next_request:
            return False
        c.next_request = clock.t + 1.0
        c.n += 1
        c.rid = f"{c.name}-{c.n}"
        st = arb.request(site_agent_pb2.ReservationRequest(reservation_id=c.rid, resource_id=SEG, holder_id=c.name))
        heard[c.name] = clock.t
        if st.state == 1:
            c.gate.note_grant(SEG, int(st.grant_epoch))
            return True
        return False

    def release(c: Carrier, why: str) -> None:
        rid = arb.reservation_id_of(SEG) if arb.holder_of(SEG) == c.name else None
        if rid is None:
            return
        epoch = arb.epoch(arb._res(SEG).gen)
        arb.release(site_agent_pb2.ReservationRelease(reservation_id=rid, reason=why))
        c.gate.revoke(SEG, epoch, reason=why)
        new = arb.holder_of(SEG)
        if new is not None and new != c.name:
            carriers[new].gate.note_grant(SEG, arb.epoch(arb._res(SEG).gen))
            if carriers[new].phase == "wait":
                carriers[new].phase = "cross"

    fired, cut_until = False, 0.0
    for k in range(int(horizon_s / DT)):
        t = clock.t
        on = {n: zone.distance(c.body.x, c.body.y) < zone.radius_m for n, c in carriers.items()}
        holder = arb.holder_of(SEG)
        h = carriers.get(holder) if holder else None
        if not fired and h is not None and h.phase == "cross" and on[h.name] and abs(h.body.x) < 8.0:
            fired = True                                    # the holder is in the middle of the lane
            if scenario == "release_early":
                release(h, "early (adversarial)")
            elif scenario == "holder_silent":
                h.link_up, cut_until = False, t + 60.0
                h.gate.note_link_down()
        if cut_until and t >= cut_until:
            for c in carriers.values():
                if not c.link_up:
                    c.link_up = True
                    c.gate.note_site_message()
            cut_until = 0.0
        for c in carriers.values():
            if c.link_up:
                heard[c.name] = t
            e = errands[c.name]
            if c.phase == "wait":
                _drive_to(c, e.parks[e.leg], t)
                if e.leg >= len(e.legs):
                    c.phase = "idle"
                elif request(c):
                    c.phase = "cross"
            elif c.phase == "cross":
                if not c.link_up:                           # its client is cut off: it stops where it is
                    _write(c, t, 0.0, 0.0)
                    continue
                goal = e.legs[e.leg][e.wp]
                if _drive_to(c, goal, t, cruise=1.2) or (e.wp < 2 and math.hypot(c.body.x - goal[0],
                                                                                   c.body.y - goal[1]) < 1.5):
                    e.wp += 1
                    if e.wp == 3:                           # parked at the far end: out of the lane, release
                        release(c, "crossed")
                        e.leg, e.wp, c.loads = e.leg + 1, 0, c.loads + 1
                        c.phase = "wait" if e.leg < len(e.legs) else "idle"
            else:
                _drive_to(c, e.parks[-1], t)
        for c in carriers.values():
            c.body.step(t, DT)
        clock.t += DT
        if k % 2 == 0:
            arb.observe({n: ((c.body.x, c.body.y), 0.0) for n, c in carriers.items()})
            arb.tick(last_heartbeat=dict(heard))
        on = {n: zone.distance(c.body.x, c.body.y) < zone.radius_m for n, c in carriers.items()}
        if all(on.values()) and not res.S1:
            res.S1, res.first_S1_t = True, round(clock.t - t0, 1)
        if arb.guard == "open":
            r = arb._res(SEG)
            for n, ins in on.items():
                if ins and r.holder != n and n not in r.custody and not res.S2:
                    res.S2, res.first_S2 = True, (round(clock.t - t0, 1), n)
        if all(c.phase == "idle" for c in carriers.values()):
            break
    res.loads = sum(c.loads for c in carriers.values())
    res.transitions = len(arb.txlog())
    return res


def run_all() -> list[TrialResult]:
    return [run_trial(s, sc) for sc in SCENARIOS for s in SCHEMES]


def main() -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    results = run_all()
    by = {(r.scenario, r.scheme): r for r in results}
    print(f"{'scenario':16s} " + " ".join(f"{s:>22s}" for s in SCHEMES))
    for sc in SCENARIOS:
        cells = []
        for s in SCHEMES:
            r = by[(sc, s)]
            v = "+".join(p for p, on in (("S1", r.S1), ("S2", r.S2)) if on) or "safe"
            cells.append(f"{v + f' ({r.loads} crossings)':>22s}")
        print(f"{sc:16s} " + " ".join(cells))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
