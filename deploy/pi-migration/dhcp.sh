#!/bin/sh
# BusyBox udhcpc renews Ethernet leases throughout backup and recovery.
set -e
case "$1" in
  deconfig) rm -f /run/arcturus-ram-ready; ip address flush dev "$interface" ;;
  bound|renew)
    # Keep a renewed address in place so an existing backup SSH survives.
    if ! ip -4 address show dev "$interface" | grep -Fq "inet $ip/"; then
      ip address flush dev "$interface"
    fi
    ip address replace "$ip/$subnet" dev "$interface"
    for gateway in $router; do ip route replace default via "$gateway" dev "$interface"; break; done
    : > /etc/resolv.conf
    for nameserver in $dns; do echo "nameserver $nameserver" >> /etc/resolv.conf; done
    cp /recovery/nonce /run/arcturus-ram-ready
    ;;
esac
