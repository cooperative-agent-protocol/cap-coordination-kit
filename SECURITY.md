<!--
SPDX-License-Identifier: Apache-2.0
Copyright 2026 CAP Authors
-->

# Security Policy — `cap-coordination-kit`

This repository follows the CAP project-wide security policy. Please report a
security issue through **GitHub Private Vulnerability Reporting** on this
repository (Security tab → Report a vulnerability), not through a public issue.
For the disclosure timeline and the severity classes, see the canonical policy
in [`cap-spec/SECURITY.md`](https://github.com/cooperative-agent-protocol/cap-spec/blob/main/SECURITY.md).

## What counts as a security issue here

The kit holds the arbiter, the entry gate, the five reservation schemes and the
conformance check with which the paper's comparison of schemes can be
reproduced. Please report as a security issue any sequence of inputs (requests,
releases, hand-overs in any machine's name, lost or delayed messages, restarts,
positions) under which the protocol (`cap`) or the lease with a vacancy check
(`lease_fenced_vacate`), with the assumptions of RFC-0003 in `cap-spec` met,

- lets two machines that must not share a zone into it at the same time (S1);
- lets a machine into a zone while it is neither the zone's holder nor in its
  custody (S2);
- admits an entry on authority that a later grant has superseded (S3);
- lets a machine leave a zone's custody while a permit issued to it for the zone
  is still counted;
- lets anything other than a typed protocol request change the arbiter or a gate
  (for example, a zone set through the parameters of a work order).

A counterexample that needs an assumption to fail, such as vacancy sensing that
reports a machine out while it is inside, is welcome as an ordinary issue: the
assumptions are stated in RFC-0003 and the paper, and no scheme is expected to
stay safe without them. The failures of the comparison schemes `mutex_on_done`,
`mutex_group` and `lease_fenced` are documented results, not vulnerabilities.

Out of scope: the simulators, language-model clients, and the host, container or
network that runs the kit.

There is no monetary bounty.
