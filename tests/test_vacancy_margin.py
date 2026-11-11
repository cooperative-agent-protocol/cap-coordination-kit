"""The false-vacancy formula: against a seeded Monte Carlo, and its limits."""

import math
import random

from cap_coordination_kit.vacancy_margin import (
    episode_false_confirmation_probability,
    false_vacancy_probability,
    necessary_margin_m,
    sufficient_margin_m,
)


def _monte_carlo(r, radius, margin, sigma, n=200_000, seed=7):
    rng = random.Random(seed)
    hit = 0
    for _ in range(n):
        if math.hypot(r + rng.gauss(0.0, sigma), rng.gauss(0.0, sigma)) >= radius + margin:
            hit += 1
    return hit / n


def test_formula_matches_monte_carlo():
    for (r, radius, margin, sigma) in ((8.5, 9.0, 1.0, 1.5), (8.0, 9.0, 2.2, 1.5), (2.0, 3.0, 0.5, 1.0)):
        q = false_vacancy_probability(r, radius, margin, sigma)
        mc = _monte_carlo(r, radius, margin, sigma)
        assert abs(q - mc) < 4.0 * math.sqrt(max(q * (1 - q), 1e-6) / 200_000) + 1e-4, (r, q, mc)


def test_positive_for_every_margin_and_falls_with_it():
    qs = [false_vacancy_probability(8.5, 9.0, m, 1.5) for m in (0.0, 1.0, 2.0, 4.0)]
    assert all(q > 0.0 for q in qs)
    assert qs == sorted(qs, reverse=True)


def test_no_error_no_false_confirmation():
    assert false_vacancy_probability(8.9, 9.0, 0.5, 0.0) == 0.0
    assert false_vacancy_probability(9.6, 9.0, 0.5, 0.0) == 1.0


def test_machine_far_outside_is_confirmed():
    assert false_vacancy_probability(30.0, 9.0, 2.0, 0.5) > 0.999


def test_episode_probability_grows_with_observations():
    one = episode_false_confirmation_probability([8.5], 9.0, 2.0, 1.5)
    many = episode_false_confirmation_probability([8.5] * 30, 9.0, 2.0, 1.5)
    assert 0.0 < one < many < 1.0
    assert abs(many - (1.0 - (1.0 - one) ** 30)) < 1e-9


def test_bounded_error_margins():
    eps = 0.5
    assert necessary_margin_m(eps) == eps
    # a machine 0.1 m inside with an outward error of 0.4 m (< eps) is reported 0.3 m outside: a margin of 0.3 m
    # (< eps) confirms it, a margin of eps does not
    reported_outside = 0.4 - 0.1
    assert reported_outside >= 0.3 and reported_outside < necessary_margin_m(eps)
    assert sufficient_margin_m(eps, 1.5, 1.0, 0.3) == eps + 1.5 * 1.3
