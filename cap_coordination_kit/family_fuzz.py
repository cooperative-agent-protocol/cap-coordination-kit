"""Randomised executions of the kit's arbiter for every scheme of the family, checked against S1-S3.

The model checker enumerates the family of reservation schemes (cap-spec/formal/tla/family) over a small model. This
module runs the same family on the arbiter's own code (``OccupancyArbiter``). An untrusted client issues requests,
permit requests, releases and hand-overs at any time; links fail and recover; answers and notices are delayed or
lost; clocks drift within rho; machines and the arbiter restart, the arbiter with its journal or without it (or,
"mixed", either at each restart, so that a restart may find the journal written during the wait after one that lost
it); and the
Site observes every machine's true position (sound vacancy sensing, A2). The machines' entry gates follow their
scheme: no check, a remembered grant, or a single-use permit timed from the moment its request was sent. After every
step the oracle checks

  S1  no two carriers are in the bay at once;
  S2  while the arbiter's zones are open, every carrier in the bay is its holder or in its custody;
  S3  every entry is made on an authorisation that the arbiter still accounts for: a permit it still counts as
      possibly in use (after a restart without its journal, any permit until its wait is over), or, for a gate that
      remembers grants, the bay's current grant (as in OccupancyHandover.tla; not checked for gates without a check).

Two environments: ``site`` - a release or hand-over may be made in any machine's name, as the Site's coordinator
may give a bay up at any time, and a machine learns that it lost the bay only from a notice over its link;
``own`` - a reservation is released or handed over only by its holder's own client, whose gate drops its authority in
the same step (the environment of the model's first version). Restarts of the arbiter keep the journal or lose it.

The module plays the coordinator and the machines' gates as the model defines them: a gate that remembers grants
is told of a grant only when the arbiter would admit the holder (no incompatible machine in custody, no incompatible
permit counted; IssueAuth in the model), and when a machine's own agent gives the bay up, an authorisation still on
its way to it is discarded on arrival (OwnGiveUp). The ``custody``, ``drift`` and ``sensing`` ablations remove the
custody rule's second half, let the machines' clocks run slower than rho allows (A4), or let the Site see a machine
in the bay as far out (A2).

A scheme is reported safe when no run violated S1-S3. Randomised runs find violations; they do not prove that there
are none. What the module shows is that the arbiter's code, and not only the model, separates the safe schemes from
the others, and in the same places.

  python -m cap_coordination_kit.family_fuzz [--runs 200] [--steps 300] [--env site|own] [--journal kept|lost|mixed]
      [--schemes all|named|safe|<arb-gate-release-expiry-succession>,...]
      [--ablate custody|drift|sensing|guard]   (guard: a restart that finds a journal opens the zones, as before
                                                2026-11-13; with --journal mixed it breaks the safe schemes)
      [--json out.json]
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cap.v0.core import events_pb2, site_agent_pb2

from .occupancy import PERMIT_REASON, OccupancyArbiter, OccupancyJournal, Scheme, VacancyEnvelope, Zone

BAY = "loading:loader"
RADIUS = 9.0
ZONE = Zone(BAY, 0.0, 0.0, RADIUS, frozenset({"loader"}))
CARRIERS = ("c1", "c2", "c3")
# where a carrier stands: in the bay; just outside it, closer than the vacancy envelope can confirm; far out
POSE = {"in": (RADIUS - 3.0, 0.0), "near": (RADIUS + 0.8, 0.0), "far": (RADIUS + 12.0, 0.0)}
GRANTED = events_pb2.ReservationStatus.RESERVATION_STATE_GRANTED
ENVELOPE = VacancyEnvelope(eps_m=0.5, v_max_mps=1.5, latency_s=0.3, d_brake_m=1.0, max_pose_age_s=1.0)

ARB, GATE, RELEASE, EXPIRY, SUCCESSION = (("none", "deny", "queue"), ("none", "local", "permit"),
                                          ("onDone", "onVacate"), ("none", "ttl", "ttlVacate"), ("free", "designated"))


def family() -> list[str]:
    """The 90 members: every combination of the five choices, without designation where nothing arbitrates."""
    return [f"{a}-{g}-{r}-{e}-{s}" for a, g, r, e, s in itertools.product(ARB, GATE, RELEASE, EXPIRY, SUCCESSION)
            if not (a == "none" and s == "designated")]


NAMED = {"mutex_on_done": "deny-local-onDone-none-free", "mutex_group": "deny-local-onVacate-none-free",
         "lease_fenced": "deny-permit-onDone-ttl-free", "lease_fenced_vacate": "deny-permit-onVacate-ttlVacate-free",
         "cap": "queue-permit-onVacate-ttlVacate-designated"}


def scheme_of(key: str, **kw: Any) -> Scheme:
    a, g, r, e, s = key.split("-")
    return Scheme(arb=a, gate=g, release=r, expiry=e, succession=s, name=key, **kw)


def expected_safe(key: str, env: str, journal: str) -> bool:
    """The model checker's verdict on S1-S3 for this environment (cap-spec/formal/tla/RESULTS.md): with Site-side
    releases, the schemes with arbitration, timed permits, release on vacancy and either no expiry or expiry into
    custody (eight); with the holder's own releases only, also the four that rely on a remembered grant and never
    expire a holder, as long as the journal is kept."""
    a, g, r, e, _s = key.split("-")
    if a == "none" or r != "onVacate":
        return False
    if g == "permit":
        return e in ("none", "ttlVacate")
    return g == "local" and e == "none" and env == "own" and journal == "kept"     # "mixed" loses it at times


# ---------------------------------------------------------------------------- the world
@dataclass
class Machine:
    name: str
    rate: float                       # its monotonic clock's rate against real time
    offset: float = 0.0               # its clock's reading at real time 0 in this start
    pos: str = "far"
    link: bool = True
    start: int = 0                    # incremented by a restart: messages to an earlier start are dropped
    permits: list[tuple[int, float]] = field(default_factory=list)    # (epoch, own clock when the request was sent)
    believes: int | None = None       # the epoch of a grant it remembers (a "local" gate admits on it)
    rid: str | None = None            # the reservation id it knows as its own

    def clock(self, real: float) -> float:
        return self.offset + self.rate * real


@dataclass
class Violation:
    prop: str
    step: int
    detail: str


class World:
    def __init__(self, key: str, *, seed: int, env: str, journal: str, steps: int, rho: float = 0.02,
                 permit_ttl_s: float = 30.0, lease_ttl_s: float = 6.0, ablate: str = "") -> None:
        self.key, self.env, self.journal_mode, self.steps, self.ablate = key, env, journal, steps, ablate
        self.rng = random.Random(seed)
        self.scheme = scheme_of(key, permit_ttl_s=permit_ttl_s, rho=rho, lease_ttl_s=lease_ttl_s,
                                custody_until_lapse=ablate != "custody")
        self.rho = rho
        self.real = 0.0
        # A4: every monotonic clock within rho of real time; the "drift" ablation lets the machines' clocks run slower
        lo, hi = (0.5, 0.8) if ablate == "drift" else (1 - rho, 1 + rho)
        self.machines = {m: Machine(m, rate=self.rng.uniform(lo, hi)) for m in CARRIERS}
        self.arb_rate = self.rng.uniform(1 - rho, 1 + rho)
        self.arb_offset = 1000.0
        self.dir = Path(tempfile.mkdtemp(prefix="cap-fuzz-"))
        self.mail: list[tuple[float, str, int, str, Any]] = []     # (deliver at, machine, its start, kind, payload)
        self.next_free: dict[str, float] = {}                       # per stream: the last delivery time scheduled
        self.last_hb: dict[str, float] = {}
        self.notified: dict[str, int] = {}                          # the grant epoch each machine was last told of
        self.n_rid = 0
        # each run draws its own mix of actions, so that quiet and busy fleets, rare and frequent restarts are tried
        base = {"time": 18, "observe": 14, "request": self.rng.choice((2, 6, 12)), "permit": 12, "enter": 14,
                "move": 10, "release": self.rng.choice((3, 6, 10)), "handover": 4, "link": 6, "mrestart": 1,
                "arestart": self.rng.choice((0.3, 1.0, 2.0))}
        self.actions = list(base)
        self.weights = [w * self.rng.uniform(0.5, 1.5) for w in base.values()]
        self.violations: list[Violation] = []
        self.trace: list[str] = []
        self.arb = self._new_arbiter()

    # ------------------------------------------------------------------ arbiter
    def _arb_clock(self) -> float:
        return self.arb_offset + self.arb_rate * self.real

    def _new_arbiter(self) -> OccupancyArbiter:
        arb = OccupancyArbiter(self.scheme, clock=self._arb_clock, journal=OccupancyJournal(self.dir, sync=False),
                               envelope=ENVELOPE, boot_id=self.rng.getrandbits(32))
        if BAY not in arb.zones:
            arb.set_zone(ZONE)
        self.last_hb = {}
        return arb

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    # ------------------------------------------------------------------ messages
    def _send(self, m: str, kind: str, payload: Any, delay: float | None = None) -> None:
        """Over m's stream: delayed, possibly lost if the link is down when it arrives, but never overtaken by a
        later message on the same stream."""
        d = self.rng.choice((0.0, 0.0, 0.5, 3.0, 12.0)) if delay is None else delay
        at = max(self.real + d, self.next_free.get(m, 0.0))
        self.next_free[m] = at
        self.mail.append((at, m, self.machines[m].start, kind, payload))

    def _deliver(self) -> None:
        due = sorted((x for x in self.mail if x[0] <= self.real + 1e-9), key=lambda x: x[0])
        self.mail = [x for x in self.mail if x[0] > self.real + 1e-9]
        for _t, m, start, kind, payload in due:
            mc = self.machines[m]
            if not mc.link or mc.start != start:
                continue                                    # lost on a cut link, or meant for an earlier start
            if kind == "permit":
                mc.permits.append(payload)
            elif kind == "grant":
                mc.believes, mc.rid = payload
            elif kind == "lost":
                mc.permits.clear()
                mc.believes = None

    def _notify_change(self, before: str | None) -> None:
        """The Site tells a machine that it lost the bay; a gate that admits on a remembered grant is told of a grant
        only when the arbiter would let the holder in - no incompatible machine in custody, no incompatible permit
        counted - as the model's IssueAuth: after a hand-over the new holder waits for the sender's vacancy."""
        arb = self.arb
        after = arb.holder_of(BAY)
        if before is not None and after != before:
            self._send(before, "lost", None)
            self.notified.pop(before, None)
        if (self.scheme.gate == "local" and self.scheme.arb != "none" and after is not None
                and self.machines[after].link and arb._blocked_for(BAY, after) is None):     # noqa: SLF001
            ep = arb.epoch(arb._res(BAY).gen)                                             # noqa: SLF001
            if self.notified.get(after) != ep:
                self._send(after, "grant", (ep, arb.reservation_id_of(BAY)))
                self.notified[after] = ep

    # ------------------------------------------------------------------ actions
    def _rid(self) -> str:
        self.n_rid += 1
        return f"r{self.n_rid}"

    def act(self, step: int) -> None:
        rng, arb = self.rng, self.arb
        m = rng.choice(CARRIERS)
        mc = self.machines[m]
        what = rng.choices(self.actions, weights=self.weights)[0]
        before = arb.holder_of(BAY)
        if what == "time":
            self.real += rng.choice((0.2, 1.0, 3.0, 8.0, 20.0))
        elif what == "observe":
            seen = {k: v.pos for k, v in self.machines.items()}
            if self.ablate == "sensing":                     # A2 broken: a machine in the bay is at times seen far out
                seen = {k: ("far" if p == "in" and rng.random() < 0.2 else p) for k, p in seen.items()}
            arb.observe({k: (POSE[p], 0.0) for k, p in seen.items()})
        elif what == "request":
            rid = self._rid()
            st = arb.request(site_agent_pb2.ReservationRequest(reservation_id=rid, resource_id=BAY, holder_id=m))
            if st.state == GRANTED and before != m:         # a new grant, made only when the bay is clear
                self._send(m, "grant", (int(st.grant_epoch), rid), delay=0.0)
                self.notified[m] = int(st.grant_epoch)
        elif what == "permit":
            if mc.link:
                t_sent = mc.clock(self.real)
                st = arb.request(site_agent_pb2.ReservationRequest(reservation_id=mc.rid or self._rid(),
                                                                   resource_id=BAY, holder_id=m,
                                                                   reason=PERMIT_REASON))
                if st.state == GRANTED and st.reason == PERMIT_REASON:
                    self._send(m, "permit", (int(st.grant_epoch), t_sent))
        elif what == "enter":
            self._enter(m, step)
        elif what == "move":
            mc.pos = {"in": rng.choice(("near", "far")), "near": rng.choice(("far", "far", "near")),
                      "far": "near"}[mc.pos]
        elif what == "release":
            self._release(m)
        elif what == "handover":
            self._handover(m)
        elif what == "link":
            mc.link = not mc.link
        elif what == "mrestart":
            mc.start += 1
            mc.offset = rng.uniform(0.0, 500.0)
            mc.permits.clear()
            mc.believes, mc.rid = None, None
        elif what == "arestart":
            lose = self.journal_mode == "lost" or (self.journal_mode == "mixed" and rng.random() < 0.5)
            if lose:
                (self.dir / "occupancy_journal.jsonl").unlink(missing_ok=True)
            self.arb_offset = rng.uniform(0.0, 5000.0)
            self.arb = arb = self._new_arbiter()
            if self.ablate == "guard" and not lose and arb.guard != "open":
                arb.guard = "open"                           # the rule before 2026-11-13: the journal taken as it stood
            before = None                                    # a restarted arbiter sends no notice of the past
        self.trace.append(f"{step}:{what}:{m}")
        self._housekeeping(before)
        self._check(step)

    def _enter(self, m: str, step: int) -> None:
        mc = self.machines[m]
        if mc.pos == "in":
            return
        g = self.scheme.gate
        authority: int | None = None
        if g == "permit":
            life = self.scheme.permit_ttl_s * (1.0 - self.rho)
            usable = [p for p in mc.permits if mc.clock(self.real) - p[1] < life]
            if not usable:
                return
            authority = usable[0][0]
            mc.permits.remove(usable[0])                     # a permit serves for one entry
        elif g == "local":
            if mc.believes is None:
                return
            authority = mc.believes
        if g == "permit":
            ok = (m, authority) in self.arb.live_permits(BAY) or self.arb.guard == "waitTime"
        else:
            ok = g == "none" or self.arb.holder_of(BAY) == m
        mc.pos = "in"
        if not ok:
            self.violations.append(Violation("S3", step, f"{m} entered on an authorisation the arbiter no longer "
                                                         f"accounts for (holder {self.arb.holder_of(BAY)})"))

    def _release(self, m: str) -> None:
        arb, mc = self.arb, self.machines[m]
        holder = arb.holder_of(BAY)
        if self.env == "own":
            if holder != m or not mc.link:
                return
            arb.release(site_agent_pb2.ReservationRelease(reservation_id=arb.reservation_id_of(BAY) or "",
                                                          reason="own"))
            self._own_give_up(m)
            return
        # any client, in any name: the holder's reservation, or one that is no longer anyone's
        rid = arb.reservation_id_of(BAY) if (holder is not None and self.rng.random() < 0.8) else self._rid()
        arb.release(site_agent_pb2.ReservationRelease(reservation_id=rid or "", reason="site"))

    def _own_give_up(self, m: str) -> None:
        """m's own agent gives the bay up: its gate forgets its authority, and an authorisation still on its way to
        it is discarded on arrival (OwnGiveUp in OccupancyHandover.tla)."""
        mc = self.machines[m]
        mc.permits.clear()
        mc.believes = None
        self.notified.pop(m, None)
        self.mail = [x for x in self.mail if not (x[1] == m and x[3] in ("grant", "permit"))]

    def _handover(self, m: str) -> None:
        arb = self.arb
        sender = arb.holder_of(BAY)
        if sender is None or sender == m:
            return
        if self.env == "own":
            smc = self.machines[sender]
            if not smc.link:
                return
            if arb.transfer_grant(BAY, m, self._rid()) is not None:
                self._own_give_up(sender)
            return
        arb.transfer_grant(BAY, m, self._rid())

    def _housekeeping(self, before: str | None) -> None:
        arb = self.arb
        if BAY not in arb.zones:
            arb.set_zone(ZONE)                               # the coordinator registers the bay again
        t = self._arb_clock()
        for k, mc in self.machines.items():
            if mc.link:
                self.last_hb[k] = t                          # the arbiter hears from every linked machine
                if not arb.table_withheld:
                    arb.acknowledge_table(k, arb.boot, arb.table_version)
        arb.tick(last_heartbeat=dict(self.last_hb))
        self._notify_change(before)
        self._deliver()

    # ------------------------------------------------------------------ oracle
    def _check(self, step: int) -> None:
        inside = sorted(k for k, mc in self.machines.items() if mc.pos == "in")
        if len(inside) > 1:
            self.violations.append(Violation("S1", step, f"{' and '.join(inside)} in the bay"))
        if self.arb.guard == "open":
            accounted = {self.arb.holder_of(BAY)} | self.arb.custody_of(BAY)
            for k in inside:
                if k not in accounted:
                    self.violations.append(Violation("S2", step, f"{k} in the bay, neither holder nor in custody"))

    def run(self) -> list[Violation]:
        try:
            for step in range(self.steps):
                self.act(step)
                if self.violations:
                    break
        finally:
            self.close()
        return self.violations


# ---------------------------------------------------------------------------- campaign
def fuzz(key: str, *, runs: int, steps: int, env: str, journal: str, base_seed: int = 0,
         ablate: str = "") -> dict[str, Any]:
    for i in range(runs):
        w = World(key, seed=base_seed + i, env=env, journal=journal, steps=steps, ablate=ablate)
        v = w.run()
        if v:
            return {"scheme": key, "safe": False, "seed": base_seed + i, "violation": v[0].prop,
                    "detail": v[0].detail, "step": v[0].step, "trace": w.trace[-12:]}
    return {"scheme": key, "safe": True, "runs": runs, "steps": steps}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--steps", type=int, default=300)
    ap.add_argument("--env", choices=("site", "own"), default="site")
    ap.add_argument("--journal", choices=("kept", "lost", "mixed"), default="kept")
    ap.add_argument("--schemes", default="all", help="all | named | safe (the model's safe ones) | comma-separated keys")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--ablate", choices=("", "custody", "drift", "sensing", "guard"), default="",
                    help="remove the custody rule's second half, let machine clocks drift beyond rho (A4), or let "
                         "the Site see a machine in the bay as far out (A2); the model has no safe scheme then")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    keys = (family() if args.schemes == "all" else list(NAMED.values()) if args.schemes == "named"
            else [k for k in family() if expected_safe(k, args.env, args.journal)] if args.schemes == "safe"
            else args.schemes.split(","))
    rows, agree = [], 0
    for key in keys:
        res = fuzz(key, runs=args.runs, steps=args.steps, env=args.env, journal=args.journal, base_seed=args.seed,
                   ablate=args.ablate)
        res["model_safe"] = expected_safe(key, args.env, args.journal) and not args.ablate
        res["agrees"] = res["safe"] == res["model_safe"]
        agree += res["agrees"]
        rows.append(res)
        tag = "safe" if res["safe"] else f"{res['violation']} (seed {res['seed']}, step {res['step']})"
        print(f"{key:45s} {tag:28s} model: {'safe' if res['model_safe'] else 'unsafe'}"
              f"{'' if res['agrees'] else '   <-- differs'}")
    n_safe = sum(r["safe"] for r in rows)
    print(f"{args.env} releases, journal {args.journal}{', ablation ' + args.ablate if args.ablate else ''}: "
          f"{n_safe} of {len(rows)} schemes without a violation in {args.runs} runs of {args.steps} steps; "
          f"{agree} of {len(rows)} agree with the model checker")
    if args.json:
        Path(args.json).write_text(json.dumps({"env": args.env, "journal": args.journal, "ablate": args.ablate,
                                               "runs": args.runs, "steps": args.steps, "seed": args.seed,
                                               "rows": rows}, indent=1))
    return 0 if agree == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
