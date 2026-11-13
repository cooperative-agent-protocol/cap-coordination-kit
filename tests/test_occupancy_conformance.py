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



# The arbiter counts a permit from its decision, before it writes and syncs its journal; the log record comes after
# the sync. Counted from the record, a grant just after the permit lapsed (by the arbiter's count) looks early.
FENCED = Scheme.named("lease_fenced")


def _fenced_trace(t_counted=None):
    permit = rec(2, 1.0, "PERMIT", "a", prior="a", holder="a", epoch=1)
    if t_counted is not None:
        permit["t_counted"] = t_counted
    return [rec(1, 0.0, "GRANTED", "a", holder="a", epoch=1), permit,
            rec(3, 1.5, "RELEASED", "a", prior="a", holder=None),
            rec(4, 3.4, "GRANTED", "b", holder="b", epoch=2)]       # 2.4 s after the record, 3.2 s after the decision


def test_a_permit_is_counted_from_the_arbiters_decision_not_from_its_log_record():
    late_record = run(_fenced_trace(), gate_logs={}, scheme=FENCED)
    assert any("a permit of ['a'] is counted" in v["reason"] for v in late_record.violations)
    decided = run(_fenced_trace(t_counted=100.2), gate_logs={}, scheme=FENCED)   # life 3.0: lapsed at 103.2
    assert decided.conforming, decided.violations
    early = run(_fenced_trace(t_counted=100.6), gate_logs={}, scheme=FENCED)     # lapses at 103.6: still counted
    assert any("a permit of ['a'] is counted" in v["reason"] for v in early.violations)


def test_a_recorded_trial_takes_the_permits_start_from_the_journal_snapshot_written_for_it(tmp_path):
    import json

    from cap_coordination_kit.occupancy_conformance import check_trial_dir

    d = tmp_path / "trial"
    (d / "state").mkdir(parents=True)
    k = 900.0                                   # monotonic = wall - 900 in this process
    tx = [dict(r, t_wall=r["t_mono"] + k) for r in _fenced_trace()]
    tx[1]["t_wall"] = 0.0                       # a caller's "now": the arbiter's wall-clock field is not used
    tx[3]["t_mono"] = 103.5                     # the arbiter's count (life 3.003 at 8x) ended at 103.203
    (d / "reservation_txlog.jsonl").write_text("".join(json.dumps(r) + "\n" for r in tx))
    (d / "gate_logs.json").write_text(json.dumps(
        {"a": [{"t_mono": 100.05, "t_wall": 100.05 + k, "event": "table", "resource_id": "", "epoch": 0, "reason": ""}]}))
    snap = {"v": 2, "boot": 1, "scheme": "lease_fenced", "zones": [],
            "res": {BAY: {"holder": "a", "rid": "r1", "gen": 1, "custody": [], "live": []}}}
    with_permit = {**snap, "t_wall": 100.2 + k, "res": {BAY: {**snap["res"][BAY], "live": [["a", 1]]}}}
    (d / "state" / "occupancy_journal.jsonl").write_text(
        "".join(json.dumps(s) + "\n" for s in ({**snap, "t_wall": 100.0 + k}, with_permit)))
    events = [{"t": 0.0, "wall": 99.0 + k, "kind": "occupancy", "machine_id": "zx200",
               "extra": {"kind": "pose", "x": 0.0, "y": 0.0}}]
    (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (d / "metrics.json").write_text(json.dumps({"scheme": "lease_fenced", "speedup": 8.0, "carriers": ["a", "b"]}))
    assert check_trial_dir(d).conforming
    (d / "state" / "occupancy_journal.jsonl").unlink()           # without the journal: counted from the record
    assert not check_trial_dir(d).conforming


# ---- the gate's own records: a permit serves one entry, and the machine times it from the request it answered

def _gates_with_receipts(second_admit_s=None, request_s=101.0):
    a = [{"t_mono": request_s, "event": "request", "resource_id": BAY, "epoch": None},
         {"t_mono": 101.05, "event": "permit", "resource_id": BAY, "epoch": 1},
         {"t_mono": 101.2, "event": "admit", "resource_id": BAY, "epoch": 1},
         {"t_mono": 101.5, "event": "enter", "resource_id": BAY, "epoch": 1, "reason": ""}]
    if second_admit_s is not None:          # out and back in on the same permit
        a += [{"t_mono": 102.0, "event": "exit", "resource_id": BAY},
              {"t_mono": second_admit_s, "event": "admit", "resource_id": BAY, "epoch": 1}]
    b = [{"t_mono": 112.6, "event": "request", "resource_id": BAY, "epoch": None},
         {"t_mono": 112.65, "event": "permit", "resource_id": BAY, "epoch": 2},
         {"t_mono": 112.7, "event": "admit", "resource_id": BAY, "epoch": 2},
         {"t_mono": 113.0, "event": "enter", "resource_id": BAY, "epoch": 2, "reason": ""}]
    return {"a": a, "b": b}


def test_a_gate_that_keeps_its_receipts_conforms_when_each_permit_serves_one_entry():
    assert run(clean(), gate_logs=_gates_with_receipts()).conforming


def test_an_admission_on_a_permit_already_used_is_caught():
    r = run(clean(), gate_logs=_gates_with_receipts(second_admit_s=102.5))
    assert not r.conforming and any("did not hold unused" in v["reason"] for v in r.violations), r.violations


def test_an_admission_later_than_the_machines_own_count_from_its_request_is_caught():
    # the request at 98.2 s: the admission at 101.2 s is 3.0 s after it, the machine's count is 2.9 s
    r = run(clean(), gate_logs=_gates_with_receipts(request_s=98.2))
    assert not r.conforming and any("after the request its permit answered" in v["reason"] for v in r.violations)


def test_a_reissued_permit_takes_its_start_from_its_own_snapshot_not_an_earlier_one(tmp_path):
    """The same holder and epoch asked twice: the first permit's snapshot also lists (a, 1). The second permit's start
    must come from the snapshot written after the record before it, not from the first."""
    import json

    from cap_coordination_kit.occupancy_conformance import decision_times_from_journal

    d = tmp_path / "trial"
    (d / "state").mkdir(parents=True)
    k = 900.0
    tx = [rec(1, 0.0, "GRANTED", "a", holder="a", epoch=1),
          rec(2, 1.0, "PERMIT", "a", prior="a", holder="a", epoch=1),
          rec(3, 2.0, "DENIED", "b", prior="a", holder="a"),
          rec(4, 5.0, "PERMIT", "a", prior="a", holder="a", epoch=1)]
    snap = {"v": 2, "boot": 1, "scheme": "cap", "zones": [],
            "res": {BAY: {"holder": "a", "rid": "r1", "gen": 1, "custody": [], "live": [["a", 1]]}}}
    snaps = [{**snap, "t_wall": 100.0 + k - 0.01, "res": {BAY: {**snap["res"][BAY], "live": []}}},
             {**snap, "t_wall": 100.8 + k}, {**snap, "t_wall": 104.4 + k}]
    (d / "state" / "occupancy_journal.jsonl").write_text("".join(json.dumps(s) + "\n" for s in snaps))
    gl = {"a": [{"t_mono": 100.05, "t_wall": 100.05 + k, "event": "table", "resource_id": "", "epoch": 0}]}
    assert decision_times_from_journal(d, tx, gl) == 3
    assert abs(tx[1]["t_counted"] - 100.8) < 1e-6 and abs(tx[3]["t_counted"] - 104.4) < 1e-6
    # without its own snapshot the second permit keeps its record's time (the first's would be earlier)
    (d / "state" / "occupancy_journal.jsonl").write_text("".join(json.dumps(s) + "\n" for s in snaps[:2]))
    tx2 = [dict(r) for r in tx]
    for r in tx2:
        r.pop("t_counted", None), r.pop("t_decided", None)
    decision_times_from_journal(d, tx2, gl)
    assert "t_counted" not in tx2[3]
