# Copied from the paper's scripts (vacancy_design.py) - the design table of the vacancy margin.
"""How much margin a vacancy check needs against position error, and what consecutive looks buy.

The arbiter confirms a machine out of a zone when ``looks`` consecutive position reports each show it out by the
margin. The worst case for position error is a machine at the boundary: a report shows it out when the outward
component e of its error is at least the allowance eps the margin leaves for error. With e Gaussian (standard
deviation sigma) and its values at two consecutive looks correlated with coefficient ``corr`` (first-order
autoregressive, stationary), this script computes

    P(n, k, a, corr) = P(some k consecutive of n looks all have e >= a * sigma),   a = eps / sigma,

and solves it for the allowance a that keeps P at a target. Independent looks (corr = 0) have the closed recursion
of p_independent below; correlated looks are integrated on a grid (the density of e jointly with the
length of the current run, one look at a time). Both are checked against each other at corr = 0 and against a
simulation at moderate probabilities (``--selfcheck``).

  python -m cap_coordination_kit.vacancy_design --selfcheck
  python -m cap_coordination_kit.vacancy_design [--tex table.tex] [--json rows.json]
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

import numpy as np

GRID_HALF = 9.0          # the grid of e / sigma covers [-9, 9]
GRID_DX = 0.01           # cells of this width, one cell edge exactly at the threshold


def tail(a: float) -> float:
    return 0.5 * math.erfc(a / math.sqrt(2.0))


def p_independent(n: int, k: int, a: float) -> float:
    q = tail(a)
    run = [1.0] + [0.0] * (k - 1)
    done = 0.0
    for _ in range(n):
        done += run[k - 1] * q
        run = [sum(run) * (1.0 - q)] + [run[j - 1] * q for j in range(1, k)]
    return done


class Grid:
    """The error between two looks on a grid of cells (corr fixed). The cells are laid so that the threshold is a cell
    edge - a cell is then wholly 'out' or wholly not - and each cell is represented by its centre (midpoint rule)."""

    def __init__(self, corr: float) -> None:
        self.corr = corr

    def p(self, n: int, k: int, a: float) -> float:
        lo, hi = math.floor((-GRID_HALF - a) / GRID_DX), math.ceil((GRID_HALF - a) / GRID_DX)
        x = a + (np.arange(lo, hi) + 0.5) * GRID_DX
        out = x > a
        s2 = 1.0 - self.corr * self.corr
        d = x[None, :] - self.corr * x[:, None]                  # row: from, column: to
        kernel = np.exp(-0.5 * d * d / s2)
        kernel /= kernel.sum(axis=1, keepdims=True)              # each row a distribution on the grid
        start = np.exp(-0.5 * x * x)
        start /= start.sum()
        f = np.zeros((k, x.size))                                # f[j]: mass with the current run j long, unconfirmed
        done = 0.0
        if k == 1:
            done += float(start[out].sum())
        else:
            f[1] = np.where(out, start, 0.0)
        f[0] = np.where(out, 0.0, start)
        for _ in range(n - 1):
            moved = f @ kernel
            nxt = np.zeros_like(f)
            nxt[0] = np.where(out, 0.0, moved.sum(axis=0))
            into = np.where(out[None, :], moved, 0.0)
            done += float(into[k - 1].sum())
            nxt[1:] = into[:-1]
            f = nxt
        return done


def solve(fn, target: float, lo: float = 0.0, hi: float = 8.5) -> float:
    """The allowance a with fn(a) = target (fn decreasing in a)."""
    for _ in range(24):
        mid = 0.5 * (lo + hi)
        if fn(mid) > target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def simulate(n: int, k: int, a: float, corr: float, reps: int, rng: random.Random) -> float:
    w, hits = math.sqrt(1.0 - corr * corr), 0
    for _ in range(reps):
        e, run = rng.gauss(0.0, 1.0), 0
        for i in range(n):
            if i:
                e = corr * e + w * rng.gauss(0.0, 1.0)
            if e >= a:
                run += 1
                if run >= k:
                    hits += 1
                    break
            else:
                run = 0
    return hits / reps


def selfcheck() -> int:
    ok = True
    g0 = Grid(0.0)
    for (n, k, a) in ((60, 1, 2.0), (60, 2, 1.5), (60, 3, 1.0), (20, 3, 2.5), (60, 2, 3.5)):
        exact, grid = p_independent(n, k, a), g0.p(n, k, a)
        good = abs(grid - exact) <= 0.01 * exact + 1e-15
        ok &= good
        print(f"corr 0    n={n} k={k} a={a}: recursion {exact:.4e} grid {grid:.4e} {'ok' if good else 'DIFFERS'}")
    rng = random.Random(20261112)
    for corr in (0.5, 0.9, 0.99):
        g = Grid(corr)
        for (n, k, a) in ((60, 1, 2.0), (60, 3, 1.5), (60, 3, 2.0)):
            grid, sim = g.p(n, k, a), simulate(n, k, a, corr, 40000, rng)
            sd = math.sqrt(max(sim * (1.0 - sim), 1e-12) / 40000)
            good = abs(grid - sim) <= 4.0 * sd + 0.02 * grid
            ok &= good
            print(f"corr {corr:<4} n={n} k={k} a={a}: grid {grid:.4e} simulation {sim:.4e} (sd {sd:.1e}) "
                  f"{'ok' if good else 'DIFFERS'}")
    print("selfcheck", "passed" if ok else "FAILED")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--looks-per-episode", type=int, default=60)
    ap.add_argument("--targets", default="1e-3,1e-6")
    ap.add_argument("--ks", default="1,2,3,5")
    ap.add_argument("--corrs", default="0,0.5,0.9,0.99")
    ap.add_argument("--tex")
    ap.add_argument("--json")
    args = ap.parse_args()
    if args.selfcheck:
        return selfcheck()
    n = args.looks_per_episode
    targets = [float(t) for t in args.targets.split(",")]
    ks = [int(k) for k in args.ks.split(",")]
    corrs = [float(c) for c in args.corrs.split(",")]
    rows = []
    for corr in corrs:
        g = Grid(corr) if corr > 0.0 else None
        for target in targets:
            for k in ks:
                fn = (lambda a, k=k: p_independent(n, k, a)) if g is None else (lambda a, k=k, g=g: g.p(n, k, a))
                a = solve(fn, target)
                rows.append({"corr": corr, "target": target, "looks": k, "allowance_sigma": round(a, 2),
                             "p_at_allowance": fn(a), "looks_per_episode": n})
                print(json.dumps(rows[-1]))
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1))
    if args.tex:
        def cell(corr: float, target: float, k: int) -> str:
            return next(f"{r['allowance_sigma']:.1f}" for r in rows
                        if r["corr"] == corr and r["target"] == target and r["looks"] == k)
        lines = ["% generated by cap_coordination_kit.vacancy_design - do not edit",
                 "\\begin{tabular}{@{}l" + "r" * (len(ks) * len(targets)) + "@{}}", "\\toprule",
                 "Correlation of the error & " + " & ".join(
                     f"\\multicolumn{{{len(ks)}}}{{c}}{{$p^\\ast = 10^{{{round(math.log10(t))}}}$}}" for t in targets)
                 + " \\\\",
                 "between two looks & " + " & ".join(f"$k={k}$" for _ in targets for k in ks) + " \\\\", "\\midrule"]
        for corr in corrs:
            lines.append(f"{corr:g} & " + " & ".join(cell(corr, t, k) for t in targets for k in ks) + " \\\\")
        lines += ["\\bottomrule", "\\end{tabular}"]
        Path(args.tex).write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
