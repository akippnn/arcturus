# Architecture transition audit

Date: 2026-07-17

This audit reconciles the post-RC2 changes with the accepted Arcturus-owned OCI and gradual Rust migration architecture.

## Retained changes

| Change | Decision | Reason |
| --- | --- | --- |
| Router and registry reconciliation hardening (#36) | Retain | Routing, manifest parity, and network idempotency remain required regardless of control-plane language or artifact source. |
| Podman pull/error and private-registry authentication hardening (#39) | Retain as compatibility | Existing deployments still use external digest references. Accurate pull failures and protected host credentials remain necessary until receipt-enforced Arcturus ingress replaces that path. |
| Rust OCI foundation (#40) | Retain | This establishes the target contracts and migration boundary. |
| Local OCI data plane (#44) | Retain | This is the storage foundation for direct GitHub Actions to Arcturus uploads. |
| Rust upload authorization (#45) | Retain | Short-lived scoped upload grants and Distribution token issuance are target-architecture components. |
| FastAPI 0.139.2 update (#42) | Retain temporarily | FastAPI is still the installed lifecycle API. Removing or downgrading it before Rust lifecycle parity would break current deployment, rollback, and recovery operations. |
| `tsx` 4.23.1 update (#43) | Retain | Bus and router remain TypeScript components and still use the toolchain. |
| Major GitHub Actions updates (#41) | Reapplied on current main | The original branch conflicted with the newer Rust audit workflow; only the intended action-version substitutions were retained. |
| OCI authorization host integration (#46) | Materialized as normal source | The PR branch contained only an encoded patch and temporary self-modifying workflow. The reviewed product changes are committed directly and the bootstrap payload is excluded. |

## Removed or changed

- Gitea workflow definitions were removed. GitHub is the source repository, CI provider, and release authority.
- Gitea runner registration examples were removed.
- GitHub is now the default project CI provider; legacy `gitea` values remain parseable only for migration compatibility.
- Documentation now distinguishes the current external-registry/Python lifecycle compatibility path from the target Arcturus-owned OCI/Rust path.

## v1.0.0 source disposition

The critical ingress path is complete in source:

1. authenticated local Distribution uploads through short-lived Rust-issued credentials;
2. private Tailscale HTTPS routing with a same-origin token realm;
3. server-side manifest, config, descriptor, layer, size, platform, revision, and ownership verification;
4. immutable artifact/layer receipts with idempotent completion;
5. manifest-v2 receipt enforcement for Arcturus-owned images;
6. fail-closed read-only installation and upgrade behavior.

## Still intentionally incomplete

The `v1.0.0-rc.1` source candidate is not yet operationally accepted. Remaining gates are:

1. migrate the Service Blueprint and CrownFi CI to the generic Arcturus publisher;
2. execute the real GitHub Actions, clean-host, live-upgrade, interruption/retry, expiry, cross-service, oversize, digest-mismatch, registry-restart, and registry-unavailable rollback matrix;
3. implement release-aware retention pins and reviewed garbage collection before manifest deletion is enabled;
4. publish signature-verifiable GitHub Release host bundles and replace the compatibility updater;
5. migrate deployment read/preflight and later activation/rollback/recovery APIs from Python to Rust only after parity and rollback acceptance;
6. remove external-registry and Python/FastAPI compatibility only after the live path no longer depends on them.
