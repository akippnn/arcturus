#!/bin/bash
# Runs on Debian. Builds and validates recovery in isolated executable RAM.
set -euo pipefail
export LC_ALL=C
stage=$1 nonce=$2 mac=$3 ip_address=$4 prefix=$5 gateway=$6 cid=$7 size=$8
refresh_nonce=${9:-}
boot=/boot/firmware
artifacts=(arcturus-recovery.img arcturus-recovery-kernel.img arcturus-recovery.dtb arcturus-recovery-cmdline.txt tryboot.txt)
test "$(id -u)" = 0
[[ "$nonce" =~ ^[0-9a-f]{32}$ ]]
stage_prefix=/run/arcturus-pi-stage.$nonce
if test "$stage" != "$stage_prefix"; then
    [[ "$stage" = "$stage_prefix".* ]]
    [[ "${stage#"$stage_prefix".}" =~ ^[0-9a-f]{8}$ ]]
fi
test "$(findmnt -n -o SOURCE /)" = /dev/mmcblk0p2
test "$(findmnt -n -o SOURCE "$boot")" = /dev/mmcblk0p1
grep -q 'Raspberry Pi 5' /proc/device-tree/model
test "$(cat /sys/block/mmcblk0/device/cid)" = "$cid"
test "$(( $(cat /sys/block/mmcblk0/size) * 512 ))" = "$size"
test "$(uname -m)" = aarch64
kernel=$(uname -r)
cmp "$boot/kernel_2712.img" "/boot/vmlinuz-$kernel"
if grep -Ei '^[[:space:]]*(include|os_prefix|kernel|cmdline|initramfs|device_tree|boot_ramdisk|tryboot_a_b)[=[:space:]]' "$boot/config.txt"; then
    echo 'Custom boot indirection needs manual review; refusing.' >&2
    exit 1
else
    test "$?" = 1
fi
if test -n "$refresh_nonce"; then
    test "$refresh_nonce" = "$nonce"
    test -s "$stage/boot-checksums"
    sha256sum -c "$stage/boot-checksums"
    for artifact in "${artifacts[@]}"; do test -f "$boot/$artifact"; done
else
    for artifact in "${artifacts[@]}"; do test ! -e "$boot/$artifact"; done
fi
full_tools=(bash dropbear dropbearconvert tar xz sha256sum head dd blockdev lsblk blkid mount umount findmnt losetup readlink stat cat sync ssh-keygen sfdisk partprobe modprobe e2fsck dumpe2fs cp mv rm cut cmp grep awk wc id ls mkdir seq sleep touch uname ip)
for tool in mkinitramfs unmkinitramfs cpio gzip gcc depmod chroot readelf find busybox dpkg "${full_tools[@]}"; do
    command -v "$tool" >/dev/null || { echo "Install prerequisite: $tool" >&2; exit 1; }
done
test -f /etc/ssh/ssh_host_ed25519_key
test -s "$stage/authorized_keys"
# Initial /init must work before any dynamic loader or normal boot scripts run.
busybox_headers=$(readelf -l "$(command -v busybox)")
if grep -q INTERP <<< "$busybox_headers"; then
    echo 'Recovery requires the installed busybox-static binary.' >&2
    exit 1
fi

build=$(mktemp -d /run/arcturus-pi-build.XXXXXX)
build_mounted=
pending=()
cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    if test "${#pending[@]}" -gt 0; then rm -f -- "${pending[@]}" || result=1; fi
    if test -n "$build_mounted"; then
        if ! umount "$build"; then
            echo "Could not unmount recovery build RAM: $build" >&2
            exit 1
        fi
    fi
    rmdir "$build" || result=1
    exit "$result"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
# /run may be noexec,nodev. Do not remount it or execute an unvalidated payload.
mount -t tmpfs -o mode=0700,size=1536m,exec,dev tmpfs "$build"
build_mounted=1
cp -a /etc/initramfs-tools "$build/config"
# Stock generation supplies firmware, existing device nodes and usr-merged layout.
mkinitramfs -d "$build/config" -o "$build/stock.img" "$kernel"
unmkinitramfs "$build/stock.img" "$build/unpacked"
root=$build/unpacked/main
test -d "$root" || root=$build/unpacked
mkdir -p "$root/bin" "$root/root/.ssh" "$root/etc/dropbear" "$root/recovery/boot" "$root/backup-root" "$root/backup-boot" "$root/proc" "$root/sys" "$root/dev"
gcc -Os -Wall -Werror "$stage/reboot-tryboot.c" -o "$build/reboot-tryboot"

# Populate after unpacking: hooks are not assumed to run, and stock applet
# aliases must never cause copy_exec's no-overwrite rule to skip full binaries.
export DESTDIR="$root" version="$kernel" MODULESDIR="/lib/modules/$kernel" verbose=n CONFDIR="$build/config"
DPKG_ARCH=$(dpkg --print-architecture)
export DPKG_ARCH
. /usr/share/initramfs-tools/hook-functions
for tool in "${full_tools[@]}"; do
    executable=$(command -v "$tool")
    rm -f "$root/bin/$tool"
    copy_exec "$executable" "/bin/$tool"
    cmp "$executable" "$root/bin/$tool"
    test "$(od -An -tx1 -N4 "$root/bin/$tool" | tr -d ' \n')" = 7f454c46
    # Stock /usr/sbin applets can otherwise shadow /bin during an SSH session.
    if test -e "$root/usr/sbin/$tool" && ! test "$root/usr/sbin/$tool" -ef "$root/bin/$tool"; then
        rm -f "$root/usr/sbin/$tool"
        ln -s "../bin/$tool" "$root/usr/sbin/$tool"
    fi
done
rm -f "$root/bin/busybox" "$root/bin/sh" "$root/bin/reboot-tryboot"
copy_exec "$(command -v busybox)" /bin/busybox
copy_exec "$build/reboot-tryboot" /bin/reboot-tryboot
ln -s busybox "$root/bin/sh"
# These libraries may be loaded through NSS rather than appear in ldd output.
for file in /lib/aarch64-linux-gnu/libnss_files.so.2 /lib/aarch64-linux-gnu/libnss_dns.so.2; do
    if test -e "$file"; then copy_exec "$file" "$file"; fi
done

# Discover the currently working Ethernet driver rather than infer it from a name.
interface=
for nic in /sys/class/net/*; do
    if test "$(cat "$nic/address")" = "$mac"; then interface=${nic##*/}; fi
done
test -n "$interface"
eth_module=
if test -e "/sys/class/net/$interface/device/driver/module"; then
    eth_module=$(basename "$(readlink -f "/sys/class/net/$interface/device/driver/module")")
fi
required_modules=(loop ext4 vfat sdhci_brcmstb)
if test -n "$eth_module"; then required_modules+=("$eth_module"); fi
mapfile -t loaded_modules < <(awk '{print $1}' /proc/modules)
# Recent Debian helpers defer copying until apply_add_modules. Older helpers
# copy immediately, so support either API while retaining one module list.
export __MODULES_TO_ADD="$build/modules-to-add"
: > "$__MODULES_TO_ADD"
manual_add_modules "${required_modules[@]}" "${loaded_modules[@]}"
if declare -F apply_add_modules >/dev/null; then
    set +u
    apply_add_modules
    set -u
fi
depmod -a -b "$root" "$kernel"
printf '%s\n' "${loaded_modules[@]}" "${required_modules[@]}" | sort -u > "$root/recovery/modules"

# Both silicon variants have the same root compatible/model. The live pinctrl
# driver identifies the exact original base DTB; never recycle old boot metadata.
d0=0 c0=0
while IFS= read -r -d '' property; do
    if grep -aFq 'brcm,bcm2712d0-pinctrl' "$property"; then d0=1; fi
    if grep -aFq 'brcm,bcm2712-pinctrl' "$property"; then c0=1; fi
done < <(find -L /proc/device-tree -type f -name compatible -print0)
test "$((d0 + c0))" = 1
if test "$d0" = 1; then dtb=$boot/bcm2712d0-rpi-5-b.dtb; else dtb=$boot/bcm2712-rpi-5-b.dtb; fi
test -s "$dtb"
test "$(od -An -tx1 -N4 "$dtb" | tr -d ' \n')" = d00dfeed
cp "/boot/vmlinuz-$kernel" "$root/recovery/boot/kernel.img"
cp "$dtb" "$root/recovery/boot/recovery.dtb"
printf '%s\n' "${dtb##*/}" > "$root/recovery/boot/source-dtb"
cp "$stage/ram-init.sh" "$root/init"
chmod 755 "$root/init"
cp "$stage/ram-guard.sh" "$stage/ram-flash.sh" "$stage/dhcp.sh" "$root/recovery/"
chmod 700 "$root/recovery/"*.sh
cp "$stage/authorized_keys" "$root/root/.ssh/authorized_keys"
chmod 700 "$root/root/.ssh"
chmod 600 "$root/root/.ssh/authorized_keys"
cp /etc/ssh/ssh_host_ed25519_key "$root/recovery/ssh_host_ed25519_key"
cp /etc/ssh/ssh_host_ed25519_key.pub "$root/recovery/ssh_host_ed25519_key.pub"
dropbearconvert openssh dropbear /etc/ssh/ssh_host_ed25519_key "$root/etc/dropbear/host_key"
printf 'root:x:0:0:RAM recovery:/root:/bin/bash\n' > "$root/etc/passwd"
printf 'root:*:19000:0:99999:7:::\n' > "$root/etc/shadow"
printf 'root:x:0:\n' > "$root/etc/group"
printf '/bin/sh\n/bin/bash\n' > "$root/etc/shells"
printf 'passwd: files\ngroup: files\nshadow: files\nhosts: files dns\n' > "$root/etc/nsswitch.conf"
printf '%s\n' "$nonce" > "$root/recovery/nonce"
printf '%s\n' "$cid" > "$root/recovery/cid"
printf '%s\n' "$size" > "$root/recovery/size"
printf '%s\n' "$mac" > "$root/recovery/mac"
printf '%s/%s\n' "$ip_address" "$prefix" > "$root/recovery/address"
printf '%s\n' "$gateway" > "$root/recovery/gateway"
printf '%s\n' "$kernel" > "$root/recovery/kernel-version"

# The exec/dev mount lets probes resolve the image's own interpreter and libs.
# They neither mount the card nor run /init or any recovery command.
chroot "$root" /bin/bash --noprofile --norc -c 'set -e; export PATH=/bin:/sbin:/usr/bin:/usr/sbin; for tool in "$@"; do command -v "$tool" >/dev/null; done; test "$(id -u root)" = 0' -- "${full_tools[@]}" reboot-tryboot
chroot "$root" /bin/sh -c ':'
available_applets=$(chroot "$root" /bin/busybox --list)
for applet in sh udhcpc reboot; do grep -qx "$applet" <<< "$available_applets"; done
if test ! -e "$root/bin/reboot"; then ln -s busybox "$root/bin/reboot"; fi
for tool in dd tar xz sha256sum stat cp mv rm head cat cut cmp grep wc id ls mkdir seq sleep touch uname blkid blockdev lsblk mount umount findmnt losetup sfdisk partprobe; do
    chroot "$root" "/bin/$tool" --version >/dev/null
done
chroot "$root" /bin/dropbear -V
chroot "$root" /bin/ip -Version >/dev/null
chroot "$root" /bin/awk 'BEGIN {exit 0}'
chroot "$root" /bin/e2fsck -V
chroot "$root" /bin/dumpe2fs -V
chroot "$root" /bin/ssh-keygen -l -f /recovery/ssh_host_ed25519_key.pub >/dev/null
for module in "${required_modules[@]}"; do chroot "$root" /bin/modprobe --dry-run "$module"; done
for script in /init /recovery/ram-guard.sh /recovery/ram-flash.sh /recovery/dhcp.sh; do chroot "$root" /bin/bash -n "$script"; done
(cd "$root"; find . -print0 | cpio --null -o -H newc --quiet | gzip -1) > "$build/recovery.img"
test "$(stat -c %s "$build/recovery.img")" -lt 134217728
printf 'console=serial0,115200 console=tty1 rdinit=/init panic=30\n' > "$build/recovery-cmdline.txt"
cat "$boot/config.txt" > "$build/tryboot.txt"
cat >> "$build/tryboot.txt" <<'CONFIG'

[all]
auto_initramfs=0
kernel=arcturus-recovery-kernel.img
device_tree=arcturus-recovery.dtb
cmdline=arcturus-recovery-cmdline.txt
initramfs arcturus-recovery.img followkernel
CONFIG
# A refresh only replaces artifacts proven to be from this same staged session.
if test -n "$refresh_nonce"; then
    sha256sum -c "$stage/boot-checksums"
    cmp "$build/tryboot.txt" "$boot/tryboot.txt"
    cmp "$build/recovery-cmdline.txt" "$boot/arcturus-recovery-cmdline.txt"
fi
sources=("$build/recovery.img" "$root/recovery/boot/kernel.img" "$root/recovery/boot/recovery.dtb" "$build/recovery-cmdline.txt" "$build/tryboot.txt")
for index in "${!artifacts[@]}"; do
    incoming=$boot/${artifacts[$index]}.$nonce.new
    test ! -e "$incoming"
    pending+=("$incoming")
    cp "${sources[$index]}" "$incoming"
done
sync
for index in "${!artifacts[@]}"; do cmp "${sources[$index]}" "${pending[$index]}"; done
# tryboot.txt is published last; the ordinary Debian boot files stay intact.
for index in "${!artifacts[@]}"; do mv "${pending[$index]}" "$boot/${artifacts[$index]}"; done
pending=()
sha256sum "$boot/arcturus-recovery.img" "$boot/arcturus-recovery-kernel.img" "$boot/arcturus-recovery.dtb" "$boot/arcturus-recovery-cmdline.txt" "$boot/tryboot.txt" > "$stage/boot-checksums"
sync
echo "RAM tryboot prepared and payload validated; DTB ${dtb##*/}. Original Debian config.txt and cmdline.txt are unchanged."
