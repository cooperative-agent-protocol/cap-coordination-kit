"""Physical-exclusivity sensitivity surface of the handover discipline.

Generalises the single operational-P4 enumeration (see ``handover_choreography``)
into a sensitivity surface over the two physical-timing parameters the discipline
must tolerate:

  * vacate latency  -- ticks the SENDER remains physically on the load point after
                       deciding to hand over (a slow-to-clear machine);
  * receiver delay  -- ticks the RECEIVER waits after acquiring before entering.

For each (vacate latency, receiver delay) cell it reports the worst-case
simultaneous occupancy of the load point (over the sender-reentry axis) under a
plain release-then-reacquire mutex versus CAP's atomic vacate-before-enter
handover. Occupancy 2 is a physical-exclusivity violation.

This reproduces the paper's sensitivity table: a plain lock co-occupies in 20 of
30 cells (exactly the region where vacate latency > receiver delay), while CAP
keeps occupancy at 1 in every cell. The model is the deterministic tick model in
``handover_choreography`` -- it isolates the timing that an instant-actuation
kinematic mock cannot reproduce.

Run:  python3 -m cap_coordination_kit.sensitivity_surface
"""
from __future__ import annotations

from .handover_choreography import Scenario, _simulate

VACATE_LATENCY = [1, 2, 3, 4, 5, 6]
RECEIVER_DELAY = [0, 1, 2, 3, 4]


def _worst_occ(vac: int, dly: int, *, atomic: bool) -> int:
    """Worst-case load-point occupancy over the sender-reentry axis for this cell."""
    return max(
        _simulate(
            Scenario(vacate_latency=vac, receiver_delay=dly, sender_reentry=r),
            atomic=atomic,
        ).max_occupancy
        for r in (False, True)
    )


def build_surface() -> dict:
    """Compute the full sensitivity surface and its headline counts."""
    grid = {}
    naive_viol = atomic_viol = total = 0
    for v in VACATE_LATENCY:
        for d in RECEIVER_DELAY:
            n = _worst_occ(v, d, atomic=False)
            a = _worst_occ(v, d, atomic=True)
            grid[(v, d)] = {"naive_occ": n, "atomic_occ": a}
            total += 1
            naive_viol += int(n > 1)
            atomic_viol += int(a > 1)
    return {
        "grid": grid,
        "cells": total,
        "naive_co_occupied": naive_viol,
        "atomic_co_occupied": atomic_viol,
        "vacate_latency": VACATE_LATENCY,
        "receiver_delay": RECEIVER_DELAY,
    }


def format_surface(res: dict) -> str:
    dl = res["receiver_delay"]
    lines = [
        "Handover sensitivity surface (worst-case load-point occupancy under a plain mutex):",
        "  vacate\\delay  " + "  ".join(f"{d:>2d}" for d in dl),
    ]
    for v in res["vacate_latency"]:
        cells = "   ".join(str(res["grid"][(v, d)]["naive_occ"]) for d in dl)
        lines.append(f"  {v:>10d}    {cells}")
    lines.append(
        f"  mutex co-occupies {res['naive_co_occupied']}/{res['cells']} cells "
        f"(occupancy 2 = violation); CAP co-occupies {res['atomic_co_occupied']}/{res['cells']} "
        f"(occupancy 1 everywhere)."
    )
    return "\n".join(lines)


def main() -> int:
    print(format_surface(build_surface()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
