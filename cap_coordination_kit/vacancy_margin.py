"""How likely is a false vacancy confirmation, given the margin of the vacancy check and the spread of the position
error?

The arbiter confirms a machine out of a circular zone (radius R) when its REPORTED position lies at least m outside
the boundary. With a report = true position + e, e Gaussian with standard deviation sigma on each axis, and the
machine truly at distance r from the zone's centre, one report confirms it out with probability

    q(r) = P(|p + e| >= R + m) = Q_1(r / sigma, (R + m) / sigma)

(the Marcum Q-function of order one: the squared reported distance over sigma^2 is non-central chi-squared with two
degrees of freedom). A machine observed n times at distances r_1..r_n while it is in custody is confirmed out at
least once with probability 1 - prod_k (1 - q(r_k)) if the errors are independent.

Three facts follow, and the paper states them as a theorem: with a bounded error (|e| <= eps) a margin m < eps
confirms some machine that is inside; m >= eps + v_max * (age + latency) never does; and with an unbounded error no
margin gives certainty, but the risk is this formula - computable before a machine moves.

No dependency: Q_1 is computed by quadrature over the tangential error.
"""

from __future__ import annotations

import math
from collections.abc import Iterable


def false_vacancy_probability(r_m: float, zone_radius_m: float, margin_m: float, sigma_m: float, *,
                              steps: int = 400) -> float:
    """q(r): the probability that ONE report on a machine at true distance ``r_m`` from the zone's centre lies at
    least ``margin_m`` outside a zone of radius ``zone_radius_m``, for a Gaussian error of ``sigma_m`` per axis."""
    c = zone_radius_m + margin_m
    if sigma_m <= 0.0:
        return 1.0 if r_m >= c else 0.0
    if steps % 2:
        steps += 1
    lim = 8.0 * sigma_m
    h = 2.0 * lim / steps
    root2 = sigma_m * math.sqrt(2.0)
    total = 0.0
    for i in range(steps + 1):
        et = -lim + i * h                                           # the error across the radial direction
        w = (1.0 if i in (0, steps) else (4.0 if i % 2 else 2.0)) * h / 3.0
        dens = math.exp(-0.5 * (et / sigma_m) ** 2) / (sigma_m * math.sqrt(2.0 * math.pi))
        if abs(et) >= c:
            p = 1.0
        else:
            a = math.sqrt(c * c - et * et)                          # the radial coordinate must be >= a or <= -a
            p = 0.5 * math.erfc((a - r_m) / root2) + 0.5 * math.erfc((a + r_m) / root2)
        total += w * dens * p
    return min(1.0, max(0.0, total))


def episode_false_confirmation_probability(distances_m: Iterable[float], zone_radius_m: float, margin_m: float,
                                           sigma_m: float) -> float:
    """The probability that a machine observed at these true distances (all inside the zone) is confirmed out by at
    least one of the reports, for independent errors."""
    survive = 1.0
    for r in distances_m:
        survive *= 1.0 - false_vacancy_probability(r, zone_radius_m, margin_m, sigma_m, steps=120)
    return 1.0 - survive


def necessary_margin_m(eps_m: float) -> float:
    """With an error of any value up to eps, a smaller margin confirms some machine that is inside."""
    return eps_m


def sufficient_margin_m(eps_m: float, v_max_mps: float, max_age_s: float, latency_s: float) -> float:
    """With this margin the machine is outside when the confirmation is made, whatever it did since the report."""
    return eps_m + v_max_mps * (max_age_s + latency_s)


def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--radius", type=float, default=9.0)
    ap.add_argument("--margin", type=float, default=2.225, help="eps + v_max * latency + braking distance [m]")
    ap.add_argument("--sigma", type=float, nargs="+", default=[0.5, 1.5])
    ap.add_argument("--depth", type=float, nargs="+", default=[0.25, 0.5, 0.75, 1.0, 2.0],
                    help="how far inside the boundary the machine stands [m]")
    ap.add_argument("--observations", type=int, default=20)
    args = ap.parse_args()
    print(f"zone radius {args.radius} m, margin {args.margin} m")
    for sigma in args.sigma:
        for d in args.depth:
            q = false_vacancy_probability(args.radius - d, args.radius, args.margin, sigma)
            n = episode_false_confirmation_probability([args.radius - d] * args.observations, args.radius,
                                                       args.margin, sigma)
            print(f"  sigma {sigma:4.2f} m, {d:4.2f} m inside: {q:.2e} per report / {n:.3f} over "
                  f"{args.observations} reports")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
