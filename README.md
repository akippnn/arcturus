# Arcturus

Arcturus is a manifest-driven application platform for deploying immutable OCI releases to a single AlmaLinux host with rootless Podman, Quadlet, and user systemd.

> **Status:** `v0.99.0-rc.1`. The core release path is implemented and tested. The remaining v1.0 work is clean-host acceptance, resource-limit support, and freezing the public manifest contract.

Arcturus deliberately targets the space between hand-maintained Compose deployments and a full container orchestrator. CI builds images and resolves their digests; Arcturus validates a release manifest, renders generated Quadlets, activates the release, verifies readiness, publishes routing state, and restores the previous healthy release when activation fails.

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

Arcturus does **not** build images on the production host, deploy floating tags, let Watchtower replace releases, or require Terraform/Compose for normal application updates.

## Architecture

```text
application repository
        |
        | tests + Buildah build + registry push
        v
immutable image digests + ServiceRelease manifest
        |
        | authenticated deployment request
        v
Arcturus deployer
  validate -> pre-pull -> render -> generator check -> activate
        |                                        |
        |                                        +-> rollback on failure
        v
rootless Podman Quadlets + user systemd
        |
        +-> active manifest -> registry -> router -> operator-owned ingress
```

See [Architecture](docs/architecture.md) for component and trust-boundary details.

## Quick start

### 1. Prepare a supported host

The current installer validates Python 3.12+, Node.js 22+, Podman 5.8+, systemd 257+, and the Podman Quadlet generator.

```bash
sudo -iu appsvc ./deploy/install-host.sh \
  --validate-only \
  --bundle 'registry.example.org/platform/arcturus@sha256:<digest>' \
  --host-user appsvc \
  --allowed-bind-root /home/appsvc/apps
```

Review the resolved installation with `--dry-run`, then rerun without those flags. Remote deployment listeners should use a private address and a source-scoped firewall rule; loopback is the default.

Full instructions: [Host installation](docs/host-installation.md).

### 2. Bootstrap an application repository

Use the companion **Arcturus Service Blueprint**:

```bash
./scripts/arcturus-setup init \
  --project-dir /path/to/my-api \
  --service my-api \
  --type web \
  --image-repository registry.example.org/team/my-api \
  --domain api.example.org \
  --deploy-url 'http://<private-host-address>:9090' \
  --bundle 'registry.example.org/platform/arcturus@sha256:<digest>' \
  --non-interactive
```

CI needs separate registry push credentials and a service-scoped Arcturus deployment token. The generated workflow builds validation targets, publishes immutable images, resolves digests, renders the release, deploys it, and verifies the active state.

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
| `deploy/` | FastAPI deployment service, release engine, installer, CLI, tests, and OCI bundle build |
| `modules/bus/` | Unix-socket event bus |
| `modules/registry/` | Active-manifest discovery and validation |
| `modules/router/` | Safe nginx vhost generation and routing receipts |
| `schemas/` | CUE schema covering legacy and current manifest APIs |
| `portal/` | Optional operator-owned ingress compatibility example; not the application release engine |
| `terraform-modules/` | Deprecated/compatibility host and Compose-era modules |
| `runners/` | Conservative CI-runner guidance; no registration credentials or generated state |
| `docs/` | Architecture, manifest, operations, security, migration, roadmap, and release documentation |

## Documentation

- [Documentation index](docs/README.md)
- [Architecture](docs/architecture.md)
- [Manifest reference](docs/manifest-reference.md)
- [Host installation](docs/host-installation.md)
- [Operations](docs/operations.md)
- [Security model](docs/security.md)
- [Migration from Compose/Terraform](docs/migration.md)
- [Roadmap](docs/ROADMAP.md)
- [Release process](docs/release-process.md)

## Versioning

Product versions and manifest API versions are independent:

- Product release candidate: `v0.99.0-rc.1`
- Stable product target: `v1.0.0`
- Current manifest API: `arcturus.u128.org/v2`
- Legacy routing/stack API: `arcturus.u128.org/v1`

Product v1.0 will stabilize the existing manifest API v2. See the [roadmap](docs/ROADMAP.md).

## Security

Keep the deployment API on loopback or a private network, use one service-scoped token per CI identity, use separate pull-only and push-capable registry credentials, and provision application secrets directly on the host. Report vulnerabilities using [SECURITY.md](SECURITY.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
