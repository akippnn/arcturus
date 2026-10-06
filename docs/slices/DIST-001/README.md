---
title: DIST-001 distributed fleet foundation
kind: slice-overview
lifecycle: stable
authority: Human overview of DIST-001
summary: Place, observe, recover, and safely move one release across heterogeneous workers.
maintenance:
  - The current contract pointer or explanatory diagrams change.
id: DIST-001-overview
slice: DIST-001
nav:
  section: Delivery records
  order: 41
---

# DIST-001: Distributed fleet foundation

DIST-001 adds fleet placement around the existing worker-local lifecycle. Its
source implementation is complete; the manifest owns the remaining gate and
owner verdict.

## System path

```mermaid
flowchart LR
  O["Operator<br/>arcturusctl"] --> C["Rust control plane"]
  W1["arm64 worker"] -->|outbound| C
  W2["amd64 worker"] -->|outbound| C
  C --> I["WorkloadIntent"]
  I --> A["WorkerAssignment"]
  A --> L["ServiceRelease v2 lifecycle"]
  L --> P["Podman / Quadlet / systemd"]
  P --> S["ObservedState"]
  P -->|RESP| R["Imported Redis"]
```

The contract has no architectural limit of two workers or one workload. The
physical gate deliberately uses two workers and one workload without making a
scalability claim.

The stable reconciliation, tombstone, outage, placement, and target-first
movement semantics are explained once in
[Distributed fleet](../../distributed-fleet.md). The approved contract below
owns the DIST-001 requirements; this overview does not restate them.

## Records

- [Approved contract](contract-v1alpha1.md)
- [Physical runbook](runbook.md)
- [Source implementation evidence](evidence/source-implementation.md)
- [Machine-readable state](manifest.yaml)
