---
title: "ADR 0002: Distributed fleet boundaries"
kind: adr
lifecycle: immutable
authority: Distributed fleet boundaries decision
summary: Add multi-workload fleet orchestration around ServiceRelease v2 while preserving worker initiation, native protocols, and fail-closed movement.
maintenance:
  - Never edit after capture; supersede with a new ADR.
id: ADR-0002
status: accepted
---

# ADR 0002: Distributed fleet boundaries

## Context

Arcturus already owns a host-local release lifecycle and must extend it across
heterogeneous workers without duplicating that lifecycle, inventing application
protocols, or treating a two-worker evidence run as the architecture.

## Decision

- Fleet orchestration wraps a complete `ServiceRelease v2`, which remains the
  atomic worker-local lifecycle and rollback unit.
- Every worker supports multiple independently assigned workloads. Desired
  generations, retries, observations, failures, and removals are isolated per
  worker and workload.
- Workers initiate communication. Snapshot omission is inert; only explicit
  `ensureAbsent` desired state authorizes removal.
- Live pressure may affect admission but cannot evict or move an existing
  workload.
- Movement evaluates movability, overlap, authoritative state, migration, and
  fencing separately. Unknown or unsafe transitions fail closed.
- Mechanically safe stateless movement needs no separate approval object.
- Fleet resource identity is independent of releases. Applications retain
  native protocols, and imported versus Arcturus-managed lifecycle remains
  explicit.
- Measured inventory, live pressure, and operator attestations remain separate.
- Placement begins with observable hard eligibility rather than a generic
  scheduler.
- Filesystem selection is centralized so rootless XDG and future FHS layouts
  share the same state contracts.
- Existing operator CLI and API surfaces are extended instead of creating a
  second fleet-only experience.

## Consequences

The fleet can grow beyond the evidence cohort without claiming unbounded
scalability. Stateful movement, scored scheduling, managed-resource lifecycle,
automatic failover, and component splitting require later owner-visible
outcomes. They cannot be introduced by editing this decision or by expanding a
status page.

The detailed current behavior belongs in [Distributed
fleet](../distributed-fleet.md), while slice-specific proof belongs in
[DIST-001](../slices/DIST-001/README.md).
