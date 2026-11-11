# Copied from the reference implementation by its scripts/paper_r1/sync_kit.py - edit there, not here.
"""The replay of a trial's records against the occupancy model (AiC revision WP3b): a clean trace conforms, and each
kind of step the model forbids is caught - so a campaign's "every trial conforms" is not the monitor's silence."""

from __future__ import annotations

from cap_coordination_kit.occupancy import Scheme
from cap_coordination_kit.occupancy_conformance import check_trial

BAY = "loading:zx200"
ZONES = {BAY: (0.0, 0.0, 9.0)}
CAP = Scheme.named("cap")


def rec(seq, t, event, m, *, prior=None, holder=None, custody=(), epoch=None, planned_before=None, planned=None,
        guard="open", rid=BAY):
    return {"seq": seq, "t_mono": 100.0 + t, "t_wall": 1000.0 + t, "event": event, "resource_id": rid,
            "requested_holder": m, "prior_holder": prior, "resolved_holder": holder, "custody": list(custody),
            "epoch": epoch, "planned": planned, "planned_before": planned_before, "guard": guard, "reason": ""}


def clean():
    """A grants, enters on a permit; hands over to the planned B; A is confirmed out; B gets its permit."""
    return [
        rec(1, 0.0, "GRANTED", "a", holder="a", epoch=1),
        rec(2, 0.1, "DENIED", "b", prior="a", holder="a", planned="b"),
        rec(3, 1.0, "PERMIT", "a", prior="a", holder="a", epoch=1, planned="b"),
        rec(4, 9.0, "HANDOVER", "b", prior="a", holder="b", custody=["a"], epoch=2, planned_before="b"),
        rec(5, 9.5, "PERMIT_DENIED", "b", prior="b", holder="b", custody=["a"]),
        rec(6, 12.0, "VACATED", "a", prior="b", holder="b"),
        rec(7, 12.5, "PERMIT", "b", prior="b", holder="b", epoch=2),
    ]


def gates():
    return {"a": [{"t_mono": 101.2, "event": "admit", "resource_id": BAY, "epoch": 1},
                  {"t_mono": 101.5, "event": "enter", "resource_id": BAY, "epoch": 1, "reason": ""}],
            "b": [{"t_mono": 112.7, "event": "admit", "resource_id": BAY, "epoch": 2},
                  {"t_mono": 113.0, "event": "enter", "resource_id": BAY, "epoch": 2, "reason": ""}]}


def poses():
    # a inside from t = 1.5 to 11, out after; b outside until 13
    out = []
    for i in range(0, 150):
        t = i / 10.0
        out.append((t, "a", 5.0 if 1.5 <= t < 11.0 else 12.0, 0.0))
        out.append((t, "b", 5.0 if t >= 13.0 else 14.0, 3.0))
    return out


def run(tx, gate_logs=None, scheme=CAP, **kw):
    return check_trial(txlog=tx, gate_logs=gates() if gate_logs is None else gate_logs, poses=poses(), zones=ZONES,
                       carriers=["a", "b"], scheme=scheme, permit_life_s=3.0, machine_permit_life_s=2.9,
                       t0_mono=100.0, **kw)


def test_a_clean_trace_conforms():
    r = run(clean())
    assert r.conforming and r.transitions == 7 and r.gate_steps == 4 and not r.sensing and not r.unasked_refusals


def test_a_grant_while_another_holds_is_caught():
    tx = clean()
    tx[1] = rec(2, 0.1, "GRANTED", "b", holder="b", epoch=2)
    assert any("held by a" in v["reason"] for v in run(tx).violations)


def test_a_permit_while_the_sender_is_not_confirmed_out_is_caught():
    tx = clean()
    tx[4] = rec(5, 9.5, "PERMIT", "b", prior="b", holder="b", custody=["a"], epoch=2)
    assert any("not confirmed out" in v["reason"] for v in run(tx).violations)


def test_a_permit_while_anothers_permit_is_counted_is_caught():
    tx = [rec(1, 0.0, "GRANTED", "a", holder="a", epoch=1),
          rec(2, 1.0, "PERMIT", "a", prior="a", holder="a", epoch=1),
          rec(3, 1.5, "RELEASED", "a", prior="a", holder=None, custody=["a"]),
          rec(4, 1.6, "VACATED", "a", prior=None, holder=None),
          rec(5, 1.7, "GRANTED", "b", holder="b", epoch=2)]            # a's permit is counted until t = 4
    r = run(tx, gate_logs={})
    assert any("a permit of ['a'] is counted" in v["reason"] for v in r.violations)


def test_a_hand_over_past_the_planned_successor_is_caught():
    tx = clean()
    tx[3] = rec(4, 9.0, "HANDOVER", "b", prior="a", holder="b", custody=["a"], epoch=2, planned_before="c")
    assert any("planned successor c" in v["reason"] for v in run(tx).violations)


def test_an_expiry_while_the_holders_permit_is_counted_is_caught():
    tx = [rec(1, 0.0, "GRANTED", "a", holder="a", epoch=1),
          rec(2, 1.0, "PERMIT", "a", prior="a", holder="a", epoch=1),
          rec(3, 2.0, "EXPIRED", "a", prior="a", holder=None, custody=["a"])]
    assert any("while its permit is counted" in v["reason"] for v in run(tx, gate_logs={}).violations)


def test_the_arbiters_own_state_is_compared():
    tx = clean()
    tx[3] = rec(4, 9.0, "HANDOVER", "b", prior="a", holder="b", custody=[], epoch=2, planned_before="b")
    r = run(tx)
    assert any("custody" in d["reason"] for d in r.divergences)


def test_a_vacancy_confirmed_while_truly_inside_is_a_sensing_failure_not_a_step_of_the_arbiter():
    tx = clean()
    tx[5] = rec(6, 10.0, "VACATED", "a", prior="b", holder="b")        # a is inside until t = 11
    r = run(tx)
    assert r.sensing and r.conforming


def test_an_admission_without_a_permit_and_an_entry_without_an_admission_are_caught():
    g = gates()
    g["b"][0]["epoch"] = 7                                             # a permit the arbiter never issued
    assert any("did not issue" in v["reason"] for v in run(clean(), gate_logs=g).violations)
    g = gates()
    g["b"][1]["reason"] = "not admitted"
    assert any("without an admission" in v["reason"] for v in run(clean(), gate_logs=g).violations)
    g = gates()
    g["a"][0]["t_mono"] = 105.0                                        # 4 s after a permit usable for 2.9 s
    assert any("after its permit was issued" in v["reason"] for v in run(clean(), gate_logs=g).violations)


def test_a_refusal_the_model_does_not_ask_for_is_reported_apart():
    tx = [rec(1, 0.0, "DENIED", "a", holder=None)]
    r = run(tx, gate_logs={})
    assert r.unasked_refusals and r.conforming


def test_a_local_gate_needs_a_grant():
    mutex = Scheme.named("mutex_group")
    tx = [rec(1, 0.0, "GRANTED", "a", holder="a", epoch=1)]
    g = {"b": [{"t_mono": 101.0, "event": "admit", "resource_id": BAY, "epoch": 0}]}
    assert any("without a grant" in v["reason"] for v in run(tx, gate_logs=g, scheme=mutex).violations)
    g = {"a": [{"t_mono": 101.0, "event": "admit", "resource_id": BAY, "epoch": 1}]}
    assert run(tx, gate_logs=g, scheme=mutex).conforming


def test_a_restart_that_lost_the_journal_resets_the_model_whatever_the_guard_becomes():
    mutex = Scheme.named("mutex_on_done")
    tx = [rec(1, 0.0, "GRANTED", "a", holder="a", epoch=1),
          {**rec(2, 5.0, "GUARD_OPEN", None, rid=""), "reason": "journal unreadable after a restart: occupancy unknown"},
          rec(3, 6.0, "GRANTED", "b", holder="b", epoch=2)]
    r = run(tx, gate_logs={}, scheme=mutex)
    assert r.conforming and r.restarts == 1


def test_an_authorisation_before_the_other_gates_know_the_zone_is_caught():
    table = dict(rec(0, -3.0, "TABLE", None, epoch=1), reason="zone at (0.00, 0.00) radius 9.00 m; open to zx200")
    known = [dict(rec(0, -2.0, "MACHINE", m, rid=""), reason="known to the Site") for m in ("a", "b", "zx200")]
    ack_a = dict(rec(0, -1.0, "TABLE_ACK", "a", rid="", epoch=1))
    ack_b = dict(rec(0, -1.0, "TABLE_ACK", "b", rid="", epoch=1))
    ok = run([table, *known, ack_a, ack_b, *clean()])
    assert ok.conforming, ok.violations               # both carriers' gates know the bay; the loader is not asked
    late = run([table, *known, ack_a, *clean()])      # a is let in although b's gate may not know the bay
    assert not late.conforming and any("know the zone" in v["reason"] for v in late.violations), late.violations
    # a refusal for that reason is one the rules ask for
    refused = dict(rec(1, 0.0, "DENIED", "a"), reason="the zone table is not yet acknowledged by 'b'")
    r = run([table, *known, refused], gate_logs={})
    assert r.conforming and not r.unasked_refusals


def test_a_zone_moved_while_it_is_held_is_caught():
    table = dict(rec(0, -3.0, "TABLE", None, epoch=1), reason="zone at (0.00, 0.00) radius 9.00 m; open to zx200")
    known = [dict(rec(0, -2.0, "MACHINE", m, rid=""), reason="known to the Site") for m in ("a", "b", "zx200")]
    acks = [dict(rec(0, -1.0, "TABLE_ACK", m, rid="", epoch=1)) for m in ("a", "b")]
    grant = rec(1, 0.0, "GRANTED", "a", holder="a", epoch=1)
    moved = dict(rec(2, 0.5, "TABLE", None, epoch=2), reason="zone at (0.01, 0.00) radius 9.00 m; open to zx200")
    r = run([table, *known, *acks, grant, moved], gate_logs={})
    assert not r.conforming and any("the zone was moved" in v["reason"] for v in r.violations), r.violations
    free = run([table, *known, *acks, moved], gate_logs={})          # nobody holds it: a zone may move
    assert free.conforming, free.violations

