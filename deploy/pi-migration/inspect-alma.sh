#!/bin/bash
# Inspect the authenticated image from RAM; never mount or write the SD card.
set -euo pipefail
export PATH=/bin:/usr/bin:/sbin:/usr/sbin
export LC_ALL=C
umask 077

fail() { echo "$*" >&2; exit 1; }
test "$#" = 6 || fail 'Expected nonce CID card-size compressed-SHA raw-SHA raw-size.'
nonce=$1 cid=$2 size=$3 compressed_sha=$4 raw_sha=$5 raw_size=$6
[[ $nonce =~ ^[0-9a-f]{32}$ && $cid =~ ^[0-9a-f]{32}$ ]] || fail 'Invalid session or card identity.'
[[ $compressed_sha =~ ^[0-9a-f]{64}$ && $raw_sha =~ ^[0-9a-f]{64}$ ]] || fail 'Invalid image digest.'
[[ $size =~ ^[1-9][0-9]{0,14}$ && $raw_size =~ ^[1-9][0-9]{0,14}$ ]] || fail 'Invalid image or card size.'
test "$raw_size" -le "$size" || fail 'Image exceeds SD capacity.'
/recovery/ram-guard.sh "$nonce" "$cid" "$size" >&2
test "$(findmnt -n -o FSTYPE /run)" = tmpfs || fail '/run is not private recovery RAM.'
test -f /run/alma.raw.xz && test ! -L /run/alma.raw.xz || fail 'Missing regular compressed image.'
raw=/run/alma-inspect.raw
mountpoint=/run/alma-inspect.$nonce.$$
loop= mounted= raw_owned= directory_owned= guard_done=

cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    if test -n "$mounted"; then
        if umount "$mountpoint" >&2; then mounted=; else
            echo 'Cannot unmount inspected image; keep RAM recovery powered.' >&2
            result=1
        fi
    fi
    if test -n "$loop" && test -z "$mounted"; then
        if losetup -d "$loop" >&2; then loop=; else
            echo 'Cannot detach inspected image; keep RAM recovery powered.' >&2
            result=1
        fi
    fi
    if test -z "$loop" && test -z "$mounted"; then
        if test -n "$raw_owned"; then rm -f "$raw" || result=1; fi
        if test -n "$directory_owned"; then /bin/busybox rmdir "$mountpoint" || result=1; fi
    fi
    if test -z "$guard_done"; then
        /recovery/ram-guard.sh "$nonce" "$cid" "$size" >&2 || result=1
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

test ! -e "$raw" && test ! -L "$raw" || fail 'An earlier inspection image remains; refusing to replace it.'
test ! -e /run/alma-prepared.raw && test ! -e /run/alma-prepared.raw.partial || fail 'Prepared writer image remains; avoid double image RAM allocation.'
actual_sha=$(sha256sum /run/alma.raw.xz | cut -d ' ' -f1)
test "$actual_sha" = "$compressed_sha" || fail 'Compressed image digest differs from verified release.'
# The authenticated XZ index must agree before allocating the expanded image.
indexed_size=$(xz --robot --list /run/alma.raw.xz | awk -F '\t' '$1 == "totals" { count++; bytes=$5 } END { if (count == 1) print bytes; else exit 1 }')
test "$indexed_size" = "$raw_size" || fail 'XZ expanded size differs from verified release.'
available=$(awk '/^MemAvailable:/ {printf "%.0f\n", $2 * 1024}' /proc/meminfo)
[[ $available =~ ^[0-9]+$ ]] || fail 'Cannot determine available recovery RAM.'
test "$available" -ge "$((raw_size + 256 * 1024 * 1024))" || fail 'Insufficient RAM for read-only image inspection.'
read -r free_blocks block_size <<< "$(stat -f -c '%a %S' /run)"
test "$((free_blocks * block_size))" -ge "$((raw_size + 64 * 1024 * 1024))" || fail 'Insufficient /run tmpfs space for image inspection.'
mkdir -m 700 "$mountpoint"
directory_owned=1
# Noclobber creates a private, exclusively owned RAM file before decompression.
(set -C; : > "$raw")
raw_owned=1
# Limit both decoder memory and the output file even if the XZ index is corrupt.
# Bash uses KiB for -f; the final size check rejects its at-most-1023-byte slack.
(ulimit -f "$(((raw_size + 1023) / 1024))"; xz --memlimit-decompress=128MiB -dc /run/alma.raw.xz) > "$raw"
test "$(stat -c %s "$raw")" = "$raw_size" || fail 'Expanded image size differs from verified release.'
actual_sha=$(sha256sum "$raw" | cut -d ' ' -f1)
test "$actual_sha" = "$raw_sha" || fail 'Expanded image digest differs from verified release.'
modprobe loop >&2
loop=$(losetup --read-only --find --show --partscan "$raw")
[[ $loop =~ ^/dev/loop[0-9]+$ ]] || fail 'Unexpected loop device path.'

partitions=()
for attempt in $(seq 1 20); do
    partitions=()
    for part in "${loop}"p*; do test ! -b "$part" || partitions+=("$part"); done
    test "${#partitions[@]}" -eq 0 || break
    sleep 1
done
test "${#partitions[@]}" -gt 0 || fail 'Image has no discoverable loop partitions.'
roots=0 report=
for part in "${partitions[@]}"; do
    [[ $part =~ ^${loop}p[0-9]+$ ]] || fail 'Unexpected loop partition path.'
    filesystem=$(blkid -p -s TYPE -o value "$part" || true)
    case "$filesystem" in
        ext4) options=ro,noload,nodev,nosuid ;;
        xfs)
            if ! modprobe xfs >&2; then
                fail 'XFS candidate cannot be inspected with the recovery kernel; root uniqueness is unproved.'
            fi
            options=ro,norecovery,nodev,nosuid
            ;;
        *) continue ;;
    esac
    mount -t "$filesystem" -o "$options" "$part" "$mountpoint" >&2
    mounted=1
    if test ! -e "$mountpoint/etc/os-release" && test ! -L "$mountpoint/etc/os-release" && test ! -e "$mountpoint/usr/lib/os-release"; then
        umount "$mountpoint" >&2
        mounted=
        continue
    fi
    # Each candidate is confined by chroot. Absolute links in os-release and
    # usrmerge paths therefore resolve inside the public, read-only image.
    inspected=
    for interpreter in /usr/bin/python3 /usr/libexec/platform-python /bin/python3; do
        if candidate=$(/bin/busybox chroot "$mountpoint" "$interpreter" -I -B - "$nonce" "$cid" "$size" "$compressed_sha" "$raw_sha" "$raw_size" "$part" "$filesystem" <<'PYTHON'
import glob
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

def read_os():
    result = {}
    source = Path('/etc/os-release')
    if not source.is_file():
        source = Path('/usr/lib/os-release')
    for line in source.read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            words = shlex.split(value)
            result[key] = ' '.join(words)
    return result

try:
    release = read_os()
except (FileNotFoundError, ValueError):
    sys.exit(42)
if release.get('ID') != 'almalinux':
    sys.exit(42)

try:
    users = []
    for line in Path('/etc/passwd').read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        name, unused, uid, gid, gecos, home, shell = line.split(':')
        users.append({'name': name, 'uid': int(uid), 'gid': int(gid),
                      'gecos': gecos, 'home': home, 'shell': shell})
    groups = []
    for line in Path('/etc/group').read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        name, unused, gid, members = line.split(':')
        groups.append({'name': name, 'gid': int(gid), 'members': members.split(',') if members else []})
    tools = {name: shutil.which(name) for name in (
        'python3', 'podman', 'rpm', 'cloud-init', 'tar', 'gzip',
        'sha256sum', 'dd', 'stat', 'semanage', 'restorecon', 'systemctl', 'sshd',
        'useradd', 'usermod', 'groupadd', 'groupmod')}
    packages, rpm_error = [], None
    if tools['rpm']:
        query = subprocess.run([tools['rpm'], '--noplugins', '-qa', '--qf',
                                '%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\n'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=90)
        if query.returncode:
            raise RuntimeError('read-only RPM package query failed')
        for line in query.stdout.splitlines():
            name, version, architecture = line.split('\t')
            packages.append({'name': name, 'version': version, 'architecture': architecture})
        packages.sort(key=lambda item: (item['name'], item['architecture'], item['version']))
    else:
        rpm_error = 'rpm not installed'
    required = ('python3', 'podman', 'policycoreutils', 'coreutils', 'cloud-init',
                'tar', 'gzip', 'NetworkManager', 'openssh-server', 'shadow-utils')
    installed = {item['name'] for item in packages}
    tar_version = None
    if tools['tar']:
        query = subprocess.run([tools['tar'], '--version'], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, timeout=15)
        tar_version = query.stdout.splitlines()[0] if query.returncode == 0 and query.stdout else None
    kernels = []
    tokens = ('sch_cake', 'sdhci', 'mmc_block', 'bcmgenet', 'macb', 'gem', 'rp1',
              'pinctrl-bcm2712', 'ext4', 'vfat', 'xfs', 'overlay', 'veth', 'bridge', 'nf_tables')
    for directory in sorted(Path('/lib/modules').glob('*')):
        if not directory.is_dir():
            continue
        modules = sorted(str(path) for path in directory.rglob('*.ko*')
                         if any(token in path.name for token in tokens))
        builtin = directory / 'modules.builtin'
        builtins = sorted(line for line in builtin.read_text().splitlines()
                          if any(token in Path(line).name for token in tokens)) if builtin.is_file() else []
        kernels.append({'version': directory.name, 'module_directory': str(directory),
                        'relevant_modules': modules, 'relevant_builtins': builtins})
    configs = []
    for name in ['/etc/cloud/cloud.cfg'] + sorted(glob.glob('/etc/cloud/cloud.cfg.d/*.cfg')):
        file = Path(name)
        if not file.is_file():
            continue
        content = file.read_text()
        if len(content.encode()) > 256 * 1024:
            raise RuntimeError('cloud configuration exceeds inspection bound')
        if 'PRIVATE KEY' in content:
            configs.append({'path': name, 'content_omitted': 'private key marker'})
        else:
            configs.append({'path': name, 'text': content})
    space = os.statvfs('/')
    nonce, cid, card_size, compressed_sha, raw_sha, raw_size, partition, filesystem = sys.argv[1:]
    uid1000 = [item for item in users if item['uid'] == 1000]
    report = {
        'schemaVersion': 1, 'is_almalinux': True, 'nonce': nonce,
        'target': {'device': '/dev/mmcblk0', 'cid': cid, 'size_bytes': int(card_size)},
        'image': {'compressed_sha256': compressed_sha, 'raw_sha256': raw_sha, 'raw_bytes': int(raw_size)},
        'root_partition': partition, 'root_filesystem': filesystem, 'os_release': release,
        'users': users, 'groups': groups, 'uid1000_users': uid1000,
        'gid1000_groups': [item for item in groups if item['gid'] == 1000],
        'uid1000_almalinux_collision': any(item['name'] == 'almalinux' for item in uid1000),
        'aki_uid1000_collision': any(item['name'] != 'aki' for item in uid1000),
        'installed_packages': packages, 'required_packages': {name: name in installed for name in required},
        'rpm_error': rpm_error, 'tools': tools, 'tar_version': tar_version,
        'gnu_tar': bool(tar_version and 'GNU tar' in tar_version),
        'kernels': kernels, 'cloud_config': configs,
        'root_allocation_bytes': (space.f_blocks - space.f_bfree) * space.f_frsize,
        'root_size_bytes': space.f_blocks * space.f_frsize,
        'root_available_bytes': space.f_bavail * space.f_frsize,
    }
    print(json.dumps(report, sort_keys=True))
except Exception as error:
    print('Alma image inspection failed: ' + str(error), file=sys.stderr)
    sys.exit(1)
PYTHON
        ); then
            roots=$((roots + 1))
            test "$roots" -eq 1 || fail 'More than one AlmaLinux root partition in image.'
            report=$candidate
            inspected=1
            break
        else
            status=$?
            if test "$status" -eq 42; then inspected=1; break; fi
            if test "$status" -ne 126 && test "$status" -ne 127; then
                fail 'Alma image Python inspection failed.'
            fi
        fi
    done
    test -n "$inspected" || fail 'Linux image candidate has no usable Python interpreter; account inspection is incomplete.'
    umount "$mountpoint" >&2
    mounted=
done
test "$roots" -eq 1 || fail 'Image has no unique inspectable AlmaLinux root filesystem.'
losetup -d "$loop" >&2
loop=
rm -f "$raw"
raw_owned=
/bin/busybox rmdir "$mountpoint"
directory_owned=
/recovery/ram-guard.sh "$nonce" "$cid" "$size" >&2
guard_done=1
test -n "$report" || fail 'Image inspection produced no public report.'
printf '%s\n' "$report"
