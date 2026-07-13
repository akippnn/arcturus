# Arcturus architecture

Arcturus is a single-host application platform. It separates image construction, release declaration, host activation, and public ingress so that each layer has one owner.

## Design goals

- Immutable, digest-pinned application releases
- Rootless runtime ownership through Podman Quadlet and user systemd
- One validated manifest as the application release source of truth
- Deterministic rendering and generator validation before activation
- Health-gated promotion and automatic restoration of the last healthy release
- Explicit secret, storage, routing, and lifecycle ownership
- No production dependency on a mutable source checkout

## Components

| Component | Responsibility |
| --- | --- |
| Application repository | Source code, tests, Containerfiles, project build graph, and release template |
| CI | Test, build, push, resolve digests, render a concrete release, and call the deployment API |
| Deployment API | Authenticate the service identity and serialize deployment/lifecycle operations |
| Release engine | Validate, pre-pull, inspect, render, archive, activate, verify, and roll back releases |
| Quadlet/systemd | Own containers, networks, volumes, ordering, timers, restart policy, and boot persistence |
| Active-manifest store | Publish the currently selected release for registry and routing consumers |
| Bus and registry | Detect active-release changes and expose normalized routing state |
| Router | Generate vhosts safely, test/reload nginx, restore previous configuration on failure, and publish receipts |
| Ingress | Operator-owned TLS termination and public traffic handling |

## Release flow

1. CI validates application code and every configured image validation target.
2. CI builds release targets and pushes full-commit tags to the registry.
3. CI resolves each pushed image to `repository@sha256:digest`.
4. CI renders a concrete `ServiceRelease` and submits it with the matching 40-character Git revision.
5. The deployment API authenticates a token scoped to the requested service and acquires the per-service lock.
6. The release engine validates schema and references, checks host bind roots and required secrets, pre-pulls images, and verifies their digests.
7. Quadlet and systemd units are rendered into an immutable release directory and checked with the Podman systemd generator.
8. The active Quadlet symlink is switched atomically, systemd reloads, and the service target is activated.
9. Required service units, one-shots, timers, and Podman health checks must reach their accepted state before the release is marked active.
10. The active manifest is published. The registry/router updates ingress and records a revision/deployment-matched routing receipt.
11. If activation or readiness fails, Arcturus restores the previous Quadlet selection and validates the previous release. Failure and rollback are recorded separately.

## Workload modes

- `service` — web servers, workers, queue consumers, and other continuously running containers.
- `oneshot` — migrations, initialization, asset export, and other completion dependencies.
- `scheduled` — one-shot containers invoked by a generated systemd timer.

A multi-component application can combine all three modes and order them with `dependsOn`.

## Host layout

Paths are rooted in the service account home directory unless overridden by installation configuration.

| Path | Contents |
| --- | --- |
| `~/.local/share/arcturus-deployer/` | SQLite audit/operation state and deployer data |
| `~/.local/share/arcturus-deployer/releases/<service>/` | Immutable rendered release archives |
| `~/.local/share/arcturus-deployer/active-manifests/<service>/arcturus.json` | Current release publication |
| `~/.config/containers/systemd/arcturus/` | Active Quadlet symlinks |
| `~/.config/systemd/user/` | Service targets and Arcturus control-plane units |
| `$XDG_RUNTIME_DIR/arcturus/` | Private Podman, bus, and registry sockets plus router status |

## Networking and ingress

Applications use declared Podman networks. Routed components normally join the external `internal_routing` network so the operator-owned ingress can resolve container names. Internal components can use application-specific networks without publishing a route.

The router accepts only validated domains, aliases, ports, runtime names, and bounded nginx options. Apex ownership is denied unless `ARCTURUS_APEX_SERVICE` explicitly names the owning service. A failed nginx test or reload restores the prior configuration and records a redacted failure receipt.

Arcturus does not issue certificates or require one specific public ingress implementation. The included portal configuration is a compatibility example, not an application lifecycle owner.

## State and rollback

Release directories are immutable after creation. Activation changes generated runtime ownership, not application data. Rollback switches the active release selection and restarts the generated target.

Deployment, rollback, disable, enable, and remove do not delete:

- bind-mounted application data
- external named volumes
- Podman secrets
- release archives
- audit and operation records

Destructive storage operations must be separate and explicit.

## Trust boundaries

- A CI token is scoped to one service and should be stored only in protected CI secret storage.
- The deployment API is powerful and must remain on loopback or a private network with source restrictions.
- The rootless Podman API socket is dedicated to Arcturus control-plane services and must not be exposed to untrusted jobs.
- Application manifests can request commands and host bind mounts, so repository write access is equivalent to deployment authority for that service.
- Ingress, registry, DNS, backup, and host administration remain separate operational trust domains.
- Legacy `/deploy`, Terraform provisioners, and runner socket access carry broader authority and should be removed after migration.
