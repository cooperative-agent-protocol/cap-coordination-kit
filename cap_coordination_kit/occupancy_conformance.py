# Copied from the reference implementation by its scripts/paper_r1/sync_kit.py - edit there, not here.
"""Do a trial's records follow the model? (AiC revision WP3b)

The occupancy model (cap-spec formal/tla/OccupancyHandover.tla) says which steps a scheme's arbiter and a machine's
gate may take. This module replays what the implementation actually did in a trial - the arbiter's transition log,
the machines' gate logs and their true positions - against those rules, independently of the arbiter's code: it keeps
its own copy of the model's state (holder, custody, counted permits, the restart guard), checks that every logged step
was enabled in that state, applies the model's effect, and compares the result with the state the arbiter recorded.

    arbiter log            model action     enabled when (for a scheme with an arbiter)
    GRANTED m              Grant(m)         the guard is open; no holder; nobody incompatible in custody; no counted
                                            permit of an incompatible machine; the planned successor, if any, is m
    HANDOVER s -> m        TransferTo       s holds the zone; m is the planned successor (designated succession)
    PERMIT m               IssueAuth(m)     the scheme has permits; the guard is open; m holds the zone; nobody
                                            incompatible in custody; no counted permit of an incompatible machine
    RELEASED m             Rel(m)           m holds the zone
    EXPIRED m              LeaseExpire(m)   the scheme expires holders; m holds the zone; none of m's permits counted
    VACATED m              ConfirmVacant    m is in custody; and, from the true positions, m is out of the zone (A2)
    DENIED / PERMIT_DENIED (no step)        the corresponding step was not enabled (else: a refusal the model does
                                            not ask for - reported apart, it is not a safety matter)
    RESTORED, GUARD_*      ArbRestart ...   the state taken up equals the state before the restart; the guard's steps

    gate log               model action     enabled when
    admit / enter m        Enter(m)         gate "local": a grant to m that m has not given up; gate "permit": a
                                            permit issued to m, not yet used, not timed out on m's clock

A counted permit stops being counted T(1 + rho) after it was issued (ArbLapse; the arbiter logs no step for it).
The plan's choice of successor (Plan) is the coordinator's and is not logged as a step: the successor named in each
record is taken as given.

``violations``: a step the model does not allow. ``divergences``: the arbiter's recorded state after a step differs
from the model's. ``sensing``: a vacancy confirmed while the machine was truly inside (A2 broken - by the injected
pose error, not by the arbiter). A trial conforms when the first two are empty.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cap_coordination_kit.occupancy import Scheme


@dataclass
class OccupancyConformance:
    scheme: str = ""
    transitions: int = 0
    gate_steps: int = 0
    event_counts: dict[str, int] = field(default_factory=dict)
    violations: list[dict[str, Any]] = field(default_factory=list)
    divergences: list[dict[str, Any]] = field(default_factory=list)
    sensing: list[dict[str, Any]] = field(default_factory=list)
    unasked_refusals: list[dict[str, Any]] = field(default_factory=list)
    restarts: int = 0

    @property
    def conforming(self) -> bool:
        return not self.violations and not self.divergences

    def summary(self) -> dict[str, Any]:
        return {"scheme": self.scheme, "transitions": self.transitions, "gate_steps": self.gate_steps,
                "event_counts": dict(self.event_counts), "violations": len(self.violations),
                "divergences": len(self.divergences), "sensing": len(self.sensing),
                "unasked_refusals": len(self.unasked_refusals), "restarts": self.restarts,
                "conforming": self.conforming,
                "first_violation": self.violations[0] if self.violations else None,
                "first_divergence": self.divergences[0] if self.divergences else None}


@dataclass
class _Zone:
    holder: str | None = None
    custody: set[str] = field(default_factory=set)
    live: list[tuple[str, int, float]] = field(default_factory=list)   # (machine, epoch, counted until [mono])


def check_trial(*, txlog: list[dict[str, Any]], gate_logs: dict[str, list[dict[str, Any]]] | None = None,
                poses: list[tuple[float, str, float, float]] | None = None, zones: dict[str, tuple[float, float, float]]
                | None = None, carriers: list[str], scheme: Scheme, permit_life_s: float | None = None,
                machine_permit_life_s: float | None = None, t0_mono: float | None = None) -> OccupancyConformance:
    """Replay one trial. ``poses``: (seconds since the trial's start, machine, x, y) true positions; ``zones``:
    resource -> (x, y, radius); ``t0_mono``: the monotonic time of the trial's start (poses' zero). The permit
    lives default to the scheme's (T(1 + rho) at the arbiter, T(1 - rho) at the machine)."""
    res = OccupancyConformance(scheme=scheme.name)
    life = float(permit_life_s if permit_life_s is not None else scheme.arbiter_permit_life_s)
    m_life = float(machine_permit_life_s if machine_permit_life_s is not None
                   else scheme.permit_ttl_s * (1.0 - scheme.rho))
    carriers_set = set(carriers)
    occ_aware = scheme.release == "onVacate" or scheme.expiry == "ttlVacate"
    state: dict[str, _Zone] = {}
    guard = "open"
    tx = sorted(txlog, key=lambda r: r.get("seq", 0))
    zones = zones or {}

    def bad(kind: list, rec: dict[str, Any], reason: str, **extra: Any) -> None:
        kind.append({"seq": rec.get("seq"), "event": rec.get("event"), "resource_id": rec.get("resource_id"),
                     "machine": rec.get("requested_holder"), "reason": reason, **extra})

    def others(z: _Zone, m: str | None, t: float) -> tuple[list[str], list[str]]:
        """Incompatible machines in custody, and with a counted permit, as seen by ``m``."""
        inc_c = sorted(c for c in z.custody if c != m and c in carriers_set)
        inc_l = sorted({h for (h, _e, until) in z.live if h != m and h in carriers_set and until > t})
        return inc_c, inc_l

    def tracked(rid: str) -> bool:
        """Custody is kept only where a zone is registered (a loading bay); with no zones given, everywhere."""
        return rid in zones or not zones

    def true_inside(m: str, rid: str, t_mono: float) -> bool | None:
        if not poses or rid not in zones or t0_mono is None:
            return None
        zx, zy, zr = zones[rid]
        t = t_mono - t0_mono
        near = None
        for (tp, mid, x, y) in poses:            # the true position nearest in time (10 Hz samples)
            if mid != m:
                continue
            if near is None or abs(tp - t) < abs(near[0] - t):
                near = (tp, x, y)
            elif tp > t + 1.0:
                break
        if near is None or abs(near[0] - t) > 1.0:
            return None
        return math.hypot(near[1] - zx, near[2] - zy) < zr

    for rec in tx:
        ev, rid, m = rec.get("event"), rec.get("resource_id") or "", rec.get("requested_holder")
        t = float(rec.get("t_mono") or 0.0)
        res.transitions += 1
        res.event_counts[ev] = res.event_counts.get(ev, 0) + 1
        if not rid:
            # steps of the whole arbiter
            if ev == "RESTORED":
                res.restarts += 1                 # the journal taken up: the state must be the one before
                guard = "open"
            elif ev and ev.startswith("GUARD_"):
                new = {"GUARD_OPEN": "open", "GUARD_WAITTIME": "waitTime", "GUARD_WAITVACANCY": "waitVacancy"}.get(ev)
                if new is None:
                    bad(res.violations, rec, f"unknown guard step {ev}")
                    continue
                if new in ("waitTime", "waitVacancy") and guard == "open" and "restart" not in (rec.get("reason") or "") \
                        and "journal" not in (rec.get("reason") or ""):
                    bad(res.violations, rec, "the guard closed without a restart")
                if "journal" in (rec.get("reason") or "") and "restart" in (rec.get("reason") or ""):
                    # a restart that lost the journal (a scheme with neither leases nor custody opens at once)
                    res.restarts += 1
                    for z in state.values():      # a lost journal: the arbiter knows nothing; neither does the model
                        z.holder, z.custody, z.live = None, set(), []
                guard = new
            continue
        z = state.setdefault(rid, _Zone())
        z.live = [p for p in z.live if p[2] > t]                       # ArbLapse: permits no longer counted
        planned_before = rec.get("planned_before")
        if ev == "GRANTED":
            if scheme.arb != "none":
                inc_c, inc_l = others(z, m, t)
                if guard != "open":
                    bad(res.violations, rec, f"granted while the guard is {guard}")
                if z.holder is not None and z.holder != m:
                    bad(res.violations, rec, f"granted while held by {z.holder}")
                if inc_c:
                    bad(res.violations, rec, f"granted while {inc_c} not confirmed out")
                if inc_l:
                    bad(res.violations, rec, f"granted while a permit of {inc_l} is counted")
                if scheme.succession == "designated" and planned_before not in (None, m):
                    bad(res.violations, rec, f"granted past the planned successor {planned_before}")
                if scheme.arb == "queue" and planned_before not in (None, m):
                    bad(res.violations, rec, f"granted past the head of the queue {planned_before}")
            z.holder = m
            z.custody.discard(m)
        elif ev == "HANDOVER":
            sender = rec.get("prior_holder")
            if z.holder != sender:
                bad(res.violations, rec, f"hand-over from {sender}, who does not hold the zone ({z.holder} does)")
            if guard != "open":
                bad(res.violations, rec, f"hand-over while the guard is {guard}")
            if scheme.succession == "designated" and planned_before not in (None, m):
                bad(res.violations, rec, f"hand-over past the planned successor {planned_before}")
            z.holder = m
            if occ_aware and sender is not None and tracked(rid):
                z.custody.add(sender)
            z.custody.discard(m)
        elif ev == "PERMIT":
            inc_c, inc_l = others(z, m, t)
            if scheme.gate != "permit":
                bad(res.violations, rec, "a permit in a scheme without permits")
            if guard != "open":
                bad(res.violations, rec, f"a permit while the guard is {guard}")
            if z.holder != m:
                bad(res.violations, rec, f"a permit to {m}, who does not hold the zone ({z.holder} does)")
            if inc_c:
                bad(res.violations, rec, f"a permit while {inc_c} not confirmed out")
            if inc_l:
                bad(res.violations, rec, f"a permit while a permit of {inc_l} is counted")
            z.live.append((m, int(rec.get("epoch") or 0), t + life))
        elif ev == "RELEASED":
            holder = rec.get("prior_holder")
            if z.holder != holder:
                bad(res.violations, rec, f"released by {holder}, who does not hold the zone ({z.holder} does)")
            z.holder = None
            if scheme.release == "onVacate" and holder is not None and tracked(rid):
                z.custody.add(holder)
        elif ev == "EXPIRED":
            if scheme.expiry not in ("ttl", "ttlVacate"):
                bad(res.violations, rec, "an expiry in a scheme without expiry")
            if z.holder != m:
                bad(res.violations, rec, f"expired {m}, who does not hold the zone ({z.holder} does)")
            if any(h == m for (h, _e, _u) in z.live):
                bad(res.violations, rec, f"expired {m} while its permit is counted")
            z.holder = None
            if scheme.expiry == "ttlVacate" and m is not None and tracked(rid):
                z.custody.add(m)
        elif ev == "VACATED":
            if m not in z.custody:
                bad(res.violations, rec, f"vacancy confirmed for {m}, who is not in custody")
            inside = true_inside(m, rid, t) if m else None
            if inside:
                bad(res.sensing, rec, f"vacancy confirmed for {m} while it was inside the zone (true position)")
            z.custody.discard(m)
        elif ev == "DENIED":
            if scheme.arb != "none":
                inc_c, inc_l = others(z, m, t)
                free = (guard == "open" and z.holder is None and not inc_c and not inc_l
                        and planned_before in (None, m) and (rec.get("planned") in (None, m)))
                if free and "journal" not in (rec.get("reason") or ""):
                    bad(res.unasked_refusals, rec, "denied although the grant was enabled")
        elif ev == "PERMIT_DENIED":
            inc_c, inc_l = others(z, m, t)
            if scheme.gate == "permit" and guard == "open" and z.holder == m and not inc_c and not inc_l \
                    and "journal" not in (rec.get("reason") or ""):
                bad(res.unasked_refusals, rec, "permit refused although it was enabled")
        elif ev == "HANDOVER_REFUSED":
            pass
        else:
            bad(res.violations, rec, f"a step the model does not have: {ev}")
        # the arbiter's own record of the state after the step
        if ev in ("GRANTED", "HANDOVER", "RELEASED", "EXPIRED", "VACATED", "PERMIT"):
            if (rec.get("resolved_holder") or None) != z.holder:
                bad(res.divergences, rec, f"holder: the model has {z.holder}, the arbiter {rec.get('resolved_holder')}")
            if sorted(rec.get("custody") or []) != sorted(z.custody):
                bad(res.divergences, rec, f"custody: the model has {sorted(z.custody)}, the arbiter "
                                          f"{sorted(rec.get('custody') or [])}")
            if (rec.get("guard") or "open") != guard:
                bad(res.divergences, rec, f"guard: the model has {guard}, the arbiter {rec.get('guard')}")

    # ---- the machines' side: every admission and entry on authority the arbiter gave
    grants: dict[tuple[str, str], list[tuple[float, float | None]]] = {}    # (machine, zone) -> [(from, until)]
    permits: dict[tuple[str, str], list[tuple[float, int]]] = {}            # (machine, zone) -> [(t, epoch)]
    cur: dict[str, tuple[str, float] | None] = {}
    for rec in tx:
        ev, rid, m, t = rec.get("event"), rec.get("resource_id") or "", rec.get("requested_holder"), \
            float(rec.get("t_mono") or 0.0)
        if not rid:
            continue
        if ev in ("GRANTED", "HANDOVER"):
            if cur.get(rid) is not None and cur[rid][0] != m:
                grants.setdefault((cur[rid][0], rid), []).append((cur[rid][1], t))
            cur[rid] = (m, t)
        elif ev in ("RELEASED", "EXPIRED") and cur.get(rid) is not None:
            grants.setdefault((cur[rid][0], rid), []).append((cur[rid][1], t))
            cur[rid] = None
        elif ev == "PERMIT":
            permits.setdefault((m, rid), []).append((t, int(rec.get("epoch") or 0)))
    for rid, c in cur.items():
        if c is not None:
            grants.setdefault((c[0], rid), []).append((c[1], None))
    for mid, events in (gate_logs or {}).items():
        used: set[int] = set()
        for e in events:
            ev, rid, t = e.get("event"), e.get("resource_id") or "", float(e.get("t_mono") or 0.0)
            if ev == "enter":
                res.gate_steps += 1
                if e.get("reason") == "not admitted":
                    res.violations.append({"event": "enter", "machine": mid, "resource_id": rid, "t_mono": t,
                                           "reason": "entered without an admission of its gate"})
                continue
            if ev != "admit":
                continue
            res.gate_steps += 1
            if scheme.gate == "local":
                ok = any(a <= t + 0.5 for (a, _b) in grants.get((mid, rid), []))    # a grant notice once received
                if not ok:
                    res.violations.append({"event": "admit", "machine": mid, "resource_id": rid, "t_mono": t,
                                           "reason": "admitted without a grant ever made to it"})
            elif scheme.gate == "permit":
                ep = e.get("epoch")
                issued = [(tp, k) for (tp, k) in permits.get((mid, rid), []) if k == ep and tp <= t + 0.5]
                if not issued:
                    res.violations.append({"event": "admit", "machine": mid, "resource_id": rid, "t_mono": t,
                                           "reason": f"admitted on a permit ({ep}) the arbiter did not issue"})
                    continue
                newest = max(tp for (tp, _k) in issued)
                if t - newest > m_life + 0.5:
                    res.violations.append({"event": "admit", "machine": mid, "resource_id": rid, "t_mono": t,
                                           "reason": f"admitted {t - newest:.1f} s after its permit was issued "
                                                     f"(usable for {m_life:.1f} s)"})
                used.add(int(ep or 0))
    return res


def check_trial_dir(d: str | Path) -> OccupancyConformance | None:
    """A trial's directory as the campaigns write it (metrics.json, reservation_txlog.jsonl, gate_logs.json,
    events.jsonl). None when the trial has no scheme or no log."""
    d = Path(d)
    try:
        m = json.loads((d / "metrics.json").read_text())
        tx = [json.loads(line) for line in (d / "reservation_txlog.jsonl").read_text().splitlines() if line.strip()]
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    name = m.get("scheme")
    if not name or not tx:
        return None
    sp = float(m.get("speedup") or (1.0 if m.get("cell") == "unity" else 8.0))
    scheme = Scheme.named(name)
    gate_logs = json.loads((d / "gate_logs.json").read_text()) if (d / "gate_logs.json").exists() else {}
    poses: list[tuple[float, str, float, float]] = []
    loader_xy: dict[str, tuple[float, float]] = {}
    carriers = list(m.get("carriers") or [])
    t0_wall = None
    if (d / "events.jsonl").exists():
        for line in (d / "events.jsonl").read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if t0_wall is None and "wall" in e and "t" in e:
                t0_wall = float(e["wall"]) - float(e["t"])
            if e.get("kind") == "occupancy" and (e.get("extra") or {}).get("kind") == "pose":
                mid = e.get("machine_id") or ""
                if mid in carriers:
                    poses.append((float(e["t"]), mid, float(e["extra"]["x"]), float(e["extra"]["y"])))
                else:
                    loader_xy.setdefault(mid, (float(e["extra"]["x"]), float(e["extra"]["y"])))
    # the loading bays: a 9 m disc round the loader (the coordinator); the times of the arbiter's log
    # are monotonic, the poses' are seconds since the trial's start - joined through the log's wall-clock times
    zones: dict[str, tuple[float, float, float]] = {}
    for rec in tx:
        rid = rec.get("resource_id") or ""
        if rid.startswith("loading:") and rid not in zones:
            loader = rid.rsplit(":", 1)[-1]
            if loader in loader_xy:
                zones[rid] = (*loader_xy[loader], 9.0)
    t0_mono = None
    if t0_wall is not None:
        t0_mono = float(tx[0]["t_mono"]) - (float(tx[0]["t_wall"]) - t0_wall)
    return check_trial(txlog=tx, gate_logs=gate_logs, poses=poses, zones=zones, carriers=carriers, scheme=scheme,
                       permit_life_s=_wall_life(m, scheme, sp),
                       machine_permit_life_s=_wall_life(m, scheme, sp, arbiter=False), t0_mono=t0_mono)


def _wall_life(m: dict[str, Any], scheme: Scheme, sp: float, *, arbiter: bool = True) -> float:
    """The permit's life in the clock the logs are written in: the campaign gives it in simulated seconds (24 s by
    default) and divides it by the speed-up for the trial."""
    ttl = float(m.get("permit_ttl_s") or 24.0) / sp
    return ttl * (1.0 + scheme.rho) if arbiter else ttl * (1.0 - scheme.rho)
