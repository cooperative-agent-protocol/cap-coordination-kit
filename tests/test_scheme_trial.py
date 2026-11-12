"""The public scheme comparison: its table is deterministic, agrees with the model's verdict for each scheme, and
every trial's records follow the model's rules."""

from __future__ import annotations

import math

from cap_coordination_kit.entry_gate import EntryGate
from cap_coordination_kit.occupancy import Zone
from cap_coordination_kit.scheme_trial import ACCEL, DELAY_S, DT, Clock, Unicycle, run_all, run_trial

UNSAFE = {("release_early", "mutex_on_done"), ("release_early", "lease_fenced"),
          ("holder_silent", "lease_fenced"),
          ("restart_lossy", "mutex_on_done"), ("restart_lossy", "lease_fenced"),
          ("released_unused", "mutex_on_done"), ("released_unused", "mutex_group"), ("released_unused", "lease_fenced")}


def test_the_table_is_the_models():
    results = run_all()
    assert len(results) == 30
    for r in results:
        assert r.conforming, (r.scheme, r.scenario, r.notes)
        assert r.unadmitted_entries == 0
        assert (r.S1 or r.S2) == ((r.scenario, r.scheme) in UNSAFE), (r.scheme, r.scenario, r.S1, r.S2)


def test_the_schemes_with_a_vacancy_check_and_a_gate_are_safe_everywhere():
    for r in run_all():
        if r.scheme in ("lease_fenced_vacate", "cap"):
            assert not r.S1 and not r.S2, (r.scheme, r.scenario)


def test_a_run_is_reproducible():
    a, b = run_trial("cap", "holder_silent"), run_trial("cap", "holder_silent")
    assert (a.S1, a.S2, a.loads, a.transitions, a.gate_steps) == (b.S1, b.S2, b.loads, b.transitions, b.gate_steps)


def test_the_gate_keeps_a_drive_out_whatever_it_is_told():
    """Closed loop: a unicycle with a command delay and an acceleration limit, commanded straight at the bay, in
    turns and in creeps, never crosses its edge without a permit."""
    clock = Clock()
    zone = Zone("bay", 0.0, 0.0, 9.0, frozenset())
    for start_yaw, pattern in ((math.pi, 0), (math.pi / 2, 1), (2.5, 2)):
        body = Unicycle(14.0, 1.0, start_yaw)
        gate = EntryGate(machine_id="c", mode="permit", permit_ttl_s=24.0, rho=1e-3, delta_entry_s=4.0, clock=clock,
                         act_delay_s=DELAY_S, decel_mps2=ACCEL, period_s=2 * DT)
        gate.set_zones([zone])
        d_min = math.inf
        for k in range(4000):
            bearing = math.atan2(-body.y, -body.x)
            err = math.atan2(math.sin(bearing - body.yaw), math.cos(bearing - body.yaw))
            v = (1.5, 0.3, 1.5 if (k // 40) % 2 else 0.0)[pattern]
            w = max(-1.0, min(1.0, 1.5 * err)) + (0.6 * math.sin(0.37 * k) if pattern else 0.0)
            _reason, cap = gate.check((body.x, body.y, body.yaw, body.v), (v, w))
            if cap is not None:
                scale = 0.0 if cap <= 0 or abs(v) < 1e-9 else min(1.0, cap / abs(v))
                v, w = v * scale, w * scale
            body.command(clock.t, v, w)
            body.step(clock.t, DT)
            clock.t += DT
            d_min = min(d_min, math.hypot(body.x, body.y))
        assert 9.0 <= d_min < 10.5, (pattern, d_min)
