---
title: Raspberry Pi same-card migration tooling
kind: guide
lifecycle: operational
authority: Pi migration backup and recovery procedure
summary: Preserve a Debian Pi, migrate its boot card through RAM, and restore workloads on AlmaLinux.
maintenance:
  - Recovery, backup, restore, or destructive-write prerequisites change.
---

# Raspberry Pi same-card migration

[deploy/pi-migrate](../deploy/pi-migrate) coordinates a Debian Pi 5 backup and
AlmaLinux migration over Ethernet. It pins the existing SSH host key, Ethernet
MAC, SD CID/capacity and a session nonce. DHCP rediscovery uses a bounded LAN
scan; Tailscale is not required. Each operation records its result on trusted
external storage.

## Backup and restoration scope

The backup includes an offline whole-card rollback image, root and boot
filesystem archives with numeric ownership, ACLs and extended attributes,
private system/user service and account inventory, subordinate-ID mappings,
stopped-container snapshots, and persistent Docker volumes. Additional mounted
storage and unsupported runtime settings stop the workflow for explicit handling.

Portable restoration deploys the owner's home, `/root`, `/opt`, `/srv`,
`/usr/local` and reviewed custom units. It appends the bootstrap SSH key,
relabels restored paths for SELinux, and imports container snapshots and volumes
into rootful Podman. Dedicated systemd units reproduce each workload's running
state and restart policy. Debian's Docker/containerd storage and vendor OS
files are retained in the complete backup rather than copied over AlmaLinux.

The current host adapter supports the captured HomeKit WOL, Cloudflare tunnel
and update timer, thermal governor, and per-interface CAKE network shaping
units. It recreates the HomeKit virtual environment using exact source package
pins and verifies the portable Cloudflare binary's hash. The exact protected
`/etc/cloudflared/token` is selected when the reviewed tunnel unit references it;
its ownership and permissions are checked before any host-service activation.
It preserves supported cron entries and user lingering. Unknown custom system/user units and ownership
mappings require implementation and acceptance before flashing; they are not
silently omitted.

Inventory and plans contain credentials and environment values. Never add the
backup session to Git. Transfers use protected `.partial` files and check that
the external volume remains mounted on the expected device. A partial artifact
cannot qualify as a verified backup.

## Operation sequence

Commands are deliberately separate so their evidence can be checked between
steps. Use `deploy/pi-migrate --help` for options. Put `--session` before each
subcommand after initialization:

```sh
deploy/pi-migrate discover --host 192.168.68.58 --scan-subnet 192.168.68.0/22
deploy/pi-migrate init --host 192.168.68.58 --backup-dir /mounted/external/backup
# Use the session directory printed by init for every following command.
deploy/pi-migrate --session /absolute/session preflight-source
deploy/pi-migrate --session /absolute/session fetch-image \
  --signing-key /trusted/AlmaLinux-10-key.asc \
  --signing-fingerprint EE6DB7B98F5BF5EDD9DA0DE5DEE5C11CC2A1E572
deploy/pi-migrate --session /absolute/session prepare-ram
deploy/pi-migrate --session /absolute/session probe-ram
deploy/pi-migrate --session /absolute/session export-images
deploy/pi-migrate --session /absolute/session boot-ram
deploy/pi-migrate --session /absolute/session backup
deploy/pi-migrate --session /absolute/session inspect-image
deploy/pi-migrate --session /absolute/session prepare-restore
deploy/pi-migrate --session /absolute/session verify-restoration
deploy/pi-migrate --session /absolute/session stage-image
deploy/pi-migrate --session /absolute/session flash --confirm-cid EXACT_BACKED_UP_CID
deploy/pi-migrate --session /absolute/session boot-alma
deploy/pi-migrate --session /absolute/session wait-alma
deploy/pi-migrate --session /absolute/session test-recovery
deploy/pi-migrate --session /absolute/session boot-alma
deploy/pi-migrate --session /absolute/session wait-alma
deploy/pi-migrate --session /absolute/session restore
deploy/pi-migrate --session /absolute/session upgrade-alma
# Exercise recovery and boot the upgraded kernel before permanent promotion.
deploy/pi-migrate --session /absolute/session test-recovery
deploy/pi-migrate --session /absolute/session boot-alma
deploy/pi-migrate --session /absolute/session wait-alma
deploy/pi-migrate --session /absolute/session resume-timers
deploy/pi-migrate --session /absolute/session verify-environment
deploy/pi-migrate --session /absolute/session promote
deploy/pi-migrate --session /absolute/session reboot-final
deploy/pi-migrate --session /absolute/session wait-alma
deploy/pi-migrate --session /absolute/session resume-timers
deploy/pi-migrate --session /absolute/session verify-environment
```

`prepare-ram` stages a matching kernel, physical-revision device tree and full
recovery initramfs through firmware `tryboot.txt`. Normal Debian boot remains
selected before flashing. `probe-ram` checks the exact image's SSH authentication
and tools in isolated RAM namespaces before stopping workloads. `export-images`
captures running state, stops reviewed lifecycle units and containers, and
exports snapshots without removing the original containers.

`boot-ram` requests a one-shot firmware boot. Keyed root recovery SSH uses port
2222. The guard checks the nonce, card identity, RAM root, every process mount
namespace, swap, loop/holder relationships and open card references. `backup`
reads the unmounted whole card and independently compares its raw checksum.
It archives the clean ext4 filesystem using a read-only, journal-free mount.
`verify` rechecks all backup hashes, gzip integrity and filesystem archive
metadata.

`inspect-image` checks the signed image in read-only RAM, including existing
UID/GID collisions, bootstrap tools, package inventory and kernel modules.
`prepare-restore` builds a selected portable bundle and dependency artifacts.
`verify-restoration` checks them against the source inventory and validates
local volume archives and ownership. Do not manually edit session acceptance
flags.

`flash` requires verified offline backup/restoration, a signed image, sufficient
RAM and the exact original CID. The writer provisions and checks the whole new
bootable image in RAM before the first SD write, detaches its loop devices,
repeats the exclusivity guard, writes the card and verifies readback. It keeps
the tested Debian kernel/initramfs as the new card's default RAM recovery;
AlmaLinux is initially selected only through one-shot `boot-alma`.
The writer runs in a detached RAM session with a private log and exit status,
so an SSH disconnect does not terminate the card write. `finish-flash` resumes
observation of that same job and never launches a second writer. Keep the Pi
powered while it runs. `stage-image` resumes only a checksum-matching prefix
of the signed OS image in RAM; it does not write the SD card.

If the Pi returns to recovery or has been power-cycled after the first Alma
request, `retry-boot-alma` can retry an unpromoted `alma-boot-requested` session.
It checks the preserved recovery kernel and device tree, mounts the pinned
CIDATA partition read-only, verifies all eleven frozen boot hashes, unmounts
it and repeats the exclusivity guard before requesting one tryboot. It records
public boot observations and preserves the existing phase and acceptance
flags; it never recreates the volatile flash marker or rewrites boot files.

`wait-alma` verifies Ethernet, the session nonce, SSH identity, account mapping,
cloud-init completion and on-card recovery hashes. Cloud-init exit code 2 is
accepted only for its exact existing-group warnings after checking the source
group names and actual owner membership. Other warnings and any stage errors
stop acceptance. `test-recovery` performs a
normal reset and proves that the preserved recovery system returns. `restore`
installs target dependencies, deploys selected files, imports Podman workloads
and adapts host services. `verify-environment` checks activity, enablement,
user lingering, stable container restart counts, published TCP connectivity
and boot payload integrity. `resume-timers` reproduces timers that were active
but disabled on the source without enabling them for future boots.
If firewalld is active, restoration opens the captured application TCP ports
and HomeKit mDNS discovery in the wired interface's zone.

If file deployment and Podman restoration completed but host restoration stopped,
`finish-restore` validates the frozen artifacts, recorded file result and live
runtime before continuing only the host adapter. It never recopies application
data or imports containers again. It installs the pinned cloudflared binary
atomically, preserving provenance for the source symlink. An attempt record
blocks automatic repetition after an ambiguous host-apply failure; inspect and
reconcile the actual service state before continuing.

For a proven partial host apply, the protected `reconcile-host.py` helper
requires SHA-256 pins for the frozen plan and current adapter. It checks exact
transformed service files, existing dependencies, cron and linger, then issues
only missing unit-state actions. It does not rewrite service files, rebuild a
virtual environment or change workload data. Retain the original attempt record
and record the actual reconciliation result before subsequent health checks.

For a read-only observation, use:

```sh
deploy/pi-observe --session /absolute/session
```

The observer checks only the saved SSH endpoints and verifies the nonce, wired
MAC, SD identity and OS/root state. Its public JSON omits credentials and private
session details; it does not update the session. A running Alma system reports
that its SD is mounted. Recovery acceptance requires the SD to be unmounted;
card writes still require the full recovery exclusivity guard.

This observer incorporates the earlier coordinator adapter into Arcturus as a
foundation for node-update checks. Signed fleet delivery, rollout policy and
agent self-update remain separate implementation work.

`upgrade-alma` updates the versioned official Pi image to current repository
packages. `promote` selects permanent AlmaLinux boot only after fallback,
repository-update and live environment verification. A final normal reboot and
verification establish that the migrated environment survives boot.

## Recovery

From reachable RAM recovery, restore the verified original whole card with:

```sh
deploy/pi-migrate --session /absolute/session rollback --confirm-cid EXACT_BACKED_UP_CID
```

Rollback verifies full-card readback and leaves reboot as a separate action.
Before any card write, `resume-source` can restart the original Debian workloads;
subsequent migration requires a fresh snapshot session. After flashing and
before promotion, an ordinary reset selects the on-card RAM recovery.
A power interruption during SD writing can still require an external card
reader: a RAM writer cannot make a single boot card immune to interrupted writes.

## Validation record

The local tests cover archive integrity and traversal, metadata deployment,
ownership, supported runtime/lifecycle planning, host-unit adaptation,
readback acceptance and guarded boot promotion. Physical RAM boot, Ethernet
recovery, exclusive-card checks, consistent snapshots and complete offline
backup verification passed on the target Pi 5 on 2026-10-01. The AlmaLinux
image write and full readback passed. On 2026-10-04, an unchanged one-shot
AlmaLinux boot passed headless SSH, wired identity, account mapping, first-boot
and frozen recovery-payload checks. A normal reset then returned to the pinned
RAM recovery and passed exclusivity checks. By 2026-10-05, selected files, all
three Podman workloads and six captured host-unit states were restored. The
repository upgrade passed live service, TCP-connectivity and all eleven frozen
recovery-file checks. The protected tunnel token was recovered from the fully
verified offline archive; host reconciliation issued only the missing network-shaping enable/start
actions. After the backup volume was reconnected and its original UUID verified,
the post-upgrade normal reset again returned to pinned RAM recovery. Upgraded
AlmaLinux boot, permanent promotion and final normal-boot acceptance passed.
The final running system reports AlmaLinux 10.2 and kernel
`6.12.96-20260724.v8.1.el10`, with synchronized time. All restored container
and host-unit states, published TCP connectivity and preserved recovery hashes
passed after the final reboot. The external session records the final proof in
`final-migration-verification.json`.

Image availability is checked from the official
[AlmaLinux Pi repository](https://repo.almalinux.org/almalinux/10/raspberrypi/images/).
The [AlmaLinux Pi guide](https://wiki.almalinux.org/documentation/raspberry-pi.html)
describes Pi 5 and CIDATA support. The
[Raspberry Pi firmware guide](https://www.raspberrypi.com/documentation/computers/config_txt.html)
documents custom kernel/initramfs and boot configuration.

## Optional Tailscale access

The wired migration does not depend on Tailscale. On 2026-10-05, Tailscale
1.102.4 was installed from its official Alma-compatible RPM repository after
permanent AlmaLinux boot acceptance. The original node state was recovered
from the checksum-verified offline archive while the new daemon was stopped.
The protected state files were verified before activation; source daemon
defaults matched the packaged defaults. No new node enrollment was needed.

The original `hori` identity rejoined at `100.103.104.22`, with MagicDNS name
`hori.opossum-arcturus.ts.net`. The daemon is active and enabled at boot.
Pinned keyed SSH and direct peer pings passed again after a daemon restart.
Access from a connected tailnet client uses ordinary OpenSSH:

```sh
ssh -i ~/.ssh/hori aki@100.103.104.22
# Or, when MagicDNS is enabled on the client:
ssh -i ~/.ssh/hori aki@hori.opossum-arcturus.ts.net
```

The session records `tailscale-access-verification.json`. Tailscale node state
contains credentials and must remain protected; preserve it only for this
original node rather than reusing it on other machines.
