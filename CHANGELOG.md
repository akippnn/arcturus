# Changelog

All notable public changes to Arcturus are documented here. The project follows semantic versioning for product releases; manifest API versions are tracked separately.

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
