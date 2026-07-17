# Arcturus OCI ingress

Arcturus is moving application artifact storage into the platform so a deployment does not depend on a separate Gitea registry. The target flow is GitHub Actions over Tailscale to an Arcturus-owned OCI endpoint, followed by server-side policy verification and digest-pinned activation.

The current implementation slice installs only the **local, read-only OCI data plane**. It does not yet expose an upload endpoint to CI or accept artifact pushes.

## Current boundary

When configured, the host installer creates a rootless Distribution-compatible registry with these properties:

- the registry image must be pinned by `sha256` digest;
- the OCI endpoint binds only to `127.0.0.1`;
- persistent blobs live under the service account home by default;
- the registry HTTP secret persists across restarts so resumable upload state remains valid;
- abandoned upload data is purged after a safety period;
- manifest deletion remains disabled until artifact receipts and retention pins exist;
- the registry is read-only until Rust authorization and receipt enforcement are present;
- no route, firewall rule, Tailscale listener, or CI credential is created.

This makes storage available for the next Rust authorization and receipt slices without creating an unauthenticated remote registry.

## Install or preserve the data plane

Supply an operator-selected Distribution image by immutable digest:

```bash
./deploy/install-host.sh \
  --bundle 'registry.example.org/platform/arcturus@sha256:<arcturus-bundle-digest>' \
  --host-user appsvc \
  --vhosts-dir /home/appsvc/stacks/portal/config/nginx/vhosts.d \
  --oci-registry-image 'registry.example.org/distribution/distribution@sha256:<distribution-digest>' \
  --oci-registry-port 9443
```

The default storage directory is:

```text
~/.local/share/arcturus-registry
```

Override it only with an absolute path without whitespace:

```bash
--oci-registry-storage /srv/arcturus-registry
```

`arcturus-host-update bootstrap` records these options and replays them during later host updates. A direct installer upgrade also reads the existing protected OCI configuration when the options are omitted.

## Installed files

```text
~/.config/arcturus/oci-registry.env
~/.config/arcturus/oci-registry-runtime.env
~/.config/containers/systemd/arcturus/arcturus-oci-registry.container
~/.local/share/arcturus-registry/
```

Both environment files use mode `0600`. The runtime file contains the persistent registry HTTP secret and must not be committed or copied into application repositories.

The generated Quadlet uses `Pull=never`; the installer first ensures that the exact digest-pinned registry image exists in rootless Podman storage.

## Verify

```bash
systemctl --user status arcturus-oci-registry.service
curl --fail http://127.0.0.1:9443/v2/
stat -c '%a %n' \
  "$HOME/.config/arcturus/oci-registry.env" \
  "$HOME/.config/arcturus/oci-registry-runtime.env"
```

The two configuration files should report mode `600`. The OCI endpoint must not be reachable through a public or tailnet address in this phase. Loopback is a host-wide boundary rather than a per-user boundary, so read-only mode remains mandatory until authenticated ingress exists.

## Disable

```bash
./deploy/install-host.sh [existing host options] --disable-oci-registry
```

Disabling stops the generated service and removes its Quadlet and protected configuration. Stored blobs are intentionally preserved. Deleting artifact storage requires a separate reviewed operation.

## What comes next

The following capabilities are intentionally absent until the Rust control plane provides them:

1. `POST /v1/artifact-uploads` grant issuance;
2. standard Registry v2 Bearer-token authorization;
3. service/component/revision-scoped push access;
4. server-side manifest and layer policy verification;
5. immutable artifact receipts;
6. deployment receipt enforcement;
7. retention pins and reviewed garbage collection;
8. a TLS-protected Tailscale-facing OCI endpoint.

Do not point GitHub Actions at the loopback registry or add a permanent registry push token. The existing external private-registry path remains compatibility-only until these controls are complete.
