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
- `bash`, `sha256sum`, `getent`, and user systemd support

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

It creates the runtime socket directory, state directories, active-manifest directory, Quadlet directory, and the external routing network when absent. It enables user lingering so services can start without an interactive session.

## Configuration preservation

Existing host configuration is preserved by default. Replacement requires the explicit force option supported by the installer. Review backups before removing old configuration.

Use systemd drop-ins under `~/.config/systemd/user/<unit>.d/` for host-specific overrides instead of editing installed units.

## Post-install checks

```bash
systemctl --user status arcturus-podman-api.service
systemctl --user status 'arcturus-deployer@*'
systemctl --user status arcturus-bus.service arcturus-registry.service arcturus-router.service
podman network exists internal_routing
curl --fail http://127.0.0.1:9090/docs >/dev/null
```

Then create a service-scoped token, deploy a non-critical example, verify routing, and run the clean-host acceptance sequence described in [Release process](release-process.md).
