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

Authenticate the service account to the registry with a pull-only credential before installation. The bundle contains the deployer, CLI, compiled bus/registry/router modules, Python wheels, and user units.

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

It creates the runtime socket directory, state directories, active-manifest directory, Quadlet directory, and the external routing network when absent. It enables user lingering so services can start without an interactive session.


## Optional local OCI data plane

The target GitHub-to-Arcturus artifact flow uses an Arcturus-owned OCI registry rather than Gitea. The current host slice installs storage on loopback only; remote upload authorization is added separately.

Enable it with a digest-pinned Distribution image:

```bash
./deploy/install-host.sh [existing options] \
  --oci-registry-image 'registry.example.org/distribution/distribution@sha256:<digest>' \
  --oci-registry-port 9443
```

Storage defaults to `~/.local/share/arcturus-registry`. The installer preserves a protected registry HTTP secret, keeps the registry read-only, disables manifest deletion, and verifies `http://127.0.0.1:9443/v2/` before completing.

This phase creates no public or tailnet listener and no CI registry credential. See [Arcturus OCI ingress](oci-ingress.md) for the boundary and staged rollout.

## Configuration preservation

Existing host configuration is preserved by default. Replacement requires the explicit force option supported by the installer. Review backups before removing old configuration.

Use systemd drop-ins under `~/.config/systemd/user/<unit>.d/` for host-specific overrides instead of editing installed units.

## Post-install checks

```bash
systemctl --user status arcturus-podman-api.service
systemctl --user status 'arcturus-deployer@*'
systemctl --user status arcturus-bus.service arcturus-registry.service arcturus-router.service
systemctl --user status arcturus-oci-registry.service  # when configured
podman network exists internal_routing
curl --fail http://127.0.0.1:9090/healthz
curl --fail http://127.0.0.1:9443/v2/  # when configured
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

Re-running the installer with the same host user and a new digest-pinned bundle preserves the generated deployer and platform settings when their flags are omitted, including allowed bind roots, router domain, certificate domain, vhost directory, nginx container, already-active private deployer listeners, and the optional loopback OCI image, port, and storage path. The installer switches the `current` release atomically and restarts the running platform services so they execute the new release immediately.
