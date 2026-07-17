# Arcturus roadmap

Arcturus is a practical single-host application platform. It is not intended to become a small Kubernetes distribution.

## Versioning model

Product versions and manifest APIs are independent:

- Current product candidate: `v1.0.0-rc.1`
- Stable release target: `v1.0.0`
- Operational status: acceptance pending; not yet declared stable
- Current release API: `arcturus.u128.org/v2`
- Legacy stack/routing API: `arcturus.u128.org/v1`

Product v1.0 preserves and stabilizes the current v2 manifest API.

## Current state

The main release path already supports:

- digest-pinned multi-component releases
- rootless Podman Quadlets and user systemd
- service, one-shot, and scheduled components
- dependency ordering and health-gated activation
- deployment history, explicit lifecycle operations, and automatic rollback
- active-manifest publication and router receipts
- project generation and CI integration through the service blueprint

The `v1.0.0-rc.1` source candidate contains the tested manifest-v2 lifecycle plus Arcturus-owned OCI upload grants, private Tailscale ingress, independent artifact verification, immutable receipts, and owned-registry deployment enforcement. External private registries and Python/FastAPI remain explicit compatibility paths while the Service Blueprint/CrownFi migration and real-host acceptance matrix are completed.

The control-plane language migration remains incremental. Rust owns artifact ingress; Python continues to own activation, rollback, and recovery until parity is proven.

# v0.99 — Public release candidate

## Goal

Ship a safe, coherent public preview of the implemented platform without adding unrelated providers or deployment strategies.

## Release gates

- Public source contains only reusable code, generic examples, and public documentation.
- Public history contains no private operations material or historical credentials.
- README and architecture documentation lead with the manifest/Quadlet path.
- GitHub validation workflows cover tests, builds, audits, syntax, schemas, and secret scanning.
- A clean clone reproduces all repository-local validation.
- Only the intended branch and release tags are published.

# v1.0 — Stable single-host core

## Goal

Freeze the current release contract and prove the platform on clean supported AlmaLinux hosts.

## Remaining work

- CPU, memory, and PID/process limits in the manifest and generated units
- conflict detection for domains, host ports, unit names, and application names
- operator-oriented host diagnostics
- stable compatibility/deprecation policy for the legacy API
- cleanup of test/runtime warnings
- complete clean-host acceptance documentation
- migrate the Service Blueprint and CrownFi CI to the receipt-producing publisher
- release-aware artifact pins, retention policy, and reviewed garbage collection
- live acceptance of the TLS-protected Tailscale OCI endpoint and failure-injection matrix
- incremental Rust control-plane ownership while retaining tested Python activation and rollback until parity is proven

Quadlet `.build` is excluded: production images are built in CI. Quadlet `.pod` remains optional until a real namespace-sharing requirement appears.

## Acceptance gates

1. Install from a digest-pinned bundle on a clean supported host.
2. Deploy public web, internal worker, scheduled, one-shot, and persistent multi-component examples.
3. Confirm repeated unchanged deployment is safe and does not recreate ownership unnecessarily.
4. Confirm invalid manifests change nothing on the host.
5. Confirm an unhealthy release automatically restores the prior healthy release and route.
6. Confirm intended services and timers recover after reboot.
7. Confirm removal preserves protected data and secrets.
8. Upgrade/reinstall the control-plane bundle without losing configuration.
9. Freeze and document the manifest API v2 contract.
10. Validate at least two independent application repositories through the public blueprint.

# v1.1 — Cloudflare offload

## Goal

Add an optional provider for workloads that benefit from moving object storage and database/API execution away from the shared-vCPU host.

## Planned scope

1. Provider lifecycle and ownership model
2. Cloudflare account/token references
3. R2 bucket import and provisioning
4. Scoped R2 credentials for VPS containers
5. Worker deployment and environment bindings
6. D1 import/provisioning and ordered migrations
7. Plan, status, output, and destruction previews
8. Automated R2 and D1 examples

D1 will not be represented as a remote SQLite server. VPS applications will use a narrowly scoped Worker API; Worker applications can use native D1 bindings.

# v2.0 — Extended production platform

Several capabilities originally imagined for v2 already exist in the current core: scheduled and one-shot workloads, dependency ordering, health promotion, digest locking, deployment history, journald logs, lifecycle operations, and automatic rollback.

Product v2 will focus on genuinely missing capabilities:

- backup, restore, retention, and restore verification
- environment overlays and reproducible promotion
- pre/post deployment hooks and backup-gated destruction
- additional ingress providers
- explicit local/managed resource abstractions
- blue-green and later canary strategies
- metrics, OpenTelemetry, and alerting adapters
- policy enforcement for root/privileged containers, capabilities, host networking, unsafe mounts, and missing limits
- optional image signature verification

# v2.1 and later — Distributed operation

Multi-host placement, cross-host discovery, distributed secrets/storage, failover, and fleet policy remain deferred until a concrete requirement cannot be met by the single-host design.

Arcturus will not adopt Kubernetes solely to claim multi-host support.

## Delivery order

1. Freeze and validate the `v1.0.0-rc.1` source candidate.
2. Migrate the Service Blueprint and CrownFi CI, then complete clean-host/live-upgrade/failure-matrix acceptance.
3. Add release-aware retention and reviewed garbage collection.
4. Publish the operationally accepted v1.0 release artifacts.
5. Add R2, Worker, and D1 support in independently testable slices.
6. Reassess v2 priorities using actual operational evidence from v1/v1.1.

## OCI ingress implementation sequence

1. **Complete:** Rust contracts, service-token verification, persisted upload grants, Registry v2 JWT issuance, and public JWKS.
2. **Complete:** Rootless persistent Distribution data plane on loopback with fail-closed read-only defaults.
3. **Complete in source:** Dedicated Tailscale Service ingress, private HTTPS validation, disk/concurrency limits, and authenticated write unlock.
4. **Complete in source:** Server-side manifest/config/layer verification, immutable artifact receipts, and manifest-v2 receipt enforcement for Arcturus-owned images.
5. **In progress:** Migrate the Service Blueprint and CrownFi workflows to `arcturus-oci-publish.sh` and remove their external registry dependency.
6. **Release gate:** Execute the clean-host, live-upgrade, real GitHub Actions, failure-injection, restart/re-pull, and registry-unavailable rollback acceptance matrix.
7. **Remaining reliability:** Add release-aware retention pins and reviewed garbage collection before enabling deletion.
