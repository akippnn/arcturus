---
title: Pre-v4 roadmap
kind: archive
lifecycle: archived
authority: Historical roadmap
summary: Preserved roadmap state from before the v4 realignment.
maintenance:
  - No routine maintenance; preserve as historical context.
---

# Archived pre-v4 roadmap

This page preserves the roadmap framing used before the distributed fleet and
four-root filesystem realignment. It is historical context, not current status
or delivery order.

## v0.99 — Public release candidate

The goal was to ship a safe public preview of the implemented host-local
platform without adding unrelated providers or deployment strategies. Its
release gates covered public-source hygiene, removal of private operations
material and credentials, manifest/Quadlet-led documentation, GitHub
validation, clean-clone reproducibility, and controlled branch/tag publishing.

## v1.0 — Stable single-host core

The goal was to freeze and prove the host-local release contract on clean
AlmaLinux hosts. Planned work included resource limits, conflict detection,
host diagnostics, legacy deprecation policy, test cleanup, clean-host evidence,
Service Blueprint and CrownFi OCI-receipt migration, retention, garbage
collection, real private-ingress evidence, and incremental Rust ownership.

The acceptance matrix called for clean digest-pinned installation; web,
internal, scheduled, one-shot, and persistent examples; idempotency; invalid
manifest safety; rollback; reboot recovery; data-preserving removal; upgrades;
manifest-v2 freeze; and two independent blueprint consumers.

Quadlet `.build` was excluded because production images are built in CI.
Quadlet `.pod` remained optional until a real namespace-sharing requirement.

## Historical OCI ingress sequence

1. Rust contracts, service-token verification, persisted upload grants,
   Registry v2 JWT issuance, and public JWKS.
2. Rootless persistent Distribution on loopback with fail-closed read-only
   defaults.
3. Dedicated Tailscale Service ingress, private HTTPS validation, resource
   limits, and authenticated write unlock.
4. Server-side artifact verification, immutable receipts, and manifest-v2
   receipt enforcement for Arcturus-owned images.
5. Migrate application workflows to the common OCI publisher.
6. Execute clean-host, live-upgrade, CI, failure-injection, restart/re-pull, and
   registry-unavailable rollback acceptance.
7. Add release-aware retention pins and reviewed garbage collection before
   enabling deletion.

The current product direction and gates are in the [roadmap](../ROADMAP.md) and
[status page](../status.md).
