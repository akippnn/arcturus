# Arcturus roadmap

Arcturus is a practical single-host application platform. It is not intended to become a small Kubernetes distribution.

## Versioning model

Product versions and manifest APIs are independent:

- Current product candidate: `v0.99.0-rc.1`
- Stable product target: `v1.0.0`
- Current release API: `arcturus.u128.org/v2`
- Legacy stack/routing API: `arcturus.u128.org/v1`

Product v1.0 will stabilize the current v2 manifest API.

## Current state

The main release path already supports:

- digest-pinned multi-component releases
- rootless Podman Quadlets and user systemd
- service, one-shot, and scheduled components
- dependency ordering and health-gated activation
- deployment history, explicit lifecycle operations, and automatic rollback
- active-manifest publication and router receipts
- project generation and CI integration through the service blueprint

The existing deployment engine is feature-complete for a v0.99 preview. Before the stable release, artifact transport is being consolidated into an Arcturus-owned OCI ingress and the control-plane API is beginning a gradual Rust migration. External private registries and Python/FastAPI remain supported as explicit compatibility paths during that transition.

The transition is delivered in independently reversible slices: Rust contracts and health, loopback OCI storage, scoped upload authorization, artifact receipts, deployment enforcement, remote Tailscale ingress, and finally lifecycle migration from Python.

# v0.99 — Public release candidate

## Goal

Ship a safe, coherent public preview of the implemented platform without adding unrelated providers or deployment strategies.

## Release gates

- Public source contains only reusable code, generic examples, and public documentation.
- Public history contains no private operations material or historical credentials.
- README and architecture documentation lead with the manifest/Quadlet path.
- GitHub and Gitea validation workflows cover tests, builds, audits, syntax, schemas, and secret scanning.
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
- Arcturus-owned OCI upload grants, artifact receipts, and retention pins
- a TLS-protected Tailscale OCI endpoint with no permanent application registry token
- Rust ownership of the stable control-plane API while retaining tested compatibility rollback

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

1. Publish and operate v0.99.
2. Complete clean-host acceptance and release v1.0.
3. Add R2, Worker, and D1 support in independently testable slices.
4. Reassess v2 priorities using actual operational evidence from v1/v1.1.
