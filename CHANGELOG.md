# Changelog

All notable public changes to Arcturus are documented here. The project follows semantic versioning for product releases; manifest API versions are tracked separately.

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
