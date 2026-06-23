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
| `refinement_monitor.py` | Runtime spec↔implementation **conformance monitor**: reconstructs holder state from an arbiter transaction log alone and checks every transition against the spec's allowed transition relation (no double-grant, no spurious denial, handover only by the current holder). |

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

# 4. Arbiter + Handover invariants (no-double-grant, atomic transfer, monitor, surface)
python3 -m pytest tests/ -q          # 24 passed

# 5. Re-derive the per-transition conformance verdict from a shipped campaign log
python3 -m cap_coordination_kit.refinement_monitor <path>/reservation_txlog.jsonl
```

The campaign reservation transaction logs (`reservation_txlog.jsonl`) ship with
the paper's supplementary material; the monitor here re-derives the reported
"every transition is spec-permitted" verdict from them without the closed
reference implementation.

## Scope

This kit makes the **operational-P4 choreography**, its **sensitivity surface**,
and the **per-transition conformance verdict** publicly reproducible, and lets
the **arbiter and handover invariants** be unit-tested directly. It does **not** regenerate the contended
fault-injection / handover campaigns or the physics witness — those run inside
the closed reference implementation and are made *auditable* (published
transition logs + per-run records), not re-runnable. See the paper's Data &
Code Availability section.

## License

Apache-2.0. Copyright 2026 CAP Authors.
