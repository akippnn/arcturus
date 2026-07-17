# Changelog

All notable public changes to Arcturus are documented here. The project follows semantic versioning for product releases; manifest API versions are tracked separately.

## [Unreleased]

## [1.0.0-rc.1] - 2026-07-17

This release freezes the `v1.0.0-rc.1` source candidate for the stable `v1.0.0` release. It completes the receipt-enforced private OCI path in source while retaining the proven manifest-v2 Python lifecycle. Operational stable-release acceptance still requires a real supported host, a live GitHub Actions upload, and the documented failure-injection matrix.

### Added

- Dedicated Tailscale HTTPS OCI ingress that proxies `/v2/` to loopback Distribution and API/token routes to loopback Rust
- Short-lived service/component-scoped upload grants and Ed25519 Registry v2 token issuance
- `POST /v1/artifact-uploads/{uploadId}/complete` with server-side manifest, descriptor, config, platform, revision, blob, and size verification
- Immutable artifact and layer receipts persisted in SQLite, including idempotent completion and artifact audit events
- Manifest-v2 deployment enforcement for Arcturus-owned images, bound to service, component, repository, revision, and digest
- Generic Buildah publisher implementing grant, login, push, completion, and receipt output
- Bounded concurrent artifact verification and a maximum layer-count policy

### Changed

- Distribution remains loopback-only and starts read-only; installation unlocks writes only after private HTTPS, Bearer challenge, authorization, and disk-headroom checks succeed
- Host upgrades atomically merge installer-managed configuration while preserving operator-defined keys and backing up replaced files
- External digest-pinned registries and the Python/FastAPI deployment, rollback, and recovery lifecycle remain supported compatibility paths
- Product, Rust workspace, and internal Node package versions are aligned on `1.0.0-rc.1`
- Rust CI and release builds use the fixed 1.97.1 toolchain rather than the superseded 1.97.0 compiler
- Release tags build the full test container—including Rust workspace tests—before publishing archives or bundles
- Local source-install identities include compiled Node artifacts, preventing stale release-directory reuse after module-only changes

### Not yet accepted as operationally stable

- Service Blueprint and CrownFi CI migration to the Arcturus publisher
- Release-aware retention pins and reviewed Distribution garbage collection
- Clean-host installation, live-host upgrade, real GitHub Actions upload, interruption/retry, registry-restart, registry-unavailable rollback, and Gitea-offline acceptance runs
- Rust ownership of deployment activation, rollback, and recovery; Python remains the tested lifecycle owner

## [0.99.0-rc.2] - 2026-07-15

### Added

- Replayable host bootstrap and upgrade workflow with recorded non-secret installer arguments and append-only update history
- Unauthenticated health and capability reporting plus authenticated service-access and deployment-preflight endpoints
- Preflight validation for token scope, Podman secrets, external volumes, external networks, and required host capabilities
- Machine-readable compatibility metadata for coordinated control-plane and blueprint releases

### Changed

- Legacy Compose ownership handoff is transactional and restores previously running containers when activation fails
- Host upgrades preserve installer-managed configuration and restart active deployment listeners
- CLI diagnostics now distinguish authentication, authorization, dependency-preflight, activation, and runtime failures
- Control-plane bundle packaging now includes the replayable host updater

## [0.99.0-rc.1] - 2026-07-13

### Added

- Manifest-driven `ServiceRelease` v2 deployment path
- Digest verification, immutable release archives, generated Quadlets, and user-systemd lifecycle
- Service, one-shot, and scheduled workload modes
- Multi-component dependencies, health-gated promotion, deployment history, and rollback
- Service-scoped token storage and redacted structured audit output
- Active-manifest registry and safe nginx router receipts
- Digest-pinned control-plane OCI bundle and host installer
- Public architecture, manifest, operations, security, migration, release, and roadmap documentation
- GitHub-native validation and secret-scanning workflows
- Tag-gated GitHub release packaging and monthly dependency-update configuration

### Changed

- Terraform and Compose application deployment are deprecated compatibility paths
- Apex-domain ownership is explicitly configured with `ARCTURUS_APEX_SERVICE`
- Public examples use generic domains, registries, users, and paths
- Internal Node packages are marked private

### Removed

- Production inventory, real service vhosts, runtime logs, generated CrowdSec state, runner registration data, and tracked environment files from the public source tree
