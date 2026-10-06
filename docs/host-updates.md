---
title: Host updates
kind: guide
lifecycle: operational
authority: Supported host update procedure
summary: Update installed Arcturus hosts while preserving layout and state.
maintenance:
  - Update, rollback, or bundle behavior changes.
nav:
  section: Operate Arcturus
  order: 22
---

# Updating an Arcturus host

`deploy/arcturus-host-update` is the current compatibility updater. It persists
only non-secret host installation arguments, installs itself into the resolved
binary directory, and replays those arguments against a digest-pinned OCI
bundle or a local release directory. The target stable updater will verify and
install signed host bundles published by GitHub Releases; that bootstrap path
is not yet implemented.

The underlying `install-host.sh` stages releases below the resolved deployer
data directory, switches the `current` symlink atomically, preserves existing
configuration unless explicitly replaced, refreshes generated user units, and
restarts services through user systemd.

## Record the first installation

Run the wrapper instead of calling `install-host.sh` directly:

```bash
sudo -iu appsvc ./deploy/arcturus-host-update bootstrap \
  --bundle 'registry.example.org/platform/arcturus@sha256:<64-hex-digest>' \
  --host-user appsvc \
  --listen-address 100.64.0.10 \
  --runner-cidr 100.64.0.0/24 \
  --allowed-bind-root /home/appsvc/apps \
  --network internal_routing \
  --base-domain example.org \
  --vhosts-dir /home/appsvc/stacks/portal/config/nginx/vhosts.d
```

The wrapper forwards all options to `install-host.sh`, then writes:

| Path | Purpose |
| --- | --- |
| `<config-root>/arcturus/host-install.json` | Current bundle/source, replayable non-secret installer arguments, last command, and updater checksum |
| `<data-root>/arcturus-deployer/host-install-history.jsonl` | Append-only successful host installation/update history |
| `<bin-dir>/arcturus-host-update` | Stable operator command for later upgrades |
| `<bin-dir>/arcturus-layout.json` | Protected bootstrap locator for the configured directories |

Both state files are mode `0600`; their parent directories are mode `0700`. Deployment tokens, registry credentials, application secrets, and token-file contents are never accepted or stored by the updater.

## Validate an upgrade

Use the digest from the new Arcturus bundle release:

```bash
sudo -iu appsvc arcturus-host-update apply \
  --bundle 'registry.example.org/platform/arcturus@sha256:<new-64-hex-digest>' \
  --validate-only
```

For the installer's resolved write plan without changing the saved update state:

```bash
sudo -iu appsvc arcturus-host-update apply \
  --bundle 'registry.example.org/platform/arcturus@sha256:<new-64-hex-digest>' \
  --dry-run
```

When the updater is running from its installed binary directory, it pulls the
new bundle, extracts that bundle's own installer and path resolver, and uses
them for the upgrade. This avoids relying on a stale Git checkout.

## Apply an upgrade

```bash
sudo -iu appsvc arcturus-host-update apply \
  --bundle 'registry.example.org/platform/arcturus@sha256:<new-64-hex-digest>'
```

The saved host arguments are replayed automatically. Use `--force-config` only when intentionally replacing `deployer.env` and `platform.env`; the installer makes timestamped backups first.

## Update from a local checkout

Development builds can be applied from a compiled local deploy directory:

```bash
sudo -iu appsvc arcturus-host-update apply \
  --source-dir /path/to/arcturus/deploy
```

The source directory must satisfy the same compiled-artifact validation as `install-host.sh`. Bundle-based updates remain the recommended production path because the bundle is immutable and records an exact digest.

## Inspect the recorded command

```bash
sudo -iu appsvc arcturus-host-update show
```

The output includes the installed source, application time, last exact update command, persisted host arguments, and the template for the next bundle update.

## Change host configuration

`apply` intentionally changes only the Arcturus release source and transient validation flags. To change listeners, routing, bind roots, firewall settings, or other persistent installer arguments, rerun `bootstrap` with the complete desired host configuration. A successful bootstrap replaces the saved replay contract; a failed installation does not modify it.

The replay contract includes the four filesystem roots, binary/workload roots,
and DIST-001 fleet-control/worker-agent role flags. A layout locator installed
beside the updater lets it find the saved configuration even when XDG variables
are not exported in a later shell. Secret values are never persisted.

Changing a root is a state migration, not an ordinary update. Follow
[Filesystem layout](filesystem-layout.md); the installer refuses to make
recorded or historical rootless state disappear merely because a new root
points at an empty directory.

## Recovery

The updater does not delete older releases. If an update needs to be reversed, rerun `arcturus-host-update apply` with the previous bundle digest. Application release rollback remains separate and continues to use `arcturusctl rollback` or the project lifecycle workflow.
