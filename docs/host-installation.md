# Host installation

Arcturus installs into a dedicated rootless service account and uses user systemd units. A production host should be treated as infrastructure: application repositories submit immutable releases but do not edit the host checkout.

## Supported baseline

The installer currently requires:

- AlmaLinux or a compatible systemd/SELinux distribution
- Python 3.12 or newer
- Node.js 22 or newer
- Podman 5.8 or newer
- systemd 257 or newer
- the Podman systemd/Quadlet generator
- `bash`, `curl`, `sha256sum`, `getent`, and user systemd support

Run the actual installer validation on the target host; package availability differs across distribution point releases.

## Account and directories

Create or choose a non-login-purpose service account such as `appsvc`. The account owns:

- Arcturus releases and state
- user systemd units
- rootless Podman storage
- the dedicated Arcturus Podman API socket
- permitted application bind roots

Do not run the application control plane as root.

## Installation sources

### Digest-pinned OCI bundle

Preferred for production:

```bash
sudo -iu appsvc /path/to/install-host.sh \
  --validate-only \
  --bundle 'registry.example.org/platform/arcturus@sha256:<digest>' \
  --host-user appsvc \
  --allowed-bind-root /home/appsvc/apps
```

The current OCI-bundle compatibility updater requires the service account to authenticate to the bundle registry with a pull-only credential before installation. The bundle contains the deployer, CLI, compiled bus/registry/router modules, the statically linked Rust `arcturusd` binary, Python wheels, and user units. Signed GitHub Release host bundles remain a later bootstrap slice.

### Local source directory

For development only:

```bash
sudo -iu appsvc ./deploy/install-host.sh \
  --source-dir ./deploy \
  --host-user appsvc \
  --allowed-bind-root /home/appsvc/apps
```

A source installation is inappropriate for release acceptance because it can depend on an uncommitted checkout or local Node/Python state.

## Validation and dry run

Use both before writing:

```bash
./deploy/install-host.sh [options] --validate-only
./deploy/install-host.sh [options] --dry-run
```

Validation checks required tools, versions, path ownership, image pinning, listener safety, the network name, unit conflicts, bind-root configuration, and optional firewall arguments.

## Listener exposure

The deployment API defaults to loopback. For remote CI, bind only to a private address:

```bash
./deploy/install-host.sh \
  --bundle 'registry.example.org/platform/arcturus@sha256:<digest>' \
  --host-user appsvc \
  --listen-address '<private-address>' \
  --runner-cidr '<trusted-runner-cidr>' \
  --configure-firewall \
  --allowed-bind-root /home/appsvc/apps
```

Wildcard listeners (`0.0.0.0` and `::`) are rejected. A private listener still requires token authentication and source restriction.

## Installed services

The installer provides user units for:

- the dedicated Podman API socket/service
- the deployment API listener(s)
- the event bus
- the active-manifest registry
- the router
- an optional loopback-only OCI artifact data plane
- an optional Rust OCI authorization service

It creates the runtime socket directory, state directories, active-manifest directory, Quadlet directory, and the external routing network when absent. It enables user lingering so services can start without an interactive session.


## Optional Arcturus-owned OCI ingress

The preferred GitHub-to-Arcturus path stores application images in a loopback Distribution registry and exposes it only through a dedicated Tailscale HTTPS Service.

Use a supported Distribution v3 image and pin it by digest. The `v1.0.0-rc.1` source freeze uses v3.1.1 as the current stable reference, including the upstream fix for CVE-2026-41888. The installer cannot infer security support from an arbitrary digest, so review upstream advisories and verify the selected release before installation.

For a storage-only compatibility installation, provide only the digest-pinned Distribution image. The registry remains read-only:

```bash
./deploy/install-host.sh [existing options] \
  --oci-registry-image 'registry.example.org/distribution/distribution:v3.1.1@sha256:<digest>' \
  --oci-registry-port 9443
```

For authenticated writable ingress, configure the complete boundary in one installation:

```bash
./deploy/install-host.sh [existing options] \
  --oci-registry-image 'registry.example.org/distribution/distribution:v3.1.1@sha256:<digest>' \
  --oci-registry-port 9443 \
  --enable-oci-auth \
  --oci-registry-host registry.example-tailnet.ts.net \
  --oci-tailscale-service svc:arcturus-oci
```

This mode:

- starts Distribution only on `127.0.0.1:<port>` and `arcturusd` only on `127.0.0.1:9190`;
- creates and preserves a protected Ed25519 signing seed, public JWKS, grant/receipt database, registry HTTP secret, and persistent blob storage;
- configures Distribution to trust only the Rust issuer and EdDSA JWKS;
- routes `/v2/*` to Distribution and API/token routes to Rust through private Tailscale HTTPS;
- verifies the service's anonymous `/v2/` response is the expected `401` Bearer challenge;
- requires Tailscale 1.98.9 or newer and enough free storage for at least two maximum-sized artifacts;
- unlocks writes only after all checks pass and returns the registry to read-only if post-unlock validation fails;
- enables manifest-v2 receipt enforcement for images hosted at the configured Arcturus registry hostname.

Storage defaults to `~/.local/share/arcturus-registry`. A local-source installation with `--enable-oci-auth` requires a compiled `deploy/arcturusd`; production bundles include it.

Disable token authorization with `--disable-oci-auth`, which returns the loopback registry to read-only mode and drains and clears any recorded dedicated Tailscale Service. Use `--disable-oci-registry` to remove the generated registry unit and managed configuration; it also clears the recorded Service. Blob storage is preserved in either case.

See [Arcturus OCI ingress](oci-ingress.md) and [OCI upload grants, verification, and receipts](oci-upload-authorization.md).

## Configuration preservation

Existing host configuration is preserved by default. Replacement requires the explicit force option supported by the installer. Review backups before removing old configuration.

Use systemd drop-ins under `~/.config/systemd/user/<unit>.d/` for host-specific overrides instead of editing installed units.

## Post-install checks

```bash
systemctl --user status arcturus-podman-api.service
systemctl --user status 'arcturus-deployer@*'
systemctl --user status arcturus-bus.service arcturus-registry.service arcturus-router.service
systemctl --user status arcturus-oci-registry.service  # when configured
systemctl --user status arcturusd.service  # when OCI authorization is enabled
podman network exists internal_routing
curl --fail http://127.0.0.1:9090/healthz
curl --fail http://127.0.0.1:9443/v2/  # registry without token authorization
curl --silent --output /dev/null --write-out '%{http_code}\n' \
  http://127.0.0.1:9443/v2/  # expect 401 when token authorization is enabled
```

Then create a service-scoped token on the host:

```bash
umask 077
arcturusctl token create \
  --database "$HOME/.config/arcturus/tokens.json" \
  --service my-api \
  --token-id my-api-ci \
  --output "$HOME/.config/arcturus/my-api-ci.token"
cat "$HOME/.config/arcturus/my-api-ci.token"
```

Copy only that final token value into the project CI secret named `ARCTURUS_DEPLOY_TOKEN`. The database stores a hash, while the output file contains the one-time credential. Token-file changes are read on each request and do not require restarting the deployment API.

An HTTP `401` means the token is absent or invalid. An HTTP `403` means the token is valid but not scoped to the requested service. A deployment response with HTTP `502` and JSON `status: failed` means authentication succeeded, activation failed, and Arcturus attempted rollback; inspect the returned `error` and `rollback` fields and the generated unit journals.

Deploy a non-critical example, verify routing, and run the clean-host acceptance sequence described in [Release process](release-process.md).

## Upgrade behavior

Re-running the installer with the same host user and a new digest-pinned bundle preserves the generated deployer and platform settings when their flags are omitted, including allowed bind roots, router domain, certificate domain, vhost directory, nginx container, already-active private deployer listeners, and the optional loopback OCI image, port, storage path, and authorization mode. The installer switches the `current` release atomically and restarts the running platform services so they execute the new release immediately.
