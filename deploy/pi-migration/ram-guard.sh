#!/bin/bash
# Invoke before backup and immediately before every destructive write.
set -euo pipefail
export PATH=/bin:/usr/bin:/sbin:/usr/sbin
export LC_ALL=C
test "$(id -u)" = 0
test -f /run/arcturus-ram-ready
test "$(cat /run/arcturus-ram-ready)" = "$1"
test "$(cat /recovery/nonce)" = "$1"
test "$(cat /sys/block/mmcblk0/device/cid)" = "$2"
test "$(cat /recovery/cid)" = "$2"
test "$(blockdev --getsize64 /dev/mmcblk0)" = "$3"
test "$(cat /recovery/size)" = "$3"
test "$(uname -r)" = "$(cat /recovery/kernel-version)"
case "$(findmnt -n -o FSTYPE /)" in rootfs|tmpfs|ramfs) ;; *) echo 'Root is not RAM' >&2; exit 1;; esac
test "$(wc -l < /proc/swaps)" = 1
test -z "$(losetup -a)"
for dev in /sys/class/block/mmcblk0 /sys/class/block/mmcblk0p*; do
    test -e "$dev/dev" || continue
    test -z "$(ls -A "$dev/holders")"
    id=$(cat "$dev/dev")
    for mounts in /proc/[0-9]*/mountinfo; do
        test -r "$mounts" || continue
        if awk -v d="$id" '$3==d {found=1} END {exit !found}' "$mounts"; then
            echo "SD device mounted in $mounts" >&2; exit 1
        fi
    done
done
for dev in /dev/mmcblk0 /dev/mmcblk0p*; do
    test -b "$dev" || continue
    device_id=$(stat -Lc %r "$dev")
    for ref in /proc/[0-9]*/cwd /proc/[0-9]*/root /proc/[0-9]*/exe /proc/[0-9]*/fd/* /proc/[0-9]*/map_files/*; do
        test -e "$ref" || continue
        ids=$(stat -Lc '%d %r' "$ref" 2>/dev/null) || {
            test ! -e "$ref" && continue
            echo "Cannot inspect stable process reference: $ref" >&2; exit 1
        }
        read -r fs_id block_id <<< "$ids"
        if test "$fs_id" = "$device_id" || test "$block_id" = "$device_id"; then
            echo "SD filesystem or block device is held open: $ref" >&2; exit 1
        fi
    done
done
echo 'RAM independence, card identity and exclusive device checks passed.'
