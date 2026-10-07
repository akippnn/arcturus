#!/bin/bash
# Bounded SSH check of the staged recovery initramfs. Run on the Debian Pi 5.
set -euo pipefail
export LC_ALL=C

usage() {
    echo "Usage: $0 <32-character-lowercase-hex-nonce> <mmcblk0-CID>" >&2
    exit 2
}

[[ $# -eq 2 ]] || usage
nonce=$1
cid=$2
[[ $nonce =~ ^[0-9a-f]{32}$ ]] || usage
[[ $cid =~ ^[[:xdigit:]]{32}$ ]] || usage
[[ $(id -u) -eq 0 ]] || { echo 'Run this probe as root.' >&2; exit 1; }

boot=/boot/firmware
image=$boot/arcturus-recovery.img
test "$(findmnt -n -o SOURCE /)" = /dev/mmcblk0p2 || {
    echo 'Root is not the original /dev/mmcblk0p2 system.' >&2
    exit 1
}
test "$(findmnt -n -o SOURCE "$boot")" = /dev/mmcblk0p1 || {
    echo 'Firmware boot is not on the original /dev/mmcblk0p1.' >&2
    exit 1
}
grep -aq 'Raspberry Pi 5' /proc/device-tree/model || {
    echo 'This is not a Raspberry Pi 5.' >&2
    exit 1
}
test "$(cat /sys/block/mmcblk0/device/cid)" = "$cid" || {
    echo 'The supplied CID does not match /dev/mmcblk0.' >&2
    exit 1
}
test -s "$image" || { echo "Missing staged image: $image" >&2; exit 1; }
for tool in unmkinitramfs unshare chroot mount umount mknod cpio awk cut ip ssh-keygen; do
    command -v "$tool" >/dev/null || { echo "Missing prerequisite: $tool" >&2; exit 1; }
done

probe=$(mktemp -d /run/arcturus-pi-probe.XXXXXX)
mounted=
namespace_pid=
cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    if [[ -n ${namespace_pid:-} ]] && kill -0 "$namespace_pid" 2>/dev/null; then
        # This is the owned unshare supervisor. Its PID namespace contains the
        # server and its connection children, so ending it cannot affect host jobs.
        kill -TERM "$namespace_pid" 2>/dev/null || true
        for _ in {1..50}; do
            kill -0 "$namespace_pid" 2>/dev/null || break
            sleep 0.1
        done
        if kill -0 "$namespace_pid" 2>/dev/null; then
            kill -KILL "$namespace_pid" 2>/dev/null || true
        fi
        wait "$namespace_pid" 2>/dev/null || true
    fi
    if [[ -n $mounted ]]; then
        if ! umount "$probe"; then
            echo "Could not unmount probe RAM: $probe" >&2
            result=1
        fi
    fi
    rmdir "$probe" || result=1
    exit "$result"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# /run is often noexec. Extract only into this unique, private executable RAM.
mount -t tmpfs -o mode=0700,size=1024m,exec,dev,nosuid tmpfs "$probe"
mounted=1
unmkinitramfs "$image" "$probe/unpacked"
root=$probe/unpacked/main
test -d "$root" || root=$probe/unpacked
test -x "$root/bin/dropbear"
test -s "$root/etc/dropbear/host_key"
test -s "$root/root/.ssh/authorized_keys"
host_public=$(ssh-keygen -y -f /etc/ssh/ssh_host_ed25519_key | awk '{print $1, $2}')
image_public=$(awk 'NR == 1 {print $1, $2}' "$root/recovery/ssh_host_ed25519_key.pub")
test -n "$host_public" && test "$host_public" = "$image_public" || {
    echo 'The staged image host key does not match this Debian host key.' >&2
    exit 1
}
test "$(cat "$root/recovery/nonce")" = "$nonce" || {
    echo 'The staged image nonce does not match.' >&2
    exit 1
}
test "$(cat "$root/recovery/cid")" = "$cid" || {
    echo 'The staged image CID does not match the original card.' >&2
    exit 1
}
address=$(cut -d / -f1 "$root/recovery/address")
[[ $address =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || {
    echo 'The staged image has an invalid IPv4 address.' >&2
    exit 1
}
ip -o -4 addr show | awk '{sub(/\/.*/, "", $4); print $4}' | grep -Fxq "$address" || {
    echo "Staged wired address $address is not currently assigned on this Pi." >&2
    exit 1
}

# PID isolation lets namespace teardown remove Dropbear's accepted-connection
# children as well. The mount namespace keeps proc/devpts mounts out of Debian.
unshare --mount --pid --fork --kill-child --propagation private \
    /bin/bash -s -- "$root" "$address" "$nonce" <<'NAMESPACE' &
set -euo pipefail
root=$1
address=$2
nonce=$3

mkdir -p "$root/dev/pts"
mount -t proc proc "$root/proc"
mount -t devpts -o newinstance,ptmxmode=0666,mode=0620 devpts "$root/dev/pts"
ln -snf pts/ptmx "$root/dev/ptmx"
for device in 'null 1 3 666' 'zero 1 5 666' 'random 1 8 666' 'urandom 1 9 666' 'tty 5 0 666'; do
    read -r name major minor mode <<< "$device"
    rm -f "$root/dev/$name"
    mknod -m "$mode" "$root/dev/$name" "c" "$major" "$minor"
done

chroot "$root" /bin/dropbear -F -E -s -j -k \
    -p "$address:2223" -r /etc/dropbear/host_key &
server_pid=$!
for _ in {1..50}; do
    kill -0 "$server_pid" 2>/dev/null || {
        echo 'Staged Dropbear exited before becoming ready.' >&2
        wait "$server_pid"
    }
    sleep 0.1
done
echo "PROBE_READY $nonce $address 2223"
# Give the coordinator time to check key authentication and the staged payload.
sleep 55
kill -TERM "$server_pid" 2>/dev/null || true
for _ in {1..50}; do
    kill -0 "$server_pid" 2>/dev/null || break
    sleep 0.1
done
if kill -0 "$server_pid" 2>/dev/null; then
    kill -KILL "$server_pid" 2>/dev/null || true
fi
wait "$server_pid" 2>/dev/null || true
echo "PROBE_DONE $nonce"
NAMESPACE
namespace_pid=$!
wait "$namespace_pid"
namespace_pid=
