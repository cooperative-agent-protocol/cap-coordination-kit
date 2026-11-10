"""Five reservation schemes, one loading bay, two carriers: a small deterministic simulation that anyone can run.

The paper compares a mutex released on completion, a mutex group released on exit, a renewable fenced lease, the
same lease with a vacancy check, and the proposed protocol. Its trials use the reference implementation's agents and
simulators, which are not public. This module re-creates the comparison with public code only: the same arbiter
(``occupancy.OccupancyArbiter``), the same entry gate (``entry_gate.EntryGate``), and - in place of the agents - two
scripted carriers driven as unicycles on a simulated clock. Nothing here is random; a run is reproducible bit for bit.

    python -m cap_coordination_kit.scheme_trial            # the table: scheme x scenario
    python -m cap_coordination_kit.scheme_trial --json out.json

Scenarios (the holder is the carrier loading when the scenario starts):

    nominal          no fault: load, leave, release, the other carrier's turn
    release_early    the holder's client releases while the carrier is still inside
    holder_silent    the holder's link is cut for 90 s while it is inside; it stands still, then leaves
    reenter_old      after its turn a carrier is driven back in while the other holds the bay
    restart_lossy    the arbiter restarts during a load and has lost its journal

Oracles, from the simulated true positions: S1 two carriers inside the bay at once; S2 a carrier inside that the
arbiter accounts neither as holder nor in custody. The run's records are also replayed against the model's rules
(``occupancy_conformance``).

The kinematics are deliberately plain (a unicycle with a command delay and an acceleration limit); the point is the
schemes' logic, not vehicle dynamics.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any

from cap.v0.core import site_agent_pb2

from cap_coordination_kit.entry_gate import EntryGate
from cap_coordination_kit.occupancy import (
    PERMIT_REASON,
    OccupancyArbiter,
    OccupancyJournal,
    Scheme,
    VacancyEnvelope,
    Zone,
)
from cap_coordination_kit.occupancy_conformance import check_trial

BAY = "loading:loader"
BAY_R = 9.0
LOAD_POSE_R = 5.5                  # where a carrier stands to be loaded
STAGE_R = 15.0                     # where a carrier waits and where it goes when it leaves
DT = 0.1                           # s
DELAY_S, ACCEL = 0.3, 1.0          # the drive: command delay, acceleration limit
V_MAX = 1.5
LOAD_S = 40.0
PERMIT_T, LEASE_T, DELTA_ENTRY = 24.0, 48.0, 4.0
SCENARIOS = ("nominal", "release_early", "holder_silent", "reenter_old", "restart_lossy")
SCHEMES = ("mutex_on_done", "mutex_group", "lease_fenced", "lease_fenced_vacate", "cap")


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@dataclass
class Unicycle:
    """A body that follows (v, w) commands after a delay, its speed changing by at most ACCEL per second."""
    x: float
    y: float
    yaw: float
    v: float = 0.0
    w: float = 0.0
    pending: list[tuple[float, float, float]] = field(default_factory=list)   # (time it takes effect, v, w)
    target_v: float = 0.0

    def command(self, t: float, v: float, w: float) -> None:
        self.pending.append((t + DELAY_S, v, w))

    def step(self, t: float, dt: float) -> None:
        while self.pending and self.pending[0][0] <= t + 1e-9:
            _t, self.target_v, self.w = self.pending.pop(0)
        dv = max(-ACCEL * dt, min(ACCEL * dt, self.target_v - self.v))
        self.v += dv
        self.x += self.v * math.cos(self.yaw) * dt
        self.y += self.v * math.sin(self.yaw) * dt
        self.yaw = math.atan2(math.sin(self.yaw + self.w * dt), math.cos(self.yaw + self.w * dt))


@dataclass
class Carrier:
    """A scripted client and its machine: wait for the bay, drive in, load, drive out, release."""
    name: str
    body: Unicycle
    gate: EntryGate
    stage: tuple[float, float]
    load_pose: tuple[float, float]
    phase: str = "wait"            # wait | approach | load | leave | idle | rogue
    rid: str = ""
    n: int = 0
    loaded_until: float = 0.0
    link_up: bool = True
    loads: int = 0
    next_request: float = 0.0


@dataclass
class TrialResult:
    scheme: str
    scenario: str
    S1: bool = False
    S2: bool = False
    loads: int = 0
    first_S1_t: float | None = None
    first_S2: tuple[float, str] | None = None
    conforming: bool = True
    sensing: int = 0
    transitions: int = 0
    gate_steps: int = 0
    unadmitted_entries: int = 0
    notes: list[str] = field(default_factory=list)


def _drive_to(c: Carrier, goal: tuple[float, float], t: float, *, cruise: float = 0.9) -> bool:
    """One control tick towards ``goal`` through the gate; True on arrival."""
    b = c.body
    dx, dy = goal[0] - b.x, goal[1] - b.y
    dist = math.hypot(dx, dy)
    if dist < 0.4:
        _write(c, t, 0.0, 0.0)
        return abs(b.v) < 0.02
    err = math.atan2(math.sin(math.atan2(dy, dx) - b.yaw), math.cos(math.atan2(dy, dx) - b.yaw))
    w = max(-0.8, min(0.8, 1.5 * err))
    v = 0.0 if abs(err) > 0.6 else min(cruise, 0.4 + 0.5 * dist)
    _write(c, t, v, w)
    return False


def _write(c: Carrier, t: float, v: float, w: float) -> None:
    """Every drive command goes through the entry gate (its check, then the speed it allows)."""
    _reason, cap = c.gate.check((c.body.x, c.body.y, c.body.yaw, c.body.v), (v, w))
    if cap is not None:
        scale = 0.0 if cap <= 0.0 or abs(v) < 1e-9 else min(1.0, cap / abs(v))
        v, w = v * scale, w * scale
    c.body.command(t, v, w)


def run_trial(scheme_name: str, scenario: str, *, noise_sigma: float = 0.0, horizon_s: float = 600.0) -> TrialResult:
    clock = Clock()
    scheme = Scheme.named(scheme_name, permit_ttl_s=PERMIT_T, lease_ttl_s=LEASE_T)
    zone = Zone(BAY, 0.0, 0.0, BAY_R, frozenset({"loader"}))
    envelope = VacancyEnvelope(eps_m=0.5, v_max_mps=V_MAX, latency_s=0.2, d_brake_m=V_MAX ** 2 / (2 * ACCEL),
                               max_pose_age_s=1.0)
    state_dir = tempfile.mkdtemp(prefix="cap-kit-")

    def make_arbiter() -> OccupancyArbiter:
        a = OccupancyArbiter(scheme, clock=clock, journal=OccupancyJournal(state_dir, sync=False), envelope=envelope)
        a.set_zone(zone)
        return a

    arb = [make_arbiter()]                       # in a list: a restart replaces it
    res = TrialResult(scheme=scheme_name, scenario=scenario)
    carriers: dict[str, Carrier] = {}
    heard: dict[str, float] = {}
    poses_log: list[tuple[float, str, float, float]] = []
    t0 = clock.t

    def ask_permit(c: Carrier) -> None:
        if not c.link_up:
            return
        t_req = clock.t
        st = arb[0].request(site_agent_pb2.ReservationRequest(
            reservation_id=f"{c.name}-permit-{int(clock.t * 10)}", resource_id=BAY, holder_id=c.name,
            reason=PERMIT_REASON))
        heard[c.name] = clock.t
        if st.state == 1 and st.reason == PERMIT_REASON:
            c.gate.note_permit(BAY, int(st.grant_epoch), t_req)

    for i, name in enumerate(("a", "b")):
        ang = math.radians(160 + 40 * i)
        stage = (STAGE_R * math.cos(ang), STAGE_R * math.sin(ang))
        load = (LOAD_POSE_R * math.cos(ang), LOAD_POSE_R * math.sin(ang))
        gate = EntryGate(machine_id=name, mode=scheme.gate, permit_ttl_s=scheme.permit_ttl_s, rho=scheme.rho,
                         delta_entry_s=DELTA_ENTRY, link_lease_s=scheme.lease_ttl_s, clock=clock,
                         act_delay_s=DELAY_S, decel_mps2=ACCEL, period_s=2 * DT, resend_s=1.0)
        gate.set_zones([zone])
        c = Carrier(name=name, body=Unicycle(stage[0], stage[1], ang + math.pi), gate=gate, stage=stage, load_pose=load)
        gate.request_permit = (lambda _rid, c=c: ask_permit(c))
        carriers[name] = c

    def request(c: Carrier) -> bool:
        if not c.link_up or clock.t < c.next_request:
            return False
        c.next_request = clock.t + 1.0
        c.n += 1
        c.rid = f"{c.name}-{c.n}"
        st = arb[0].request(site_agent_pb2.ReservationRequest(reservation_id=c.rid, resource_id=BAY, holder_id=c.name))
        heard[c.name] = clock.t
        if st.state == 1:
            c.gate.note_grant(BAY, int(st.grant_epoch))
            return True
        return False

    def release(c: Carrier, why: str) -> None:
        rid = arb[0].reservation_id_of(BAY) if arb[0].holder_of(BAY) == c.name else None
        if rid is None:
            return
        epoch = arb[0].epoch(arb[0]._res(BAY).gen)
        arb[0].release(site_agent_pb2.ReservationRelease(reservation_id=rid, reason=why))
        c.gate.revoke(BAY, epoch, reason=why)          # the machine is told its grant ended
        new = arb[0].holder_of(BAY)                    # a designated scheme has handed the bay over
        if new is not None and new != c.name:
            carriers[new].gate.note_grant(BAY, arb[0].epoch(arb[0]._res(BAY).gen))
            carriers[new].phase = "approach" if carriers[new].phase == "wait" else carriers[new].phase

    fired = {"scenario": False, "reentered": False, "restart": False}
    cut_until = 0.0
    steps = int(horizon_s / DT)
    for k in range(steps):
        t = clock.t
        # ---- the scenario
        holder = arb[0].holder_of(BAY)
        h = carriers.get(holder) if holder else None
        inside = {n: math.hypot(c.body.x, c.body.y) < BAY_R for n, c in carriers.items()}
        if not fired["scenario"] and h is not None and h.phase == "load" and t > h.loaded_until - LOAD_S + 5.0:
            fired["scenario"] = True
            if scenario == "release_early":
                release(h, "early (adversarial)")
            elif scenario == "holder_silent":
                h.link_up, cut_until = False, t + 90.0
                h.gate.note_link_down()
            elif scenario == "restart_lossy":
                OccupancyJournal(state_dir).path.unlink(missing_ok=True)
                log, seq = arb[0]._txlog, arb[0]._seq
                arb[0] = make_arbiter()
                started = list(arb[0]._txlog)
                for i, rec in enumerate(started, start=1):
                    rec["seq"] = seq + i
                    log.append(rec)
                arb[0]._txlog, arb[0]._seq = log, seq + len(started)
                res.notes.append(f"arbiter restarted without its journal at {t - t0:.1f} s (guard {arb[0].guard})")
        if cut_until and t >= cut_until:
            for c in carriers.values():
                if not c.link_up:
                    c.link_up = True
                    c.gate.note_site_message()
            cut_until = 0.0
        if scenario == "reenter_old" and not fired["reentered"]:
            for c in carriers.values():
                other = next(o for o in carriers.values() if o is not c)
                if c.loads >= 1 and c.phase in ("wait", "idle") and other.phase == "load":
                    fired["reentered"] = True
                    c.phase = "rogue"                   # driven back in on whatever authority it still has
        # ---- each carrier's script
        for c in carriers.values():
            if c.link_up:
                heard[c.name] = t
            if c.phase == "wait":
                _drive_to(c, c.stage, t)
                if request(c):
                    c.phase = "approach"
            elif c.phase == "approach":
                if _drive_to(c, c.load_pose, t):
                    c.phase, c.loaded_until = "load", t + LOAD_S
            elif c.phase == "load":
                _write(c, t, 0.0, 0.0)
                if t >= c.loaded_until and c.link_up:
                    c.loads += 1
                    c.phase = "leave"
            elif c.phase == "leave":
                if _drive_to(c, c.stage, t):
                    release(c, "load done")                        # the carrier is out before its client releases
                    c.phase = "wait" if c.loads < 2 else "idle"
            elif c.phase == "rogue":
                if _drive_to(c, c.load_pose, t) or t - t0 > horizon_s - 60:
                    c.phase = "leave"
            else:
                _drive_to(c, c.stage, t)
        # ---- the world and the arbiter
        for c in carriers.values():
            c.body.step(t, DT)
        clock.t += DT
        if k % 2 == 0:
            obs = {}
            for i, (n, c) in enumerate(carriers.items()):
                # a fixed, repeatable pseudo-error when asked for (no random numbers anywhere in the kit)
                e = noise_sigma * math.sin(0.7 * k + 1.3 * i) * 1.6
                obs[n] = ((c.body.x + e, c.body.y - e), 0.0) if c.link_up else (None, None)
            arb[0].observe(obs)
            arb[0].tick(last_heartbeat=dict(heard))
        for n, c in carriers.items():
            poses_log.append((clock.t - t0, n, c.body.x, c.body.y))
        # ---- the oracles
        inside = {n: math.hypot(c.body.x, c.body.y) < BAY_R for n, c in carriers.items()}
        if all(inside.values()) and not res.S1:
            res.S1, res.first_S1_t = True, round(clock.t - t0, 1)
        if arb[0].guard == "open":
            r = arb[0]._res(BAY)
            for n, ins in inside.items():
                if ins and r.holder != n and n not in r.custody and not res.S2:
                    res.S2, res.first_S2 = True, (round(clock.t - t0, 1), n)
        if all(c.phase == "idle" for c in carriers.values()):
            break
    res.loads = sum(c.loads for c in carriers.values())
    gate_logs = {n: [asdict(e) for e in c.gate.log] for n, c in carriers.items()}
    res.unadmitted_entries = sum(1 for es in gate_logs.values() for e in es
                                 if e["event"] == "enter" and e.get("reason") == "not admitted")
    conf = check_trial(txlog=arb[0].txlog(), gate_logs=gate_logs, poses=poses_log, zones={BAY: (0.0, 0.0, BAY_R)},
                       carriers=list(carriers), scheme=scheme, t0_mono=t0)
    res.conforming, res.sensing = conf.conforming, len(conf.sensing)
    res.transitions, res.gate_steps = conf.transitions, conf.gate_steps
    if not conf.conforming:
        res.notes.append(f"not conforming: {(conf.violations or conf.divergences)[0]}")
    return res


def run_all(noise_sigma: float = 0.0) -> list[TrialResult]:
    return [run_trial(s, sc, noise_sigma=noise_sigma) for sc in SCENARIOS for s in SCHEMES]


def table(results: list[TrialResult]) -> str:
    by = {(r.scenario, r.scheme): r for r in results}
    lines = [f"{'scenario':16s} " + " ".join(f"{s:>20s}" for s in SCHEMES)]
    for sc in SCENARIOS:
        cells = []
        for s in SCHEMES:
            r = by[(sc, s)]
            v = "+".join(p for p, on in (("S1", r.S1), ("S2", r.S2)) if on) or "safe"
            cells.append(f"{v + f' ({r.loads} loads)':>20s}")
        lines.append(f"{sc:16s} " + " ".join(cells))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ap.add_argument("--noise", type=float, default=0.0, help="amplitude scale of a fixed position error at the arbiter")
    args = ap.parse_args()
    results = run_all(args.noise)
    print(table(results))
    bad = [r for r in results if not r.conforming]
    print(f"\n{len(results)} trials; records conform to the model in {len(results) - len(bad)}; "
          f"{sum(r.transitions for r in results)} arbiter transitions, {sum(r.gate_steps for r in results)} gate steps")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump([asdict(r) for r in results], fh, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
