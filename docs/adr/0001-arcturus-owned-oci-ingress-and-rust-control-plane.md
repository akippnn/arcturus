# ADR 0001: Arcturus-owned OCI ingress and Rust control plane

- Status: Accepted
- Date: 2026-07-17
- Decision owners: Arcturus maintainers

## Context

The existing application pipeline builds in GitHub Actions, pushes images to an external private registry, submits only digest references to Arcturus, and then makes the production host pull those images back from that registry. This divides one deployment transaction across two credential systems and two availability domains.

The external registry hop creates permanent application-side push credentials, a separate host-side pull credential, duplicate network transfer, and a failure mode where an otherwise healthy Arcturus host cannot activate a release because an unrelated registry is unavailable.

Arcturus is also currently split across Python/FastAPI for deployment lifecycle operations and TypeScript for bus, registry, and routing functions. The control-plane API needs stronger compile-time contracts, predictable resource use, and a migration path that does not require replacing every component simultaneously.

## Decision

### Source and update authority

GitHub is the source repository, CI provider, and release authority.

- Application builds originate in GitHub Actions.
- Arcturus application artifacts are uploaded directly to an Arcturus-owned OCI endpoint over Tailscale.
- Arcturus self-updates originate from signed GitHub Release bundles.
- Gitea is not part of the target application or control-plane update path.

### OCI data plane

Arcturus will initially run a proven OCI Distribution implementation as its blob and manifest data plane. Arcturus will not implement the OCI upload protocol from scratch.

The Arcturus control plane will own:

- short-lived, service- and component-scoped upload grants;
- repository authorization;
- image and layer policy;
- immutable artifact receipts;
- deployment eligibility;
- retention pins and garbage-collection intent.

A deployment may activate only a digest with an accepted receipt bound to the same service, component, and Git revision.

### Rust migration

A Rust workspace becomes the destination for control-plane APIs and lifecycle logic.

Migration follows a strangler pattern:

1. Introduce shared Rust contracts and a non-production Rust health endpoint.
2. Add OCI upload-grant and registry-token endpoints in Rust.
3. Add artifact verification and receipt state in Rust.
4. Move deployment read APIs and preflight into Rust.
5. Move activation, rollback, and lifecycle operations into Rust.
6. Remove Python/FastAPI only after contract parity, live shadow comparison, and rollback acceptance.

Python remains available behind explicit compatibility boundaries while routes are migrated. The TypeScript bus, registry, and router are not automatically included in this decision; each requires a separate migration decision.

### Compatibility mode

The external private-registry authentication path remains supported temporarily for existing deployments and emergency recovery. It is not the default architecture for new applications and must not be required by the Arcturus-owned OCI path.

## Security properties

- CI stores a service-scoped Arcturus control-plane token, not a permanent registry push token.
- Each OCI upload receives a short-lived token restricted to exact repositories and actions.
- Application manifests and deployment requests never carry registry credentials.
- The OCI endpoint is reachable only through private Tailscale connectivity and TLS.
- Artifact acceptance is based on server-side manifest and descriptor verification, not only CI-provided measurements.
- Active, previous-known-good, pending, and rollback artifacts are retention-pinned.

## Consequences

### Positive

- Gitea availability no longer affects application deployments.
- Artifact ownership, validation, and activation become one Arcturus transaction.
- OCI-standard clients retain resumable uploads, digest verification, and deduplication.
- Rust can replace FastAPI incrementally without a flag day.
- The existing external-registry fix remains useful as a bounded compatibility layer.

### Costs

- Arcturus becomes responsible for registry storage capacity, retention, and garbage collection.
- A registry authorization service and artifact receipt database must be operated safely.
- During migration, Rust and Python contracts must be tested against the same fixtures.
- Arcturus self-updates require a separate signed GitHub Release bootstrap path.

## Acceptance criteria

The decision is fully realized when:

- GitHub Actions pushes application images directly to Arcturus over Tailscale;
- upload tokens are short-lived and repository-scoped;
- deployments require accepted artifact receipts;
- Gitea can be offline or removed without affecting deployment or rollback;
- Rust owns artifact ingress authorization, receipts, and deployment lifecycle APIs;
- signed GitHub Releases are the supported Arcturus self-update source;
- Python/FastAPI is removed after verified parity and live-host acceptance.
