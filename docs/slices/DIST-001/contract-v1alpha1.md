---
title: DIST-001 multi-workload distributed fleet contract
kind: contract
lifecycle: immutable
authority: Approved normative slice behavior
summary: Required behavior and proof gates for the distributed fleet foundation.
maintenance:
  - Never edit after its first immutable identity; create a new contract version.
slice: DIST-001
version: v1alpha1
contract_state: approved
approval:
  authority: product owner
  record: Codex task approval and implementation instruction
  date: 2026-09-12
  immutable_identity: pending first commit
---

# DIST-001: Multi-workload distributed fleet foundation

## Outcome

Submit one existing blueprint release through `arcturusctl`, place and run it
through the existing `ServiceRelease v2` lifecycle on either of two workers,
observe live state, recover after a container failure, and move it target-first
without changing the application.

## Authentic path

`arcturusctl → WorkloadIntent → Rust placement → WorkerAssignment → outbound
agent → Python lifecycle → Podman → Quadlet → systemd → ObservedState →
arcturusctl status`

The physical cohort is two workers and one workload. The architecture and
contracts impose neither an exactly-two-worker nor an exactly-one-workload
limit. `ServiceRelease v2` remains one atomic placement and rollback unit.

## Required behavior

- A worker has stable enrolled identity, operator attestations, measured
  inventory, dynamic pressure, heartbeat, and a scoped credential.
- Inventory and pressure have separate generations. Pressure may reject new
  placement but never evicts, moves, or withdraws an existing assignment.
- Workload intent generations are per workload, assignment generations are per
  worker and workload, and assignment-set revisions are per worker.
- Polling returns a complete plural snapshot when its revision changes.
  Snapshot omission is inert; only explicit `ensureAbsent` removes a workload.
- Tombstones remain authoritative. Future collection requires durable
  acknowledgement of the exact absence generation.
- A worker durably accepts each workload independently before mutation. One
  workload's credential, failure, retry, or conflict cannot change another.
- Cached assignments remain desired through control-plane outages.
- Target-first movement waits for target health at the exact assignment
  generation and release digest before removing the source.
- Movement fails closed unless movability, overlap, authoritative state,
  migration, and fencing permit it. Mechanically stateless service-only
  releases need no separate evidence object.
- Imported resources have identity independent of releases. Redis material is
  projected through existing Podman secrets and applications retain RESP.
- Python/Pydantic remains authoritative for `ServiceRelease v2`. Rust stores
  its specification as opaque JSON and consumes only derived characteristics.

## Excluded

Replicas, component splitting, scored scheduling, automatic failover,
pressure-driven movement, state migration, fencing execution, tombstone
collection, volume transfer, overlay networking, global routing, a generic
provider framework, managed-resource provisioning, control-plane HA, FFI,
WASM/WIT, and new application data protocols are outside this slice.

## Gates

| Gate | Required proof |
| --- | --- |
| `DIST-001-CONTRACT` | Rust contracts and Python-derived characteristics agree on canonical fixtures. |
| `DIST-001-ORDER` | Stale, replayed, conflicting, plural, restart, and tombstone behavior is deterministic. |
| `DIST-001-ISOLATION` | Failure of workload A does not change workload B. |
| `DIST-001-AUTH` | Operator, worker, and service credentials cannot cross boundaries. |
| `DIST-001-PLACE` | Hard eligibility and refusal reasons are observable; pressure never changes an existing assignment. |
| `DIST-001-MOVE` | Target health precedes source removal; target failure preserves the source. |
| `DIST-001-OUTAGE` | Cached healthy workloads continue without the control plane. |
| `DIST-001-CLI` | Enrollment, listing, apply, status, explain, resource import, and move use `arcturusctl`. |
| `DIST-001-PATHS` | Rootless XDG defaults and explicit FHS roots use centralized resolution. |
| `DIST-001-HARDWARE` | The arm64/amd64 owner procedure passes with a pinned multi-architecture image and imported Redis. |

Owner acceptance remains pending until the physical hardware gate is
completed.

