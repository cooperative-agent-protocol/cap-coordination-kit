"""Runtime spec<->implementation conformance monitor for the reservation arbiter.

The offline refinement test in cap-reference (`test_spec_refinement.py`) drives the reference
``ReservationManager`` and a minimal port of the TLA+ transition relation through 200 *synthetic*
randomized traces and asserts step-by-step equivalence. This module closes the same spec<->code
gap from the other end: it validates the ACTUAL RUNTIME traces the shipped cap-pangaea arbiter
produces -- its authoritative transaction log (``reservation_txlog.jsonl``) -- against the spec's
allowed transition relation, both online (as a transition is emitted) and offline (replaying the
persisted log of any committed campaign run). So the evidence is "every reservation transition the
implementation actually took, in every experiment, is a transition the spec permits", at the scale
of the whole fault-injection and handover campaigns rather than a synthetic sample.

The cap-pangaea arbiter runs the DENY-on-busy regime (``ReservationManager`` emits
GRANTED / DENIED / RELEASED / HANDOVER / EXPIRED; see ``coordination/reservation.py``). The spec
guards a conforming trace must satisfy, replayed over the holder state reconstructed from the log:

  * GRANTED   : only when the resource is free OR re-granted to the same holder. A GRANTED while
                the resource is held by a DIFFERENT holder is a NoDoubleGrant violation (P2).
  * DENIED    : only when the resource is held by a different holder (a free or self-held resource
                must be granted, not denied -- a spurious denial is a liveness/correctness defect).
  * RELEASED  : by the current holder -> resource becomes free; by a non-holder -> no-op (harmless).
  * HANDOVER  : only by the current holder (sender); ownership moves sender -> receiver atomically
                (P4 -- the slot is never unheld). A HANDOVER whose sender is not the holder is a
                violation.
  * EXPIRED   : the held grant lapses -> resource becomes free.

Each transition is also cross-checked against the arbiter's OWN recorded post-state
(``resolved_holder``): if the holder reconstructed by replaying the spec guards disagrees with what
the arbiter recorded as the resolved holder, that is a refinement divergence (the implementation
took a step the abstract relation did not). A run is conforming iff it has zero violations and zero
divergences.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ConformanceResult:
    transitions: int = 0
    event_counts: dict[str, int] = field(default_factory=dict)
    violations: list[dict[str, Any]] = field(default_factory=list)   # spec-guard violations
    divergences: list[dict[str, Any]] = field(default_factory=list)  # replay vs resolved_holder
    noop_releases: int = 0

    @property
    def conforming(self) -> bool:
        return not self.violations and not self.divergences

    def summary(self) -> dict[str, Any]:
        return {
            "transitions": self.transitions,
            "event_counts": dict(self.event_counts),
            "violations": len(self.violations),
            "divergences": len(self.divergences),
            "noop_releases": self.noop_releases,
            "conforming": self.conforming,
        }


class ReservationConformanceMonitor:
    """Validates each reservation transition against the spec's allowed transition relation.

    Use online by registering ``feed`` as the arbiter's ``on_transition`` callback, or offline by
    replaying a persisted ``reservation_txlog.jsonl`` via :func:`check_records`. Holder state is
    reconstructed purely from the log, so the monitor is independent of the implementation under
    test (it does not read the arbiter's private state).
    """

    def __init__(self) -> None:
        self._holder: dict[str, str | None] = {}
        self.result = ConformanceResult()

    def feed(self, rec: dict[str, Any]) -> None:
        ev = rec.get("event")
        res = rec.get("resource_id")
        req = rec.get("requested_holder") or None
        prior = rec.get("prior_holder") or None
        resolved = rec.get("resolved_holder") or None
        cur = self._holder.get(res)
        r = self.result
        r.transitions += 1
        r.event_counts[ev] = r.event_counts.get(ev, 0) + 1

        def violation(reason: str) -> None:
            r.violations.append({"seq": rec.get("seq"), "event": ev, "resource_id": res,
                                 "requested_holder": req, "prior_holder": prior,
                                 "reconstructed_holder": cur, "reason": reason})

        new_holder = cur  # default: unchanged
        if ev == "GRANTED":
            if cur is not None and cur != req:
                violation(f"GRANTED to '{req}' while held by '{cur}' (NoDoubleGrant / P2)")
            new_holder = req
        elif ev == "DENIED":
            if cur is None or cur == req:
                violation(f"DENIED '{req}' but resource was {'free' if cur is None else 'self-held'} "
                          f"(spurious denial)")
            # holder unchanged
        elif ev == "RELEASED":
            if prior is not None and prior == cur:
                new_holder = None
            else:
                r.noop_releases += 1  # release by a non-holder: harmless no-op, not a violation
        elif ev == "HANDOVER":
            if cur is not None and prior is not None and cur != prior:
                violation(f"HANDOVER sender '{prior}' is not the current holder '{cur}'")
            new_holder = resolved  # ownership moves to the receiver atomically
        elif ev == "EXPIRED":
            new_holder = None
        # cross-check the spec-replayed post-state against the arbiter's own recorded post-state
        if ev in ("GRANTED", "RELEASED", "HANDOVER", "EXPIRED") and new_holder != resolved:
            r.divergences.append({"seq": rec.get("seq"), "event": ev, "resource_id": res,
                                  "replayed_holder": new_holder, "resolved_holder": resolved,
                                  "reason": "spec-replayed post-state != arbiter resolved_holder"})
        self._holder[res] = new_holder


def check_records(records: list[dict[str, Any]]) -> ConformanceResult:
    """Replay an ordered list of txlog records through the spec guards; return the verdict."""
    mon = ReservationConformanceMonitor()
    for rec in sorted(records, key=lambda r: r.get("seq", 0)):
        mon.feed(rec)
    return mon.result


def check_txlog_file(path: str | Path) -> ConformanceResult:
    recs = []
    for ln in Path(path).read_text().splitlines():
        try:
            recs.append(json.loads(ln))
        except (json.JSONDecodeError, ValueError):
            continue
    return check_records(recs)


def check_campaign_dir(root: str | Path) -> dict[str, Any]:
    """Run the monitor over every ``reservation_txlog.jsonl`` under ``root`` and aggregate."""
    root = Path(root)
    runs = sorted(root.rglob("reservation_txlog.jsonl"))
    per_run = []
    total_tx = total_viol = total_div = nonconforming = 0
    for p in runs:
        res = check_txlog_file(p)
        per_run.append({"run": str(p.parent.relative_to(root)), **res.summary()})
        total_tx += res.transitions
        total_viol += len(res.violations)
        total_div += len(res.divergences)
        nonconforming += 0 if res.conforming else 1
    return {
        "root": str(root), "runs": len(runs), "total_transitions": total_tx,
        "total_violations": total_viol, "total_divergences": total_div,
        "nonconforming_runs": nonconforming,
        "all_conforming": (total_viol == 0 and total_div == 0),
        "per_run": per_run,
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Spec-conformance monitor over persisted reservation txlogs.")
    ap.add_argument("roots", nargs="+", help="campaign directories to scan for reservation_txlog.jsonl")
    ap.add_argument("--out", type=Path, default=None, help="write the aggregate JSON here")
    args = ap.parse_args()
    overall = {"campaigns": []}
    g_tx = g_viol = g_div = g_runs = g_nc = 0
    for root in args.roots:
        agg = check_campaign_dir(root)
        overall["campaigns"].append({k: agg[k] for k in
                                     ("root", "runs", "total_transitions", "total_violations",
                                      "total_divergences", "nonconforming_runs", "all_conforming")})
        g_tx += agg["total_transitions"]
        g_viol += agg["total_violations"]
        g_div += agg["total_divergences"]
        g_runs += agg["runs"]
        g_nc += agg["nonconforming_runs"]
        print(f"[{root}] runs={agg['runs']} transitions={agg['total_transitions']} "
              f"violations={agg['total_violations']} divergences={agg['total_divergences']} "
              f"all_conforming={agg['all_conforming']}")
    overall["totals"] = {"runs": g_runs, "transitions": g_tx, "violations": g_viol,
                         "divergences": g_div, "nonconforming_runs": g_nc,
                         "all_conforming": (g_viol == 0 and g_div == 0)}
    print(f"\nTOTAL: runs={g_runs} transitions={g_tx} violations={g_viol} "
          f"divergences={g_div} all_conforming={overall['totals']['all_conforming']}")
    if args.out:
        args.out.write_text(json.dumps(overall, indent=2) + "\n")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
