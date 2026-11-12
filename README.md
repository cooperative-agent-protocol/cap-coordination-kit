# cap-coordination-kit

A small, dependency-light **protocol-logic** carve of the CAP reference
implementation. It contains **only protocol logic** — no LLM agents, no path
planner, no physics simulator — so that the protocol-level results in the CAP
paper *"Ownership and occupancy in shared-zone handover for autonomous earthwork
fleets: a model-checked coordination protocol"* (revised version of *"A Model-Checked
Coordination Protocol for Shared-Resource Handover in Autonomous Earthwork Fleets"*)
can be reproduced from open code, while the full
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
| `scheme_trial.py` | A small deterministic simulation - two scripted carriers as unicycles, one loading bay - that runs the five schemes through six scenarios (one of them with the custody rule switched off on request) and reports which ones let two carriers into the bay at once. Public code only; reproducible bit for bit. |
| `zones.py` | A conflict zone of another shape for the same arbiter and gate: a **single-lane segment** of road (the distance to a line segment instead of to a point). The arbiter and the gate use a zone only through that distance and the zone's reach. |
| `segment_trial.py` | The five schemes on the single-lane segment, crossed from both ends by two carriers: the same arbiter, gate, drive and oracles as `scheme_trial.py`, a different geometry. The schemes fail on the lane where they fail at the bay. |
| `vacancy_margin.py` | The probability of a **false vacancy confirmation** from the margin of the vacancy check and the spread of the position error (a Marcum Q-function), per report and over a custody episode; the margins that are necessary and sufficient when the error is bounded. No dependency. |
| `vacancy_design.py` | The **allowance for position error** that the vacancy check's margin must leave, in standard deviations of the error, for a target probability of a false confirmation when `k` consecutive observations must show the machine out and the error is correlated from one observation to the next (exact recursion for independent errors; a grid integration otherwise, checked against both by `--selfcheck`). Needs numpy. |
| `family_fuzz.py` | Randomised executions of the arbiter's own code for all **90 schemes of the family**: an untrusted client that requests, releases and hands over in any machine's name, delayed and lost messages, clocks drifting within rho, restarts of machines and of the arbiter with or without its journal, and the machines' true positions at the Site; S1-S3 checked after every step. The schemes without a violation are the ones the model checker finds safe, in each of four environments (Site-side or own releases, journal kept or lost), and removing the custody rule, the bound on clock drift or sound vacancy sensing breaks every one of them. Randomised runs find violations; they do not prove that there are none. |

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

# 5. The five schemes, six scenarios (revised paper: which scheme is safe where) - bit-exact
python3 -m cap_coordination_kit.scheme_trial
#   scenario                mutex_on_done          mutex_group         lease_fenced  lease_fenced_vacate                  cap
#   nominal                safe (4 loads)       safe (4 loads)       safe (4 loads)       safe (4 loads)       safe (4 loads)
#   release_early         S1+S2 (4 loads)       safe (4 loads)      S1+S2 (4 loads)       safe (4 loads)       safe (4 loads)
#   holder_silent          safe (4 loads)       safe (4 loads)      S1+S2 (4 loads)       safe (4 loads)       safe (4 loads)
#   reenter_old            safe (4 loads)       safe (4 loads)       safe (4 loads)       safe (4 loads)       safe (4 loads)
#   restart_lossy         S1+S2 (4 loads)       safe (4 loads)      S1+S2 (4 loads)       safe (4 loads)       safe (4 loads)
#   released_unused       S1+S2 (4 loads)      S1+S2 (4 loads)      S1+S2 (4 loads)       safe (4 loads)       safe (4 loads)
#
#   30 trials; records conform to the model in 30; 5669 arbiter transitions, 252 gate steps
#   (S1: two carriers inside the bay at once; S2: a carrier inside that the arbiter does not account for.
#    One deterministic run per cell: "safe" here is one execution, not a proof - the exhaustive answer for each
#    scheme is the model checker's, in cap-spec/formal/tla/family. released_unused: the Site gives the bay up for
#    a carrier that holds an unused permit and cannot be told; its client backs it out of the vacancy envelope's
#    reach and drives it in on the permit.)

# 5b. The same without the custody rule (a machine leaves custody when seen outside, whatever permits of it are
#     still counted): the two schemes with a vacancy check now fail in the new scenario, and their records no
#     longer conform to the model
python3 -m cap_coordination_kit.scheme_trial --no-custody-rule
#   ...
#   released_unused       S1+S2 (4 loads)      S1+S2 (4 loads)      S1+S2 (4 loads)      S1+S2 (4 loads)      S1+S2 (4 loads)

# 5c. A second kind of space: the same schemes on a single-lane stretch of road crossed from both ends
python3 -m cap_coordination_kit.segment_trial
#   scenario                  mutex_on_done            mutex_group           lease_fenced    lease_fenced_vacate                    cap
#   nominal              safe (4 crossings)     safe (4 crossings)     safe (4 crossings)     safe (4 crossings)     safe (4 crossings)
#   release_early       S1+S2 (4 crossings)     safe (4 crossings)    S1+S2 (4 crossings)     safe (4 crossings)     safe (4 crossings)
#   holder_silent        safe (4 crossings)     safe (4 crossings)    S1+S2 (4 crossings)     safe (4 crossings)     safe (4 crossings)

# 6. Arbiter + Handover invariants (no-double-grant, atomic transfer, monitor, surface, lease)
python3 -m pytest tests/ -q          # 64 passed

# 6a. The design table of the vacancy margin (revised paper): allowance in standard deviations for k looks and a
#     correlated error (needs numpy)
python3 -m cap_coordination_kit.vacancy_design --selfcheck
python3 -m cap_coordination_kit.vacancy_design

# 6b. The risk of a false vacancy confirmation for a margin and an error spread (revised paper, Theorem 4)
python3 -m cap_coordination_kit.vacancy_margin --radius 9 --margin 2.225 --sigma 0.5 1.5
#   sigma 0.50 m, 0.50 m inside: 2.91e-08 per report / 0.000 over 20 reports
#   sigma 1.50 m, 0.50 m inside: 4.09e-02 per report / 0.567 over 20 reports

# 8. The family of schemes on the arbiter's own code (a few minutes per environment); each line ends with how
#    many schemes ran without a violation and how many agree with the model checker
python3 -m cap_coordination_kit.family_fuzz --env site --journal kept      # 8 of 90; 90 agree
python3 -m cap_coordination_kit.family_fuzz --env own --journal kept       # 12 of 90; 90 agree
python3 -m cap_coordination_kit.family_fuzz --schemes safe --ablate custody   # 0 of 8; 8 agree

# 7. Re-derive the per-transition conformance verdict from a shipped campaign log
python3 -m cap_coordination_kit.refinement_monitor <path>/reservation_txlog.jsonl
```

The campaign reservation transaction logs (`reservation_txlog.jsonl`) ship with
the paper's supplementary material; the monitor here re-derives the reported
"every transition is spec-permitted" verdict from them without the closed
reference implementation.

## Scope

For the revised paper, the kit reproduces from public code the comparison of the
five schemes scenario by scenario (`scheme_trial`, `segment_trial`), the
calculations of the vacancy margin (`vacancy_margin`, `vacancy_design`) and the
conformance verdict on the released records of the trials
(`occupancy_conformance`). For the original submission it reproduces the
**operational-P4 choreography**, its **sensitivity surface**, the **lease-lock
baseline** and the **per-transition conformance verdict**. It does **not** re-run
the paper's kinematic and physics campaigns: they use the closed reference
implementation's Site, coordinator and machine agents, and are made *auditable*
through their released records, not re-runnable. See the paper's Data and Code
Availability section.

## Security

Please report a security issue privately, as described in
[SECURITY.md](SECURITY.md). The continuous integration runs the tests against
bindings generated from `cap-spec` and a CodeQL analysis.

## License

Apache-2.0. Copyright 2026 CAP Authors.
