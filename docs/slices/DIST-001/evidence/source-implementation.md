---
title: DIST-001 source implementation evidence
kind: evidence
lifecycle: immutable
authority: Captured source and local integration evidence
summary: Repository tests and operator-path smoke evidence for DIST-001.
maintenance:
  - Never rewrite captured results; add a new evidence record for a new attempt.
slice: DIST-001
captured_at: "2026-09-13"
source:
  repository: arcturus
  base_revision: 9d82dbd1853553e3c494d7238b4c8759f97d75fa
  state: dirty-working-copy
  change_identity: not captured by the original run
gates:
  DIST-001-CONTRACT: pass
  DIST-001-ORDER: pass
  DIST-001-ISOLATION: pass
  DIST-001-AUTH: pass
  DIST-001-PLACE: pass
  DIST-001-MOVE: pass
  DIST-001-OUTAGE: pass
  DIST-001-CLI: pass
  DIST-001-PATHS: pass
---

# DIST-001 source implementation evidence

## What passed

- The locked Rust workspace passed 41 tests, formatting, and strict Clippy.
  Coverage includes three-worker registration, plural assignment persistence,
  restart ordering, workload-local conflict isolation, admission-only pressure,
  refusal reasons, fail-closed movement, exact-digest target-first ordering,
  retained tombstones, and credential boundaries.
- The Python deployment, lifecycle, CLI, registry, provisioning, path, and
  cross-language fixture suite passed 106 tests.
- Host update, OCI publication, registry installation, Quadlet rendering,
  tailnet ingress, and version-consistency checks passed.

## Operator-path smoke

A loopback Rust control plane and the installed Python CLI were exercised
without handwritten HTTP or direct database edits. The flow created a
fleet-operator token, enrolled and listed two workers, imported the RESP
resource fixture, submitted an authoritatively validated intent, and exposed
stable refusal reasons because no physical agents were attached.

This found and repaired list-response handling in the CLI. It does not replace
the physical Podman, systemd, Redis, and movement gate.

## Repository findings

- `ServiceRelease v2` is the existing lock, archive, activation, routing, and
  rollback boundary.
- Podman `external` means a pre-existing host-local object, not fleet
  ownership or mobility.
- Python/Pydantic is the complete release validator; Rust intentionally keeps
  the release specification opaque.
- Existing lifecycle tokens are service-scoped, so agent credentials remain
  per service.
- Live systemd and routing observations supplement deployment-time health.

## Deviations resolved during implementation

- Runtime-root resolution became lazy so processes that need no runtime file do
  not require `XDG_RUNTIME_DIR`.
- Agent pressure sequencing moved into SQLite so ordering survives restart.
- The agent now repairs live unit drift through the idempotent lifecycle path
  and reports the actual active digest.
- The installer rejects a combined non-loopback fleet and OCI listener until
  the Rust service can separate those listeners.
- A pre-existing Rust lint was mechanically corrected so the strict gate could
  run.
- Raspberry Pi first-boot delivery now stages the path resolver with the
  installer.

## Limitation

The original run did not capture a binary-safe identity for the dirty working
copy, so this record proves local source behavior but is not a clean immutable
candidate. Physical evidence remains separate and pending.
