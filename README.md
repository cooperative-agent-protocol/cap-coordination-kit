# cap-coordination-kit

A small, dependency-light **protocol-logic** carve of the CAP reference
implementation. It contains **only protocol logic** — no LLM agents, no path
planner, no physics simulator — so that the protocol-level results in the CAP
paper *"A Model-Checked Coordination Protocol for Shared-Resource Handover in
Autonomous Earthwork Fleets"* can be reproduced from open code, while the full
reference implementation (which carries proprietary agents and a third-party
simulator) stays closed.

## What's inside

| Module | What it is |
|---|---|
| `reservation.py` | The authoritative reservation arbiter: single-holder granting (deny-on-busy), the atomic machine-to-machine **Handover** primitive (`transfer_grant`), the no-double-grant audit (`audit_double_grants`), and an authoritative transaction log (`txlog`). |
| `handover.py` | `HandoverManager` — runtime driver for the typed Handover sub-protocol (initiate → ack → begin{atomic} → complete → cleanup). |
| `handover_choreography.py` | Deterministic **operational-P4** model: enumerates handover interleavings and reports load-point co-occupancy under an atomic typed handover vs. a plain release-then-reacquire mutex. |
| `sensitivity_surface.py` | Extends the operational-P4 model into a **sensitivity surface** over vacate latency x receiver delay, locating the boundary where a plain lock co-occupies the load point (20 of 30 cells) while CAP never does (occupancy 1 in every cell). |
| `lease_lock.py` | A realistic **lease+TTL lock** (Chubby/ZooKeeper-style) baseline and a deterministic experiment: it reclaims and re-grants a **live holder's** lock when the lease outlasts the hold (a "lease steal", 15 of 25 cells) while CAP's deny-on-busy arbiter never does (0); the steal is invisible to the no-double-grant audit, motivating CAP's grant-epoch fence. |
| `refinement_monitor.py` | Runtime spec↔implementation **conformance monitor**: reconstructs holder state from an arbiter transaction log alone and checks every transition against the spec's allowed transition relation (no double-grant, no spurious denial, handover only by the current holder). |
| `occupancy.py` | The **occupancy-gated arbiter** of the revised paper and the family of schemes it runs: mutex released on completion, mutex group released on exit, renewable fenced lease, the lease with a vacancy check, and the proposed protocol (`Scheme.named`). Entry permits timed by the lease rule, custody until a confirmed vacancy (`VacancyEnvelope`), a journal with a restart guard. |
| `entry_gate.py` | The machine-side **entry gate**: checks every drive command against the path to a stop under a model of the drive (command delay, acceleration limit, check period), slows or stops a machine that has no authority to enter a zone, stops a command that is not renewed, and lets a machine without authority inside a zone move out only. |
| `occupancy_conformance.py` | Replays a trial's records (arbiter log, gate logs, true positions) against the rules of the occupancy model (`cap-spec/formal/tla/OccupancyHandover.tla`), independently of the arbiter's code. |
| `scheme_trial.py` | A small deterministic simulation - two scripted carriers as unicycles, one loading bay - that runs the five schemes through five scenarios and reports which ones let two carriers into the bay at once. Public code only; reproducible bit for bit. |
| `vacancy_margin.py` | The probability of a **false vacancy confirmation** from the margin of the vacancy check and the spread of the position error (a Marcum Q-function), per report and over a custody episode; the margins that are necessary and sufficient when the error is bounded. No dependency. |

The only external dependency is the public **cap.v0** Protocol Buffer bindings
(from [`cap-spec`](https://github.com/cooperative-agent-protocol/cap-spec) /
[`cap-reference`](https://github.com/cooperative-agent-protocol/cap-reference)).

## Reproduce the paper's open-code results

```bash
# 1. Put the cap.v0 protobuf bindings on the path (from cap-spec gen or cap-reference)
export PYTHONPATH=".:/path/to/cap-spec/gen/python"

# 2. Operational-P4 (paper Figure: mutex 18/24 co-occupied vs CAP 0/24) — bit-exact
python3 -m cap_coordination_kit.handover_choreography
#   atomic [CAP atomic typed handover]: 0/24 interleavings co-occupied
#   naive  [plain mutex]: 18/24 interleavings co-occupied

# 3. Sensitivity surface (paper Table: mutex 20/30 cells co-occupied vs CAP 0/30) — bit-exact
python3 -m cap_coordination_kit.sensitivity_surface
#   mutex co-occupies 20/30 cells (occupancy 2 = violation); CAP co-occupies 0/30

# 4. Lease-lock baseline (paper Table: lease steals a live holder's lock 15/25 vs CAP 0) — bit-exact
python3 -m cap_coordination_kit.lease_lock
#   lease steals total=15/25 cells; CAP deny-on-busy steals=0; audit flags=0

# 5. The five schemes, five scenarios (revised paper: which scheme is safe where) - bit-exact
python3 -m cap_coordination_kit.scheme_trial
#   scenario                mutex_on_done          mutex_group         lease_fenced  lease_fenced_vacate                  cap
#   nominal                safe (4 loads)       safe (4 loads)       safe (4 loads)       safe (4 loads)       safe (4 loads)
#   release_early         S1+S2 (4 loads)       safe (4 loads)      S1+S2 (4 loads)       safe (4 loads)       safe (4 loads)
#   holder_silent          safe (4 loads)       safe (4 loads)      S1+S2 (4 loads)       safe (4 loads)       safe (4 loads)
#   reenter_old            safe (4 loads)       safe (4 loads)       safe (4 loads)       safe (4 loads)       safe (4 loads)
#   restart_lossy         S1+S2 (4 loads)       safe (4 loads)      S1+S2 (4 loads)       safe (4 loads)       safe (4 loads)
#   25 trials; records conform to the model in 25; 4918 arbiter transitions, 202 gate steps
#   (S1: two carriers inside the bay at once; S2: a carrier inside that the arbiter does not account for.
#    One deterministic run per cell: "safe" here is one execution, not a proof - the exhaustive answer for each
#    scheme is the model checker's, in cap-spec/formal/tla/family.)

# 6. Arbiter + Handover invariants (no-double-grant, atomic transfer, monitor, surface, lease)
python3 -m pytest tests/ -q          # 49 passed

# 6b. The risk of a false vacancy confirmation for a margin and an error spread (revised paper, Theorem 4)
python3 -m cap_coordination_kit.vacancy_margin --radius 9 --margin 2.225 --sigma 0.5 1.5
#   sigma 0.50 m, 0.50 m inside: 2.91e-08 per report / 0.000 over 20 reports
#   sigma 1.50 m, 0.50 m inside: 4.09e-02 per report / 0.567 over 20 reports

# 7. Re-derive the per-transition conformance verdict from a shipped campaign log
python3 -m cap_coordination_kit.refinement_monitor <path>/reservation_txlog.jsonl
```

The campaign reservation transaction logs (`reservation_txlog.jsonl`) ship with
the paper's supplementary material; the monitor here re-derives the reported
"every transition is spec-permitted" verdict from them without the closed
reference implementation.

## Scope

This kit makes the **operational-P4 choreography**, its **sensitivity surface**,
the **lease-lock baseline**, and the **per-transition conformance verdict**
publicly reproducible, and lets the **arbiter and handover invariants** be
unit-tested directly. It does **not** regenerate the contended
fault-injection / handover campaigns or the physics witness — those run inside
the closed reference implementation and are made *auditable* (published
transition logs + per-run records), not re-runnable. See the paper's Data &
Code Availability section.

## License

Apache-2.0. Copyright 2026 CAP Authors.
