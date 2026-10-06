---
title: Arcturus
kind: landing
lifecycle: stable
authority: Public product overview
summary: Distributed application and infrastructure platform built around OCI workloads.
maintenance:
  - Product positioning or primary entrypoints change.
---

# Arcturus

Arcturus runs ordinary OCI applications across Linux workers while preserving
a small, inspectable local lifecycle: rootless Podman, Quadlet, and user
systemd. A Rust fleet layer adds desired state, placement, reconciliation, and
resource binding without turning applications into Arcturus-specific software.

Arcturus sits between hand-maintained Compose deployments and a full container
orchestrator. It is not a Kubernetes distribution, and applications do not
need an Arcturus SDK.

See [Architecture](docs/architecture.md) for the system topology rather than a
second copy of it here.

## Start here

- [Documentation](docs/README.md)
- [Architecture](docs/architecture.md)
- [Current delivery status](docs/status.md)
- [Host installation](docs/host-installation.md)
- [Operations](docs/operations.md)
- [Delivery roadmap](docs/ROADMAP.md)

## Boundaries

- `ServiceRelease v2` is the atomic worker-local activation and rollback unit.
- Workers initiate fleet communication and continue reconciling cached desired
  state during control-plane outages.
- Only explicit desired state removes a fleet workload.
- Existing local deployments are never adopted implicitly.
- Compute placement and resource placement are independent.
- Arcturus manages resource lifecycle and binding while applications retain
  standard protocols such as HTTP, RESP, S3, SQL, TCP, and UDP.
- Git may store desired configuration or trigger automation, but it is not an
  executing orchestrator.

## Repository map

| Path | Responsibility |
| --- | --- |
| `deploy/` | Python lifecycle, CLI, installer, updater, and tests |
| `rust/bins/arcturusd/` | Rust OCI authorization and fleet control plane |
| `rust/bins/arcturus-agent/` | Outbound multi-workload worker agent |
| `modules/` | Bus, active-manifest registry, and router |
| `schemas/` | CUE release schemas |
| `docs/` | Architecture, operations, contracts, evidence, and history |
| `terraform-modules/` | Deprecated compatibility modules |

Python/Pydantic remains authoritative for release validation and execution.
Rust treats the embedded release as opaque and owns additive fleet and OCI
control-plane paths.

Product compatibility is machine-readable in
[`COMPATIBILITY.json`](COMPATIBILITY.json). Security guidance is in
[Security](docs/security.md) and the [reporting policy](SECURITY.md).

Licensed under the [Apache License 2.0](LICENSE).
