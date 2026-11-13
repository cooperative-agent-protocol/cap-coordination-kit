"""Randomised executions of the arbiter's code agree with the model checker on which schemes keep S1-S3.

A small budget, deterministic seeds: the safe schemes of each environment run without a violation, every other
scheme of a sample (and every one of the five named schemes) is caught, and each of the three assumptions the model
needs (the custody rule's second half, clocks within rho, sound vacancy sensing) breaks the protocol when removed.
``python -m cap_coordination_kit.family_fuzz`` runs the whole family with a larger budget."""

from __future__ import annotations

import pytest

from cap_coordination_kit.family_fuzz import NAMED, expected_safe, family, fuzz

RUNS, STEPS = 60, 300


def test_the_family_has_ninety_members_and_the_model_counts():
    keys = family()
    assert len(keys) == 90
    counts = {(e, j): sum(expected_safe(k, e, j) for k in keys) for e in ("site", "own") for j in ("kept", "lost")}
    assert counts == {("site", "kept"): 8, ("site", "lost"): 8, ("own", "kept"): 12, ("own", "lost"): 8}


@pytest.mark.parametrize("env,journal", [("site", "kept"), ("site", "lost"), ("own", "kept"), ("own", "lost")])
def test_the_safe_schemes_survive_and_the_named_unsafe_ones_are_caught(env, journal):
    for key in [k for k in family() if expected_safe(k, env, journal)]:
        res = fuzz(key, runs=RUNS, steps=STEPS, env=env, journal=journal)
        assert res["safe"], (key, res)
    for name, key in NAMED.items():
        if expected_safe(key, env, journal):
            continue
        res = fuzz(key, runs=RUNS, steps=STEPS, env=env, journal=journal)
        assert not res["safe"], (name, env, journal)


@pytest.mark.parametrize("ablate", ["custody", "drift", "sensing"])
def test_each_assumption_is_needed_by_the_protocol(ablate):
    res = fuzz(NAMED["cap"], runs=RUNS, steps=STEPS, env="site", journal="kept", ablate=ablate)
    assert not res["safe"], ablate


def test_restarts_that_each_keep_or_lose_the_journal_keep_the_safe_schemes_safe():
    """A restart may find the journal written during the wait after one that lost it. The journal records that the
    zones are closed, and the restart waits again; the eight safe schemes stay safe."""
    for key in [k for k in family() if expected_safe(k, "site", "mixed")]:
        res = fuzz(key, runs=RUNS, steps=STEPS, env="site", journal="mixed")
        assert res["safe"], (key, res)


def test_without_the_closed_zones_in_the_journal_a_second_restart_reopens_them():
    """The rule before 2026-11-13 - a restart that finds a journal opens the zones whatever it says - breaks the
    protocol once restarts may keep or lose the journal (a model check with two restarts has the same counterexample)."""
    res = fuzz(NAMED["cap"], runs=RUNS * 4, steps=STEPS, env="site", journal="mixed", ablate="guard")
    assert not res["safe"], res
