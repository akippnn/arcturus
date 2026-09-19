---
title: Raspberry Pi SD-card provisioning
kind: guide
lifecycle: operational
authority: Raspberry Pi provisioning procedure
summary: Prepare an AlmaLinux Raspberry Pi Arcturus host safely.
maintenance:
  - Provisioning inputs, safety checks, or first-boot behavior change.
nav:
  section: Operate Arcturus
  order: 21
---

# Raspberry Pi SD-card provisioning

`deploy/provision-rpi-sd` prepares an official AlmaLinux Raspberry Pi image and
stages an Arcturus host for automatic installation on first boot. The current
destructive writer supports macOS, where Disk Arbitration provides enough
metadata to reject internal, virtual, read-only, partition-only, and undersized
targets.

The workflow does not hardcode a disk, workstation interface, subnet, gateway,
DNS server, static address, AlmaLinux release, SSH key, hostname, Arcturus
bundle, or service account. Values are
discovered, selected interactively, or supplied explicitly for reproducible
automation.

## What it does

1. Lists only suitable external physical whole disks and shows their reported
   media name, bus, and capacity.
2. Derives the active workstation interface, address, subnet, gateway, and DNS
   from its current route and DHCP lease.
3. Excludes the workstation, gateway, ARP neighbors, an optional known DHCP
   range, and addresses that answer a live probe from static-IP suggestions.
4. Uses a checksum-pinned local `.raw`/`.raw.xz` image, an exact image URL, or
   discovers supported AlmaLinux server images from the configured repository.
5. Resolves the image SHA-256 from the official `CHECKSUM` file or verifies an
   explicitly pinned checksum.
6. Requires `--write` plus an exact destructive device confirmation before it
   unmounts or writes anything.
7. Streams the verified raw or compressed image to the removable device,
   verifies that its expanded size fits, mounts the image's `CIDATA` volume,
   and writes `user-data`, `meta-data`, and `network-config`.
8. Pre-authorizes the selected OpenSSH public key for AlmaLinux's default user,
   disables SSH password authentication, and stages the Arcturus first-boot
   installer and optional target registry credentials.
9. Optionally installs Tailscale from the matching RHEL-compatible package
   repository and enrolls the host using a protected auth-key file on first
   boot.
10. Ejects the completed card.

AlmaLinux documents `CIDATA` cloud-init customization, pre-boot SSH key
installation, and a separate static `network-config` for its Raspberry Pi
images in its [official Raspberry Pi guide](https://wiki.almalinux.org/documentation/raspberry-pi).

## Before running

Obtain a digest-pinned, multi-architecture Arcturus bundle containing an arm64
variant. The provisioner deliberately rejects floating bundle tags. If the
workstation needs a private-registry credential to extract that bundle, pass a
protected Podman-compatible file with `--bundle-registry-auth-file`.

On macOS, `xz` and either Podman or Docker must be installed in addition to the standard
`diskutil`, `dd`, `route`, `ipconfig`, `arp`, `ping`, and SSH tools. Podman is
preferred; either engine can pull the selected arm64 bundle and extract its
files without executing the foreign container. Use `--container-cli` to select
one explicitly. `--bundle-registry-auth-file` is Podman-specific; Docker uses
its own login store. This workstation credential is never copied to the card.
The script invokes `sudo` only after destructive confirmation and only for the
raw-device write.

If Tailscale enrollment is wanted, create a reusable, one-off, or ephemeral
auth key in the Tailscale admin console and store it in a local file readable
only by you. Prefer a short-lived, tagged key whose permissions are limited to
Arcturus hosts. Pass the filename, never the secret value, to the
provisioner. Tailscale SSH is not enabled unless `--tailscale-ssh` is also
given. The first-boot script detects the installed RHEL-compatible major and
follows Tailscale's documented
[package installation](https://pkgs.tailscale.com/stable/) rather than executing
a downloaded shell script.

## Use an existing Raspberry Pi 5 raw image

For the supplied AlmaLinux 10.1 GPT image, start with a plan-only run. The
SHA-256 below identifies the local file that was inspected on 2026-09-17; it
does not replace comparison with an AlmaLinux-published checksum when one is
available.

```bash
deploy/provision-rpi-sd \
  --image-file "$HOME/Downloads/AlmaLinux-10-RaspberryPi-gpt-10.1-20260520.aarch64.raw" \
  --image-sha256 '0a0333a504fc13c6a1922b0b56a4bf3e22c5184afbec25bf7fc1d2bc42cc14ee' \
  --tailscale-auth-key-file './tailscale-auth.key' \
  --arcturus-bundle 'ghcr.io/<owner>/<bundle>@sha256:<digest>'
```

The interactive flow selects a safe removable disk, discovers the active LAN,
offers unused addresses outside the declared DHCP pool, selects the target
Ethernet interface and SSH public key, and prints the complete plan. It does
not touch the card until the command is repeated with `--write` and the exact
`ERASE /dev/diskN` confirmation is entered. Do not copy a device name from an
example: reconnect the reader and let the script rediscover it each time.

## Safe plan-only run

Omit `--write` first. The script performs discovery and validation, prints the
resolved plan, and does not unmount, download the image, or write the card:

```bash
deploy/provision-rpi-sd \
  --arcturus-bundle 'registry.example.org/platform/arcturus@sha256:<digest>' \
  --tailscale-auth-key-file './tailscale-auth.key'
```

The interactive selectors then ask for:

- the external disk;
- an official AlmaLinux image and its GPT/MBR variant;
- a currently unresponsive address on the detected LAN;
- the target wired interface;
- hostname and rootless service account; and
- one available local or SSH-agent public key.

Use GPT for supported Raspberry Pi 4/5-class hardware. Raspberry Pi 3 cannot
boot a GPT image, so select an available MBR image. The image list comes from
the repository at runtime rather than a fixed filename.

## Write the card

After reviewing the plan, repeat the same command with `--write`. The script
will display the exact device and media name and require the phrase
`ERASE /dev/diskN`. Any other response stops before unmounting.

For non-interactive use, every mutable selection must be explicit and the
device must be repeated in `--confirm-device`:

```bash
deploy/provision-rpi-sd \
  --write --non-interactive \
  --device '/dev/diskN' \
  --confirm-device '/dev/diskN' \
  --static-ip '<address>/<prefix>' \
  --dhcp-range '<pool-start>-<pool-end>' \
  --target-interface '<target-interface>' \
  --hostname '<hostname>' \
  --ssh-public-key "$HOME/.ssh/id_ed25519.pub" \
  --alma-major '<supported-major>' \
  --partition-scheme gpt \
  --arcturus-bundle 'registry.example.org/platform/arcturus@sha256:<digest>' \
  --host-user '<service-account>'
```

An explicit `--image-file` with mandatory `--image-sha256` uses an existing
`.raw` or `.raw.xz` file without downloading it. An explicit `--image-url` and
`--image-sha256` can instead pin the exact remote OS image.
`--alma-repository`, `--compatibility-file`, `--host-interface`, and
`--cache-dir` provide reproducible overrides without modifying the script.

If the target host itself needs pull credentials for private workload images,
provide a separate pull-only file with `--target-registry-auth-file`. This is an
explicit target secret: it is staged on the card and installed into the chosen
service account's Podman configuration. It is not reused for the workstation's
bundle extraction.

The default `--bundle-delivery staged` pulls the arm64 bundle on the workstation
and copies its `/opt/arcturus` payload to the card before first boot. If the
`CIDATA` volume is too small or a workstation-side container engine is unavailable, select
`--bundle-delivery first-boot-pull`; that explicitly trades pre-staging for a
bundle pull by the Pi during first boot.

## Static-address safety

An address that is silent now can still be leased later. The picker cannot
discover every router's DHCP policy through standard client metadata. Prefer a
router-side reservation for the Pi's MAC address. Otherwise provide the known
DHCP pool with `--dhcp-range`; the selector excludes it and rejects an explicit
address inside it.

The script rejects the network address, broadcast address, gateway, current
workstation address, known ARP neighbors, and addresses that answer a probe. It
rechecks an explicit address immediately before producing the plan, but this is
collision detection—not a substitute for address management.

The initial provisioning slice configures a wired target interface. Wi-Fi
credentials and wireless first-boot behavior are deliberately not inferred or
embedded.

## First boot

On first boot, AlmaLinux cloud-init configures the hostname, static network, and
authorized key, discovers the mounted `CIDATA` filesystem by its image-defined
label, and then runs `arcturus-firstboot.sh`. That script:

- installs the runtime prerequisites from the selected AlmaLinux repositories
  and verifies that they satisfy Arcturus' current Python, Node.js, Podman, and
  systemd compatibility requirements;
- creates the selected rootless Arcturus service account;
- enables user lingering and its systemd user manager;
- installs the staged arm64 Arcturus payload, or pulls the digest-pinned bundle
  when `first-boot-pull` was explicitly selected;
- installs and enrolls Tailscale when `--tailscale-auth-key-file` was supplied;
  Tailscale SSH remains an explicit opt-in; and
- records `/var/lib/arcturus-firstboot/complete` before deleting credential
  files from the mounted `CIDATA` filesystem.

This is the pre-boot equivalent of `ssh-copy-id`: the public key is already in
the default user's `authorized_keys` when SSH starts, so no password login or
post-boot key-copy exchange is required. Connect using the selected static IP:

```bash
ssh almalinux@'<static-address>'
```

Inspect `/var/log/arcturus-firstboot.log`, `cloud-init status --long`, and the
user-systemd Arcturus services if installation does not complete. With
`first-boot-pull`, the bundle must be reachable from the Pi; with
the default staged delivery, it only needs to be reachable from the provisioning
workstation. The bundle must contain a compatible arm64 image. A plan-only run
or successful SD write does not prove the first-boot installation; that requires
booting the target and validating the installed host services.

## Credential handling

The card necessarily carries bootstrap credentials before its first boot.
Treat it as sensitive removable media. The first-boot script deletes the live
credential files after a successful install, but flash storage does not provide
reliable secure erasure guarantees. Rotate any staged target-registry credential
if the card is lost, duplicated, or handled outside the trusted provisioning path.
The same warning applies to the Tailscale auth key. The bootstrap copies it to a
root-only temporary file, removes that copy on exit, and removes the CIDATA copy
after successful enrollment. Use a short-lived or one-off auth key so a copied
card cannot enroll arbitrary additional machines indefinitely.
