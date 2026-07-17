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

### Target Arcturus-owned artifact flow

1. GitHub Actions validates application code and every configured image target.
2. CI requests a short-lived upload grant scoped to one service, revision, and exact component repositories.
3. CI pushes OCI manifests and blobs directly to the private Arcturus endpoint over Tailscale.
4. Arcturus independently verifies manifests, descriptors, sizes, digests, and ownership, then records immutable artifact receipts.
5. A deployment may reference only receipts accepted for the same service, component, and Git revision.
6. The lifecycle service renders Quadlet and systemd units, activates them, verifies readiness, publishes routing state, and restores the previous healthy release on failure.

### Current compatibility flow

Until artifact completion and receipt enforcement are implemented, existing projects may still push images to an external registry and submit digest-pinned references. The Python/FastAPI lifecycle service pre-pulls and verifies those images before activation. This compatibility path is retained for live deployments and recovery; it is not the architecture for new integrations.

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
- Public ingress, DNS, backup, and host administration remain separate operational trust domains; application OCI storage is moving under Arcturus ownership.
- Legacy `/deploy`, Terraform provisioners, and runner socket access carry broader authority and should be removed after migration.
