#!/bin/bash
# Prepare the entire bootable image in RAM before writing the original card.
set -euo pipefail
export PATH=/bin:/usr/bin:/sbin:/usr/sbin
export LC_ALL=C
nonce=$1 cid=$2 size=$3 compressed_sha=$4 raw_sha=$5 raw_size=$6
loop=
mounted=
boot_mount=/run/alma-boot
prepared=/run/alma-prepared.raw
expected=/run/alma-prepared-boot.checksums

cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    if test -n "$mounted"; then
        if ! umount "$mounted"; then
            echo "Could not unmount $mounted; keep RAM recovery powered." >&2
            result=1
        fi
    fi
    if test -n "$loop"; then
        if ! losetup -d "$loop"; then
            echo "Could not detach $loop; keep RAM recovery powered." >&2
            result=1
        fi
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Restrict partition discovery to the supplied device, never a global label.
cidata_partition() {
    local disk=$1 found= part attempt
    for attempt in $(seq 1 20); do
        found=
        for part in "${disk}"p*; do
            test -b "$part" || continue
            if test "$(blkid -p -s LABEL -o value "$part" || true)" = CIDATA; then
                if test -n "$found"; then
                    echo "More than one CIDATA partition on $disk" >&2
                    return 1
                fi
                found=$part
            fi
        done
        if test -n "$found"; then
            local filesystem_type
            filesystem_type=$(blkid -p -s TYPE -o value "$found") || return 1
            if test "$filesystem_type" != vfat; then
                echo "CIDATA is not a FAT filesystem on $disk" >&2
                return 1
            fi
            printf '%s\n' "$found"
            return 0
        fi
        sleep 1
    done
    echo "No CIDATA partition on $disk" >&2
    return 1
}

verify_boot() {
    (cd "$boot_mount"; sha256sum -c "$expected")
    cmp "$expected" "$boot_mount/arcturus-recovery.checksums"
    for seed in user-data meta-data network-config; do
        cmp "/run/alma-seed/$seed" "$boot_mount/$seed"
    done
    cmp /run/alma-original-config.txt "$boot_mount/alma-config.txt"
    cmp /run/alma-original-config.txt "$boot_mount/tryboot.txt"
    cmp /run/persistent-recovery/recovery.img "$boot_mount/arcturus-recovery.img"
    cmp /recovery/boot/kernel.img "$boot_mount/arcturus-recovery-kernel.img"
    cmp /recovery/boot/recovery.dtb "$boot_mount/arcturus-recovery.dtb"
}

/recovery/ram-guard.sh "$nonce" "$cid" "$size"
rm -f /run/alma-flash-verified
test -f /run/alma.raw.xz
test -d /run/alma-seed
actual_compressed_sha=$(sha256sum /run/alma.raw.xz | cut -d ' ' -f1)
test "$actual_compressed_sha" = "$compressed_sha"
test "$raw_size" -le "$size"
for seed in user-data meta-data network-config; do test -s "/run/alma-seed/$seed"; done
test -s /run/persistent-recovery/recovery.img
test -s /recovery/boot/kernel.img
test -s /recovery/boot/recovery.dtb
# A failed decompression leaves any previously prepared image available.
xz -dc /run/alma.raw.xz > "$prepared.partial"
test "$(stat -c %s "$prepared.partial")" = "$raw_size"
actual_raw_sha=$(sha256sum "$prepared.partial" | cut -d ' ' -f1)
test "$actual_raw_sha" = "$raw_sha"
mv "$prepared.partial" "$prepared"
modprobe loop
loop=$(losetup --find --show --partscan "$prepared")
boot=$(cidata_partition "$loop")
mkdir -p "$boot_mount"
mount "$boot" "$boot_mount"
mounted=$boot_mount
test -s "$boot_mount/config.txt"
test ! -e "$boot_mount/autoboot.txt"
test ! -e "$boot_mount/tryboot.img"
if grep -Ei '^[[:space:]]*(boot_ramdisk|tryboot_a_b|os_prefix|include)[=[:space:]]' "$boot_mount/config.txt"; then
    echo 'Unreviewed AlmaLinux boot indirection; original card is unchanged.' >&2
    exit 1
else
    test "$?" = 1  # A grep read error must also stop before any card write.
fi

# Include FAT allocation overhead and metadata in addition to file payloads.
required=$((4 * 1024 * 1024 + 2 * $(stat -c %s "$boot_mount/config.txt")))
for payload in /run/persistent-recovery/recovery.img /recovery/boot/kernel.img /recovery/boot/recovery.dtb /run/alma-seed/user-data /run/alma-seed/meta-data /run/alma-seed/network-config; do
    required=$((required + $(stat -c %s "$payload")))
done
filesystem_space=$(stat -f -c '%a %S' "$boot_mount")
read -r free_blocks block_size <<< "$filesystem_space"
test "$((free_blocks * block_size))" -ge "$required"
cp "$boot_mount/config.txt" /run/alma-original-config.txt
cp /run/alma-original-config.txt "$boot_mount/alma-config.txt"
cp /run/alma-original-config.txt "$boot_mount/tryboot.txt"
cp /run/persistent-recovery/recovery.img "$boot_mount/arcturus-recovery.img"
cp /recovery/boot/kernel.img "$boot_mount/arcturus-recovery-kernel.img"
cp /recovery/boot/recovery.dtb "$boot_mount/arcturus-recovery.dtb"
printf 'console=serial0,115200 console=tty1 rdinit=/init panic=30\n' > "$boot_mount/arcturus-recovery-cmdline.txt"
cat > "$boot_mount/config.txt" <<'CONFIG'
[all]
arm_64bit=1
auto_initramfs=0
kernel=arcturus-recovery-kernel.img
device_tree=arcturus-recovery.dtb
cmdline=arcturus-recovery-cmdline.txt
initramfs arcturus-recovery.img followkernel
CONFIG
cp /run/alma-seed/user-data /run/alma-seed/meta-data /run/alma-seed/network-config "$boot_mount/"
(cd "$boot_mount"; sha256sum arcturus-recovery.img arcturus-recovery-kernel.img arcturus-recovery.dtb arcturus-recovery-cmdline.txt config.txt alma-config.txt tryboot.txt user-data meta-data network-config) > "$expected"
cp "$expected" "$boot_mount/arcturus-recovery.checksums"
sync
umount "$boot_mount"
mounted=
mount -o ro "$boot" "$boot_mount"
mounted=$boot_mount
verify_boot
umount "$boot_mount"
mounted=
losetup -d "$loop"
loop=
test "$(stat -c %s "$prepared")" = "$raw_size"
prepared_sha=$(sha256sum "$prepared" | cut -d ' ' -f1)

# No target write occurs before complete staged boot provisioning and readback.
/recovery/ram-guard.sh "$nonce" "$cid" "$size"
printf 'Writing fully prepared AlmaLinux image to SD. Remain powered.\n'
dd if="$prepared" of=/dev/mmcblk0 bs=4M conv=fsync status=progress
sync
blockdev --flushbufs /dev/mmcblk0
readback_sha=$(head -c "$raw_size" /dev/mmcblk0 | sha256sum | cut -d ' ' -f1)
if test "$readback_sha" != "$prepared_sha"; then
    echo 'SD readback differs from prepared image; keep RAM recovery powered.' >&2
    exit 1
fi
blockdev --rereadpt /dev/mmcblk0
partprobe /dev/mmcblk0
boot=$(cidata_partition /dev/mmcblk0)
mount -o ro "$boot" "$boot_mount"
mounted=$boot_mount
verify_boot
umount "$boot_mount"
mounted=
touch /run/alma-flash-verified
echo 'Prepared image readback, persistent recovery and headless seed verified. Reboot is a separate action.'
