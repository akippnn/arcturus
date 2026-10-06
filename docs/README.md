---
title: Arcturus documentation
kind: landing
lifecycle: stable
authority: Documentation navigation
summary: Find architecture, operations, references, delivery state, and history.
maintenance:
  - The documentation entrypoint or navigation categories change.
generated_by: docs/_tooling/vertical_slices.py
generated_sections:
  - catalog
---

# Arcturus documentation

Start here instead of searching the repository:

- [Architecture](architecture.md) explains the system and its boundaries.
- [Status](status.md) shows the current delivery gate without duplicating evidence.
- [Roadmap](ROADMAP.md) contains only approved outcomes.
- [Operations](operations.md) contains operator procedures and diagnostics.

The generated catalog below is built from each document's frontmatter. Product
ideas remain in tasks or issues until repository review and explicit owner
approval promote them into an ADR, contract, specification, or roadmap.

<!-- BEGIN GENERATED:catalog -->
## Start here

- [Architecture](architecture.md) — How operators, the control plane, workers, workloads, and resources fit together.
- [Delivery status](status.md) — Current product version, active slices, verdicts, and next gates.
- [Delivery roadmap](ROADMAP.md) — Owner-approved outcomes, dependencies, and evidence gates.

## Understand the system

- [Distributed fleet](distributed-fleet.md) — Desired state, placement, worker reconciliation, and movement semantics.
- [Resources and persistence](resources-and-persistence.md) — How workloads bind to infrastructure without replacing native protocols.
- [Security model](security.md) — Authentication, credential scope, transport, and secret-handling invariants.
- [Architecture decisions](decisions.md) — Index of durable architecture decisions.

## Operate Arcturus

- [Host installation](host-installation.md) — Install Arcturus roles and configure their filesystem roots.
- [Raspberry Pi SD-card provisioning](raspberry-pi-provisioning.md) — Prepare an AlmaLinux Raspberry Pi worker safely.
- [Host updates](host-updates.md) — Update installed Arcturus hosts while preserving layout and state.
- [Host validation and issue reporting](host-validation.md) — Validate a host and gather safe diagnostic evidence.
- [Operations](operations.md) — Operate services, fleet resources, workers, and recovery paths.
- [Release process](release-process.md) — Build, validate, and publish Arcturus releases.
- [Compose and Terraform migration](migration.md) — Move legacy Compose or Terraform deployments into ServiceRelease lifecycle.

## Reference

- [ServiceRelease manifest reference](manifest-reference.md) — Fields and behavior of the authoritative application release manifest.
- [Filesystem layout](filesystem-layout.md) — Canonical configuration, data, cache, and runtime roots.
- [Manifest-driven Quadlet deployments](quadlet-deployments.md) — How ServiceRelease manifests become Podman and systemd units.
- [OCI ingress](oci-ingress.md) — Registry ingress, authorization, and ownership boundaries.
- [OCI upload authorization](oci-upload-authorization.md) — Upload authorization and artifact acceptance behavior.
- [Private registry authentication](private-registry-auth.md) — Configure credentials for the supported external-registry path.
- [Release image-size policy](image-size-policy.md) — Accepted image size and bounded verification rules.
- [Changelog](../CHANGELOG.md) — Notable public changes grouped by product release.

## Delivery records

- [DIST-001 distributed fleet foundation](slices/DIST-001/README.md) — Place, observe, recover, and safely move one release across heterogeneous workers.
- [PATH-001 four-root filesystem transition](slices/PATH-001/README.md) — Centralize configuration, data, cache, and runtime path ownership.
<!-- END GENERATED:catalog -->

## History and migration context

- [Historical architecture audit](archive/architecture-transition-audit-2026-07.md)
- [Pre-v4 roadmap](archive/roadmap-pre-v4.md)
- [Legacy Compose and Terraform documentation](legacy/README.md)

Archived pages preserve context but do not define current architecture,
delivery state, or supported operator procedure.

## Documentation rules

- A mutable fact has one authority; other pages link to it.
- Stable explanations contain no live status badges, test counts, revisions, or
  artifact digests.
- Slice manifests own live delivery state. Evidence records own results and
  provenance. Generated pages project those authorities without replacing them.
- Public examples use generic domains, users, paths, registries, and service
  names. Private inventory and credentials stay outside this repository.
