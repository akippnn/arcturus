# Arcturus

Arcturus is a manifest-driven application platform for deploying immutable OCI releases to a single AlmaLinux host with rootless Podman, Quadlet, and user systemd.

> **Status:** post-`v0.99.0-rc.2` development. The active deployment lifecycle still runs through the tested Python/FastAPI compatibility service while Rust assumes OCI upload authorization in reversible slices. The Arcturus-owned OCI data plane is host-local, authenticated, loopback-only, read-only, and not yet the public CI upload path.

Arcturus deliberately targets the space between hand-maintained Compose deployments and a full container orchestrator. CI builds images and resolves their digests; Arcturus validates a release manifest, renders generated Quadlets, activates the release, verifies readiness, publishes routing state, and restores the previous healthy release when activation fails.

## Current focus

Arcturus is currently focused on making one Linux host behave like a small, auditable application platform: reproducible control-plane updates, immutable application releases, explicit ownership handoff, fail-closed routing, deterministic rollback, and diagnostics that another operator can reproduce from the same commit or bundle digest.

The v1.0 goal is not to imitate every Kubernetes feature. It is to establish a dependable single-host control plane with clear contracts and ordinary Linux failure modes that can be inspected through files, systemd, journald, Podman, HTTP health, and machine-readable receipts. Multi-host scheduling and failover belong after those contracts are stable.

## What it provides

- Digest-pinned, multi-component releases
- Long-running services, one-shot tasks, and systemd timers
- Rootless Podman Quadlets generated from one release manifest
- Dependency ordering and health/readiness gates
- Per-service deployment locks, audit state, and immutable release archives
- Automatic and operator-requested rollback
- Non-destructive disable, enable, and removal operations
- Podman secret references without embedding secret values in manifests
- Active-manifest publication for the registry/router
- A project bootstrap repository for web, worker, scheduled, and multi-component services
- Authenticated preflight before expensive application builds
- Transactional Compose-to-Quadlet ownership handoff
- Replayable, recorded host and project update workflows
- A copy-and-paste host validation and issue-reporting playbook

Arcturus does **not** build images on the production host, deploy floating tags, let Watchtower replace releases, or require Terraform/Compose for normal application updates.

## Architecture

```text
GitHub repository + GitHub Actions
        |
        | current compatibility: build + external digest reference
        | target: short-lived grant + direct OCI upload over Tailscale
        v
Arcturus control plane
  Rust OCI authorization -> accepted artifact receipt (next slice)
  Python/FastAPI lifecycle compatibility -> validate -> activate -> rollback
        |
        v
Arcturus-owned OCI storage + rootless Podman Quadlets + user systemd
        |
        +-> active manifest -> registry -> router -> operator-owned ingress
```

See [Architecture](docs/architecture.md) for component and trust-boundary details.

## Quick start

### 1. Prepare a supported host (current compatibility updater)

The current installer validates Python 3.12+, Node.js 22+, Podman 5.8+, systemd 257+, and the Podman Quadlet generator.

```bash
sudo -iu appsvc ./deploy/install-host.sh \
  --validate-only \
  --bundle 'registry.example.org/platform/arcturus@sha256:<digest>' \
  --host-user appsvc \
  --allowed-bind-root /home/appsvc/apps
```

Review the resolved installation with `--dry-run`. For the actual installation, use the replayable wrapper with the same persistent host arguments:

```bash
sudo -iu appsvc ./deploy/arcturus-host-update bootstrap \
  --bundle 'registry.example.org/platform/arcturus@sha256:<digest>' \
  --host-user appsvc \
  --allowed-bind-root /home/appsvc/apps
```

This installs `arcturus-host-update` into `~/.local/bin` and records the non-secret installer arguments. A later control-plane update becomes:

```bash
sudo -iu appsvc arcturus-host-update apply \
  --bundle 'registry.example.org/platform/arcturus@sha256:<new-digest>'
```

Remote deployment listeners should use a private address and a source-scoped firewall rule; loopback is the default.

Full instructions: [Host installation](docs/host-installation.md), [Host updates](docs/host-updates.md), and [Host validation and issue reporting](docs/host-validation.md).

### 2. Bootstrap an application repository

Use the companion **Arcturus Service Blueprint**:

```bash
./scripts/arcturus-update bootstrap \
  --project-dir /path/to/my-api \
  --service my-api \
  --type web \
  --image-repository registry.example.org/team/my-api \
  --domain api.example.org \
  --deploy-url 'http://<private-host-address>:9090' \
  --bundle 'registry.example.org/platform/arcturus@sha256:<digest>' \
  --non-interactive
```

Create a service-scoped deployment credential on the host as the rootless Arcturus user:

```bash
umask 077
arcturusctl token create \
  --database "$HOME/.config/arcturus/tokens.json" \
  --service my-api \
  --token-id my-api-ci \
  --output "$HOME/.config/arcturus/my-api-ci.token"
```

Copy only the output file's value into the project's protected `ARCTURUS_DEPLOY_TOKEN` CI secret. Existing blueprints still need registry push credentials while using the external-registry compatibility path. The target flow instead exchanges the service-scoped Arcturus token for a short-lived repository-scoped upload grant and pushes directly to the Arcturus OCI endpoint. Artifact receipts and deployment receipt enforcement must land before that target path replaces compatibility deployment.

The blueprint records its setup intent so future migrations can be applied by dropping in a newer blueprint and running `./scripts/arcturus-update apply`.

### 3. Operate a service

```bash
arcturusctl status --api-url http://127.0.0.1:9090 --service my-api
arcturusctl verify --api-url http://127.0.0.1:9090 --service my-api
arcturusctl rollback --api-url http://127.0.0.1:9090 --service my-api
arcturusctl disable --api-url http://127.0.0.1:9090 --service my-api
arcturusctl enable --api-url http://127.0.0.1:9090 --service my-api
arcturusctl remove --api-url http://127.0.0.1:9090 --service my-api
```

Tokens are read from `ARCTURUS_TOKEN_FILE` or `ARCTURUS_DEPLOY_TOKEN`; they are not accepted as command-line values. Use `journalctl --user` for runtime logs.

## Repository layout

| Path | Purpose |
| --- | --- |
| `deploy/` | Transitional Python/FastAPI lifecycle service, host installer/updater, CLI, tests, and bundle build |
| `modules/bus/` | Unix-socket event bus |
| `modules/registry/` | Active-manifest discovery and validation |
| `modules/router/` | Safe nginx vhost generation and routing receipts |
| `schemas/` | CUE schema covering legacy and current manifest APIs |
| `portal/` | Optional operator-owned ingress compatibility example; not the application release engine |
| `terraform-modules/` | Deprecated/compatibility host and Compose-era modules |
| `runners/` | GitHub Actions runner guidance; no registration credentials or generated state |
| `docs/` | Architecture, manifest, operations, security, migration, updates, roadmap, and release documentation |

## Documentation

- [Documentation index](docs/README.md)
- [Architecture](docs/architecture.md)
- [Manifest reference](docs/manifest-reference.md)
- [Host installation](docs/host-installation.md)
- [Host updates](docs/host-updates.md)
- [Host validation and issue reporting](docs/host-validation.md)
- [Operations](docs/operations.md)
- [Security model](docs/security.md)
- [Migration from Compose/Terraform](docs/migration.md)
- [Roadmap](docs/ROADMAP.md)
- [Release process](docs/release-process.md)
- [Machine-readable compatibility](COMPATIBILITY.json)

## Versioning and compatibility

Product versions and manifest API versions are independent:

- Product release candidate: `v0.99.0-rc.2`
- Stable product target: `v1.0.0`
- Current manifest API: `arcturus.u128.org/v2`
- Legacy routing/stack API: `arcturus.u128.org/v1`

The RC2 Service Blueprint requires the RC2 host capabilities `authenticated-preflight` and `legacy-compose-handoff`. The generated lockfile and the CLI preflight make that dependency explicit and reject an older host before application image builds begin. See [`COMPATIBILITY.json`](COMPATIBILITY.json).

Product v1.0 will stabilize the existing manifest API v2. See the [roadmap](docs/ROADMAP.md).

## Security

Keep the deployment API on loopback or a private network, use one service-scoped token per CI identity, and provision application secrets directly on the host. While compatibility deployment remains enabled, keep host pull-only credentials separate from CI push credentials; the target Arcturus OCI flow replaces the CI push credential with short-lived scoped grants. Report vulnerabilities using [SECURITY.md](SECURITY.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
