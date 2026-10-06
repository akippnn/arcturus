---
title: Resources and persistence
kind: reference
lifecycle: stable
authority: Resource ownership, binding, and persistence semantics
summary: How workloads bind to infrastructure without replacing native protocols.
maintenance:
  - Resource ownership, binding, movement, or persistence semantics change.
nav:
  section: Understand the system
  order: 12
---

# Resources and persistence

Arcturus separates application compute from the infrastructure it consumes.
Resources have fleet identity independent of a release, while applications
continue using each resource's native protocol.

## Binding

```mermaid
flowchart LR
  I["WorkloadIntent"] --> B["ResourceBinding"]
  B --> R["ResourceRecord"]
  R --> M["Protected material reference"]
  M --> S["Podman secret"]
  S --> A["Application"]
  A -->|native protocol| E["Resource endpoint"]
```

The fleet catalog stores resource identity and material references, not secret
values. A worker projects protected material through secrets already declared
by the release. Arcturus does not proxy RESP, SQL, S3, or other application
data protocols.

Podman `external` volumes and networks retain their existing meaning:
pre-existing host-local objects. They are not fleet ownership markers.

## Lifecycle ownership

```mermaid
flowchart TB
  C["Fleet resource catalog"] --> I["Imported resource"]
  C --> M["Managed resource"]
  I --> IB["Arcturus binds and observes<br/>another owner manages lifecycle"]
  M --> MB["Arcturus owns lifecycle<br/>through a proven resource integration"]
```

Imported and managed describe lifecycle ownership, not location. Managed
resources are added one concrete lifecycle at a time; the catalog does not
justify a generic provider framework by itself.

## Persistence and movement

Storage implementation does not establish whether movement is safe. The
authority and loss semantics of the data matter.

```mermaid
flowchart TB
  D["Workload data"] --> E["Ephemeral scratch"]
  D --> C["Reconstructable cache"]
  D --> L["Node-local authoritative state"]
  D --> X["Durable external or shared state"]
  E --> MOVE["May be recreated on target"]
  C --> MOVE
  L --> STOP["Movement needs migration or fencing"]
  X --> CHECK["Validate bindings and overlap"]
```

Target-first movement requires explicit safe overlap, no unresolved local
authority, and no unmet migration or fencing requirement. Unknown information
fails closed.

Backup remains separate from ownership and availability. A managed database
may back up to object storage, a game-world volume may snapshot off-site, and a
reconstructable cache may need no backup. A backup is not automatically a
replica or failover target.

## Worker knowledge

```mermaid
flowchart LR
  F["Measured facts<br/>capacity and free space"] --> E["Eligibility"]
  A["Operator attestations<br/>durability and topology"] --> E
  N["Device names"] -.->|never imply durability| E
  P["Podman content-addressed layers"] --> C["OCI image cache"]
```

Measured facts, live pressure, and operator claims remain separate. Podman's
digest-verified layer storage is the workload image cache; an additional
Arcturus cache requires a concrete need.

