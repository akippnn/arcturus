# Arcturus documentation

Arcturus documentation is organized around the current manifest-driven Quadlet architecture. Compose and Terraform application deployment are retained only as a migration reference.

## Start here

1. [Architecture](architecture.md) — control-plane components, release flow, storage layout, and trust boundaries.
2. [Host installation](host-installation.md) — supported host requirements and installer behavior.
3. [Host updates](host-updates.md) — replaying recorded host configuration against a new digest-pinned bundle.
4. [Host validation and issue reporting](host-validation.md) — exact build, update, runtime, routing, and evidence checks for accepting a host release or filing a reproducible issue.
5. [Manifest reference](manifest-reference.md) — the `arcturus.u128.org/v2` release contract.
6. [Operations](operations.md) — status, verification, logs, rollback, disable/enable, and removal.
7. [Security model](security.md) — authentication, secret boundaries, host exposure, and release integrity.
8. [Arcturus OCI ingress](oci-ingress.md) — local artifact data plane, safety boundary, installation, and staged remote-ingress plan.
9. [Private registry authentication](private-registry-auth.md) — transitional host-owned pull credentials, preflight, rotation, and pull-failure behavior.
10. [Migration](migration.md) — moving production ownership away from Compose, Terraform provisioners, and Watchtower.

## Architecture decisions

- [ADR 0001: Arcturus-owned OCI ingress and Rust control plane](adr/0001-arcturus-owned-oci-ingress-and-rust-control-plane.md)
- [OCI upload grants and token authorization](oci-upload-authorization.md) — Rust preview contracts, credentials, JWT claims, and current non-production boundary.

## Project direction

- [Roadmap](ROADMAP.md)
- [Release process](release-process.md)
- [Changelog](../CHANGELOG.md)

## Deep dives

- [Manifest-driven Quadlet deployments](quadlet-deployments.md)
- [Legacy Terraform/Compose compatibility](legacy/README.md)

## Documentation contract

Public documentation must use example domains, users, paths, registries, and service names. Generated state, production inventory, credential material, private network details, and host-specific operational notes belong in a separate private operations repository.

- [Architecture transition audit](architecture-transition-audit.md) — retained compatibility, removed Gitea defaults, and remaining migration gates.
