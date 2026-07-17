# Security model

Arcturus is a deployment control plane. A party that can modify a trusted application manifest or use a valid deployment token can run that service's containers and request allowed host mounts. Security therefore depends on repository governance, service-scoped credentials, host isolation, and immutable release inputs.

## Supported security properties

- Rootless Podman for application and control-plane containers
- Fully qualified image digests; floating tags are rejected
- Service-scoped deployment tokens stored as salted scrypt verifiers
- Per-service operation locks
- Manifest schema with unknown-field rejection
- Secret-like environment keys rejected in favor of Podman secret references
- Bind mounts restricted to configured host roots
- Generated release archives and atomic active selection
- Health-gated activation and automatic rollback
- Structured output redaction for authorization, token, password, secret, API-key, and registry-auth fields
- Safe router input validation and configuration restoration after failed nginx validation/reload

## Deployment API exposure

Keep the API on loopback whenever CI runs on the host. Remote listeners must use a private address, source-scoped firewall access, and token authentication. Never publish the API directly to the internet through the public ingress.

One token should identify one service and one CI purpose. Do not share a global bearer token across repositories.

## Registry credentials

Use separate credentials:

- CI: push permission limited to the required repositories
- Host: pull-only permission

Pass passwords through stdin into protected auth files. Do not place registry credentials in image references, manifests, Git remotes, workflow arguments, or uploaded artifacts.

## Runtime secrets

Provision application secrets on the rootless host with Podman secrets. Manifests contain only the secret name, delivery type, and target.

Use versioned secret names for rotation. Keep the prior secret usable until rollback to the previous healthy release has been tested. Deployment and removal do not delete secrets.

## Host filesystem

Bind mounts are allowed only beneath configured roots. Keep control-plane state, token databases, registry auth, and systemd credentials outside application repositories with mode `0600` where appropriate.

Generated Quadlets and unit files are build artifacts. Host customization belongs in reviewed systemd drop-ins, not direct edits to active release files.

## CI runners

The service blueprint uses Buildah storage isolated to the current job. A normal workflow does not need a privileged container or the production host Podman socket. Treat runner registration tokens and generated runner state as secrets.

## Ingress and apex ownership

The router validates DNS names, aliases, ports, runtime identifiers, redirect targets, and bounded nginx values. No service may claim the configured base-domain apex unless `ARCTURUS_APEX_SERVICE` explicitly names it.

Ingress is operator-owned. TLS keys, ACME credentials, Cloudflare tokens, generated vhosts, and runtime logs must not be committed to the platform repository.

## Legacy compatibility risk

The deprecated `/deploy` endpoint, Terraform local provisioners, mutable host Git checkouts, Compose ownership, Watchtower, and broad runner socket access have a larger trust surface than the current release path. Keep them isolated during migration and remove them after no production workflow depends on them.

## Release security checks

Before publication or release:

- scan the full public history for secrets
- inspect the exact `git archive` output
- confirm no environment files, credentials, runtime databases, logs, generated vhosts, certificates, private inventory, or runner state are tracked
- run application tests and dependency audits
- verify GitHub workflows from a clean clone
- push only intended branches and tags

See [Release process](release-process.md) and [SECURITY.md](../SECURITY.md).
