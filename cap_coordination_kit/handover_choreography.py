"""Handover-choreography model: the operational consequence of P4.

The TLA+ result (cap-spec/formal/tla/CAPCoordination, HandoverReceiverOwnership)
proves that an *atomic* ownership transfer keeps the receiver the unique holder
throughout a handover, whereas a naive release-then-reacquire admits a window in
which the handed-over resource is not owned by the receiver. This module shows
the *runtime* consequence of that distinction for a heterogeneous load-point
handover (excavator -> carrier), and quantifies the gap a reviewer asks about:
"why is CAP's typed handover better than a plain mutual-exclusion lock?"

A plain mutex guarantees **lock** exclusivity (at most one lock holder) but not
**physical** exclusivity during a handover, because it has no vacate-before-enter
choreography: the sender releases the lock when it *decides* to hand over, the
receiver acquires and moves in immediately, and during the sender's residual
vacate latency both machines occupy the load point. CAP's typed handover gates
the receiver's entry on the sender's confirmed vacate (atomic ownership
transfer), so the load point is never co-occupied.

This is a deterministic discrete-tick model (no LLM / physics), in the same
spirit as the fault-injection campaign's scripted agents + kinematic mock; the
injected fault is the sender's *vacate latency* (a sender slow to clear the load
point) and an optional sender *reentry* attempt during the handover.

Two disciplines:
  - ATOMIC (CAP typed handover): receiver enters only after the sender has
    vacated; a sender reentry during the in-progress handover is rejected
    (the receiver already owns the point). Physical occupancy <= 1 always.
  - NAIVE  (plain mutex, release-then-reacquire): receiver enters as soon as it
    acquires the freed lock; the sender remains physically present for its
    vacate latency.  Co-occupancy occurs whenever vacate latency > 0.

`run_sweep` enumerates interleavings (vacate latency x receiver eagerness x
reentry) and reports, per discipline, the fraction of interleavings with load-
point co-occupancy and the worst-case simultaneous occupancy.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product


@dataclass(frozen=True)
class Scenario:
    """One handover interleaving."""

    vacate_latency: int      # ticks the sender stays physically present after deciding to hand over
    receiver_delay: int      # ticks the receiver waits after acquiring before entering
    sender_reentry: bool     # sender attempts to re-acquire the load point during the handover


@dataclass(frozen=True)
class Outcome:
    co_occupied: bool        # two machines physically on the load point at some tick
    max_occupancy: int       # worst-case simultaneous physical occupants
    receiver_got_point: bool # the intended receiver ended up owning the point


def _simulate(scn: Scenario, *, atomic: bool) -> Outcome:
    """Discrete-tick simulation of a single excavator->carrier handover.

    Occupancy is the set of machines physically on the load point at a tick.
    The horizon is short and fixed; the model is deterministic in `scn`.
    """
    SENDER, RECEIVER = "exc", "carrier"
    horizon = scn.vacate_latency + scn.receiver_delay + 3
    occupants_over_time: list[set[str]] = []

    if atomic:
        # CAP: ownership transfers atomically; the receiver enters ONLY after the
        # sender has confirmed vacate. Sender reentry during the in-progress
        # handover is rejected (the receiver already owns the point).
        sender_vacated_at = scn.vacate_latency           # sender present on [0, vacate_latency)
        receiver_enters_at = sender_vacated_at + scn.receiver_delay
        for t in range(horizon):
            occ: set[str] = set()
            if t < sender_vacated_at:
                occ.add(SENDER)
            if t >= receiver_enters_at:
                occ.add(RECEIVER)
            # reentry rejected: sender cannot re-enter once it has vacated for the handover
            occupants_over_time.append(occ)
        receiver_got = True
    else:
        # NAIVE mutex: sender releases the lock at decision time (t=0) but is still
        # physically present for `vacate_latency` ticks; the receiver acquires the
        # freed lock and enters after `receiver_delay`. No vacate-before-enter gate.
        sender_present_until = scn.vacate_latency        # sender present on [0, vacate_latency)
        receiver_enters_at = scn.receiver_delay          # receiver enters as soon as it acquires + delay
        for t in range(horizon):
            occ = set()
            if t < sender_present_until:
                occ.add(SENDER)
            if t >= receiver_enters_at:
                occ.add(RECEIVER)
            # reentry: in the naive discipline the sender may re-acquire the lock in
            # the free window between its release and the receiver's acquire; model a
            # brief sender re-presence if it reenters before the receiver entered.
            if scn.sender_reentry and t >= sender_present_until and t < receiver_enters_at:
                occ.add(SENDER)
            occupants_over_time.append(occ)
        receiver_got = True

    max_occ = max((len(o) for o in occupants_over_time), default=0)
    return Outcome(
        co_occupied=max_occ > 1,
        max_occupancy=max_occ,
        receiver_got_point=receiver_got,
    )


def run_sweep(
    *,
    max_vacate_latency: int = 4,
    max_receiver_delay: int = 2,
) -> dict[str, dict]:
    """Enumerate handover interleavings and summarise co-occupancy per discipline.

    Returns a dict keyed by discipline ("atomic", "naive") with the count of
    interleavings, co-occupancy count/rate, and worst-case occupancy.
    """
    scenarios = [
        Scenario(vacate_latency=v, receiver_delay=d, sender_reentry=r)
        for v, d, r in product(
            range(1, max_vacate_latency + 1),   # vacate_latency >= 1 (a sender always takes some time to clear)
            range(0, max_receiver_delay + 1),
            (False, True),
        )
    ]
    summary: dict[str, dict] = {}
    for label, atomic in (("atomic", True), ("naive", False)):
        co = 0
        worst = 0
        for scn in scenarios:
            out = _simulate(scn, atomic=atomic)
            co += int(out.co_occupied)
            worst = max(worst, out.max_occupancy)
        n = len(scenarios)
        summary[label] = {
            "interleavings": n,
            "co_occupied": co,
            "co_occupancy_rate": round(co / n, 4) if n else 0.0,
            "worst_case_occupancy": worst,
        }
    return summary


def format_summary(summary: dict[str, dict]) -> str:
    lines = ["Handover-choreography co-occupancy (operational P4):"]
    for label in ("atomic", "naive"):
        s = summary[label]
        disc = "CAP atomic typed handover" if label == "atomic" else "plain mutex (release-then-reacquire)"
        lines.append(
            f"  {label:6s} [{disc}]: "
            f"{s['co_occupied']}/{s['interleavings']} interleavings co-occupied "
            f"(rate {s['co_occupancy_rate']}), worst-case occupancy {s['worst_case_occupancy']}"
        )
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    print(format_summary(run_sweep()))
