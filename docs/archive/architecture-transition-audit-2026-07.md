---
title: Architecture transition audit
kind: archive
lifecycle: archived
authority: Historical architecture audit
summary: Preserved July 2026 architecture transition findings.
maintenance:
  - No routine maintenance; preserve as historical evidence.
---

# Architecture transition audit

Original audit: 2026-07-17  
Last compatibility update: 2026-09-13

This is the historical RC2 audit. It is retained as migration evidence, not as
the current architecture or status authority.

## Retained changes

| Change | Decision | Reason |
| --- | --- | --- |
| Router and registry reconciliation hardening (#36) | Retain | Routing, manifest parity, and network idempotency remain required regardless of control-plane language or artifact source. |
| Podman pull/error and private-registry authentication hardening (#39) | Retain as compatibility | Existing deployments still use external digest references. Accurate failures and protected host credentials remain necessary during migration. |
| Rust OCI foundation (#40) | Retain | It established the target contracts and migration boundary. |
| Local OCI data plane (#44) | Retain | It is the storage foundation for direct CI uploads. |
| Rust upload authorization (#45) | Retain | Short-lived scoped grants and Distribution token issuance remain target components. |
| FastAPI 0.139.2 update (#42) | Retain temporarily | FastAPI still owns lifecycle operations until Rust parity is proven. |
| `tsx` 4.23.1 update (#43) | Retain | Bus and router remain TypeScript components. |
| Major GitHub Actions updates (#41) | Reapplied on then-current main | Only intended action-version changes were retained. |
| OCI authorization host integration (#46) | Materialized as normal source | Reviewed product changes replaced an encoded bootstrap patch. |

## Direction established by the audit

- Core CI could remain GitHub-hosted while application adapters stayed
  provider-neutral.
- External-registry deployment remained a compatibility path while projects
  could migrate to Arcturus-owned OCI receipts.
- Documentation had to distinguish the external-registry/Python lifecycle from
  the Arcturus-owned OCI/Rust migration path.
- Rust migration had to be incremental: Python activation, rollback, and
  recovery could not be removed before accepted parity.

## RC2 source disposition

The critical ingress path existed in source: authenticated Distribution
uploads, private HTTPS routing, server-side artifact verification, immutable
receipts, receipt enforcement, and fail-closed read-only installation.

Operational acceptance still required real CI and host evidence, retention and
garbage collection, signed host bundles, and accepted Rust lifecycle parity.
Those live gates are now tracked only in [Status](../status.md) and the current
[roadmap](../ROADMAP.md).
