---
title: Architecture decisions
kind: decision-index
lifecycle: stable
authority: Projection of accepted and superseded ADRs
summary: Index of durable architecture decisions.
maintenance:
  - The decision-index explanation or policy changes.
generated_by: docs/_tooling/vertical_slices.py
generated_sections:
  - decisions
nav:
  section: Understand the system
  order: 15
---

# Architecture decisions

Only explicit, durable decisions belong here. Open questions and raw ideas stay
in tasks or issues until repository review and owner approval.

<!-- BEGIN GENERATED:decisions -->
| ADR | Decision | Status | Summary |
| --- | --- | --- | --- |
| `ADR-0001` | [ADR 0001: Arcturus-owned OCI ingress and Rust control plane](adr/0001-arcturus-owned-oci-ingress-and-rust-control-plane.md) | `accepted` | GitHub remains release authority while Arcturus owns OCI ingress and incrementally moves control-plane responsibilities to Rust. |
| `ADR-0002` | [ADR 0002: Distributed fleet boundaries](adr/0002-distributed-fleet-boundaries.md) | `accepted` | Add multi-workload fleet orchestration around ServiceRelease v2 while preserving worker initiation, native protocols, and fail-closed movement. |
<!-- END GENERATED:decisions -->
