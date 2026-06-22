"""cap-coordination-kit — protocol-logic carve of the CAP reference implementation.

Contains ONLY protocol logic (no LLM agents, no simulator): the authoritative
reservation arbiter, the atomic machine-to-machine Handover primitive, the
deterministic operational-P4 handover-choreography model, and the
spec-conformance monitor. Depends only on the public cap.v0 Protocol Buffer
bindings. See README.md.
"""
from cap_coordination_kit.handover import HandoverManager
from cap_coordination_kit.reservation import ReservationManager

__all__ = ["ReservationManager", "HandoverManager"]
