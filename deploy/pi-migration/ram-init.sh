#!/bin/sh
# PID 1 runs wholly from initramfs. Never mount the original root automatically.
export PATH=/bin:/usr/bin:/sbin:/usr/sbin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /dev/pts /run /tmp
mount -t devpts devpts /dev/pts
mount -t tmpfs -o mode=0755,size=75% tmpfs /run
mount -t tmpfs -o mode=1777,size=16m tmpfs /tmp
exec >/dev/console 2>&1
echo 'Arcturus RAM recovery starting. SD card remains unmounted.'
for module in $(cat /recovery/modules); do modprobe "$module" 2>/dev/null || true; done
if test -x /sbin/udevd; then /sbin/udevd --daemon; fi
if test -x /lib/systemd/systemd-udevd; then /lib/systemd/systemd-udevd --daemon; fi
udevadm trigger --action=add 2>/dev/null || true
udevadm settle --timeout=20 2>/dev/null || true
interface=
for attempt in $(seq 1 30); do
    for nic in /sys/class/net/*; do
        if test "$(cat "$nic/address")" = "$(cat /recovery/mac)"; then interface=${nic##*/}; fi
    done
    test -n "$interface" && break
    sleep 1
done
if test -z "$interface"; then echo 'No matching Ethernet interface. Power-cycle for Debian.'; exec /bin/sh; fi
ip link set "$interface" up
address=$(cut -d / -f1 /recovery/address)
/bin/busybox udhcpc -f -i "$interface" -r "$address" -s /recovery/dhcp.sh -t 30 -T 2 &
dropbear -F -E -s -j -k -p 2222 -r /etc/dropbear/host_key &
while :; do sleep 60; done
