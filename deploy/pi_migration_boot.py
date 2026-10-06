"""Pinned, on-card boot acceptance for the Pi migration control plane.

Peers and reboot/discovery callbacks are supplied by the caller. These functions
never save state or discover hosts themselves, and never return seed contents.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex


class BootAcceptanceError(RuntimeError):
    pass


RESCUE_CONFIG = (
    "[all]\narm_64bit=1\nauto_initramfs=0\n"
    "kernel=arcturus-recovery-kernel.img\n"
    "device_tree=arcturus-recovery.dtb\n"
    "cmdline=arcturus-recovery-cmdline.txt\n"
    "initramfs arcturus-recovery.img followkernel\n"
).encode()
PAYLOAD_FILES = (
    "arcturus-recovery.img", "arcturus-recovery-kernel.img",
    "arcturus-recovery.dtb", "arcturus-recovery-cmdline.txt", "config.txt",
    "alma-config.txt", "tryboot.txt", "user-data", "meta-data", "network-config",
)
CHECKSUM_FILE = "arcturus-recovery.checksums"
_RETRY_REQUIRED_TOOLS = ("blkid", "cat", "date", "findmnt", "mkdir", "mount", "od",
                         "rmdir", "sha256sum", "tr", "umount", "uname")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _target(state):
    target = state.get("target", {})
    if (target.get("device") != "/dev/mmcblk0"
            or not re.fullmatch(r"[0-9a-f]{32}", str(target.get("cid", "")))
            or type(target.get("size_bytes")) is not int or target["size_bytes"] <= 0):
        raise BootAcceptanceError("invalid pinned SD card identity")
    return {key: target[key] for key in ("device", "cid", "size_bytes")}


def record_boot_payload(state, checksum_text, recovery_kernel):
    """Freeze writer readback evidence before leaving RAM after a successful flash.

    checksum_text is the exact /run/alma-prepared-boot.checksums byte stream.
    recovery_kernel is /recovery/kernel-version from the same guarded RAM peer.
    The caller must persist the resulting state before requesting Alma boot.
    """
    data = checksum_text.encode() if isinstance(checksum_text, str) else checksum_text
    hashes = {}
    try:
        for line in data.decode("ascii").splitlines():
            match = re.fullmatch(r"([0-9a-f]{64})  ([a-zA-Z0-9_.-]+)", line)
            if not match or match[2] not in PAYLOAD_FILES or match[2] in hashes:
                raise ValueError
            hashes[match[2]] = match[1]
    except (AttributeError, UnicodeError, ValueError):
        raise BootAcceptanceError("invalid flash boot checksum manifest") from None
    if (set(hashes) != set(PAYLOAD_FILES)
            or hashes["config.txt"] != _sha(RESCUE_CONFIG)
            or hashes["alma-config.txt"] != hashes["tryboot.txt"]):
        raise BootAcceptanceError("flash boot manifest does not describe the approved fallback")
    kernel = recovery_kernel.decode("ascii").strip() if isinstance(recovery_kernel, bytes) else recovery_kernel.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", kernel):
        raise BootAcceptanceError("invalid preserved recovery kernel version")
    if not re.fullmatch(r"[0-9a-f]{32}", str(state.get("nonce", ""))):
        raise BootAcceptanceError("invalid migration session nonce")
    hashes[CHECKSUM_FILE] = _sha(data)
    payload = {"schemaVersion": 1, "target": _target(state), "nonce": state["nonce"],
               "recovery_kernel": kernel, "sha256": hashes}
    state["boot_payload"] = payload
    # Evidence from a different flash must never authorize promotion.
    state.pop("fallback_verified", None)
    state.pop("environment_verified", None)
    state.pop("boot_promoted", None)
    return payload


def _payload(state):
    payload = state.get("boot_payload", {})
    hashes = payload.get("sha256", {})
    if (payload.get("schemaVersion") != 1 or payload.get("target") != _target(state)
            or payload.get("nonce") != state.get("nonce")
            or not re.fullmatch(r"[0-9a-f]{32}", str(payload.get("nonce", "")))
            or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", str(payload.get("recovery_kernel", "")))
            or set(hashes) != set(PAYLOAD_FILES) | {CHECKSUM_FILE}
            or any(not re.fullmatch(r"[0-9a-f]{64}", str(value)) for value in hashes.values())
            or hashes.get("config.txt") != _sha(RESCUE_CONFIG)
            or hashes.get("alma-config.txt") != hashes.get("tryboot.txt")):
        raise BootAcceptanceError("missing or inconsistent frozen flash boot evidence")
    return payload


# This program executes only on the normal Alma peer, where Python/cloud-init is
# available. RAM recovery uses a separate, small shell command below.
_REMOTE_COMMON = r'''
import hashlib, json, os, pathlib, stat, subprocess, sys

def fail(message):
    raise RuntimeError(message)

def flatten(nodes):
    for item in nodes:
        yield item
        yield from flatten(item.get("children", []))

def digest(path):
    if path.is_symlink() or not path.is_file():
        fail("boot payload is not a regular file")
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()

def inspect(allowed_config):
    target = settings["target"]
    cid = pathlib.Path("/sys/block/mmcblk0/device/cid").read_text().strip()
    size = int(pathlib.Path("/sys/block/mmcblk0/size").read_text()) * 512
    if cid != target["cid"] or size != target["size_bytes"]:
        fail("SD card identity differs from frozen flash evidence")
    listing = json.loads(subprocess.check_output([
        "lsblk", "--json", "--paths", "--output", "PATH,TYPE,PKNAME,LABEL,FSTYPE"]))
    candidates = [node for node in flatten(listing["blockdevices"]) if node.get("label") == "CIDATA"]
    if len(candidates) != 1:
        fail("CIDATA partition is not unique")
    part = candidates[0]
    if (part.get("path") != "/dev/mmcblk0p1" or part.get("type") != "part"
            or part.get("pkname") not in ("mmcblk0", "/dev/mmcblk0") or part.get("fstype") != "vfat"):
        fail("CIDATA is not the expected SD boot partition")
    device = os.stat("/dev/mmcblk0p1")
    if not stat.S_ISBLK(device.st_mode):
        fail("SD boot path is not a block device")
    mounts = json.loads(subprocess.check_output([
        "findmnt", "--json", "--output", "SOURCE,TARGET,FSTYPE,OPTIONS,MAJ:MIN,FSROOT"]))
    major_minor = str(os.major(device.st_rdev)) + ":" + str(os.minor(device.st_rdev))
    candidates = [node for node in flatten(mounts["filesystems"]) if node.get("maj:min") == major_minor]
    if len(candidates) != 1:
        fail("SD boot partition must have exactly one mount")
    mount = candidates[0]
    source = mount.get("source", "")
    if ("[" in source or os.path.realpath(source) != "/dev/mmcblk0p1"
            or mount.get("fstype") != "vfat" or mount.get("fsroot") != "/"):
        fail("SD boot mount source is unexpected")
    boot = pathlib.Path(mount["target"])
    if not boot.is_absolute() or boot.is_symlink() or not boot.is_dir() or boot.stat().st_dev != device.st_rdev:
        fail("SD boot mount does not match the block device")
    hashes = {name: digest(boot / name) for name in settings["sha256"]}
    for name, expected in settings["sha256"].items():
        if hashes[name] not in (allowed_config if name == "config.txt" else [expected]):
            fail("boot payload checksum differs from frozen flash evidence: " + name)
    return boot, {"target": {"device": "/dev/mmcblk0", "cid": cid, "size_bytes": size},
                  "partition": "/dev/mmcblk0p1", "parent": "/dev/mmcblk0", "label": "CIDATA",
                  "fstype": "vfat", "mount_source": "/dev/mmcblk0p1", "mountpoint": str(boot),
                  "sha256": hashes}

def promote(boot):
    content = (boot / "alma-config.txt").read_bytes()
    if hashlib.sha256(content).hexdigest() != settings["sha256"]["alma-config.txt"]:
        fail("Alma configuration changed before promotion")
    if (boot / "config.txt").read_bytes() == content:
        return
    temporary = boot / (".arcturus-config-" + settings["nonce"] + ".new")
    # A stale file from an interrupted attempt is never silently trusted.
    created = False
    try:
        with temporary.open("xb") as stream:
            created = True
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.read_bytes() != content:
            fail("staged Alma configuration readback failed")
        os.replace(temporary, boot / "config.txt")
        directory = os.open(boot, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        os.sync()
        if (boot / "config.txt").read_bytes() != content:
            fail("promoted Alma configuration readback failed")
    finally:
        if created and temporary.exists():
            temporary.unlink()
'''


def _script(payload, promote=False, require_rescue_default=True):
    allowed = [payload["sha256"]["config.txt"]]
    if require_rescue_default is False:
        allowed = [payload["sha256"]["alma-config.txt"]]
    elif require_rescue_default is None:
        allowed.append(payload["sha256"]["alma-config.txt"])
    source = "import json\nsettings = json.loads(" + repr(json.dumps(payload, sort_keys=True)) + ")\n"
    source += _REMOTE_COMMON
    source += "\ntry:\n    boot, report = inspect(" + repr(allowed) + ")\n"
    if promote:
        source += "    promote(boot)\n    boot, report = inspect([settings['sha256']['alma-config.txt']])\n"
    source += "    print(json.dumps(report, sort_keys=True))\nexcept Exception as error:\n    print(str(error), file=sys.stderr)\n    sys.exit(1)\n"
    return source.encode()


def _validate_report(report, payload, require_rescue_default=True):
    if (report.get("target") != payload["target"] or report.get("partition") != "/dev/mmcblk0p1"
            or report.get("parent") != "/dev/mmcblk0" or report.get("label") != "CIDATA"
            or report.get("fstype") != "vfat" or report.get("mount_source") != "/dev/mmcblk0p1"
            or not isinstance(report.get("mountpoint"), str) or not report["mountpoint"].startswith("/")):
        raise BootAcceptanceError("boot mount or SD card identity differs from frozen flash evidence")
    allowed = {payload["sha256"]["config.txt"]}
    if require_rescue_default is False:
        allowed = {payload["sha256"]["alma-config.txt"]}
    elif require_rescue_default is None:
        allowed.add(payload["sha256"]["alma-config.txt"])
    hashes = report.get("sha256", {})
    if set(hashes) != set(payload["sha256"]) or any(
            value not in (allowed if name == "config.txt" else {payload["sha256"][name]})
            for name, value in hashes.items()):
        raise BootAcceptanceError("boot payload checksum differs from frozen flash evidence")
    return report


def _remote_report(peer, payload, *, promote=False, require_rescue_default=True):
    result = peer.run("sudo -n /usr/bin/python3 -", _script(payload, promote, require_rescue_default), timeout=300)
    try:
        report = json.loads(result)
        if not isinstance(report, dict):
            raise ValueError
    except (ValueError, TypeError):
        raise BootAcceptanceError("invalid boot acceptance response") from None
    return _validate_report(report, payload, False if promote else require_rescue_default)


def verify_boot_payload(peer, state, *, require_rescue_default=True):
    """Check the unique mounted CIDATA on the pinned SD; return metadata/hashes."""
    return _remote_report(peer, _payload(state), require_rescue_default=require_rescue_default)


def _verify_recovery_payload(peer, state):
    payload = _payload(state)
    result = peer.run("uname -r; sha256sum /recovery/boot/kernel.img /recovery/boot/recovery.dtb", timeout=60)
    try:
        lines = result.decode("ascii").splitlines()
        if len(lines) != 3 or lines[0] != payload["recovery_kernel"]:
            raise ValueError
        for line, path, name in zip(lines[1:], ("/recovery/boot/kernel.img", "/recovery/boot/recovery.dtb"),
                                    ("arcturus-recovery-kernel.img", "arcturus-recovery.dtb")):
            if line != payload["sha256"][name] + "  " + path:
                raise ValueError
    except (UnicodeError, ValueError):
        raise BootAcceptanceError("recovery did not preserve the approved kernel and device tree") from None


def test_fallback(peer, state, *, normal_reboot, wait_recovery, guard):
    """Normal-reset Alma into rescue and prove pinned RAM recovery returned.

    normal_reboot(peer) handles the expected SSH disconnect. wait_recovery(state)
    returns the pinned recovery peer. guard(peer) must assert nonce, card identity,
    unmounted SD, and process namespace safety. The caller persists state.
    """
    state["fallback_verified"] = False
    verify_boot_payload(peer, state)
    state["phase"] = "fallback-boot-requested"
    normal_reboot(peer)
    recovery = wait_recovery(state)
    guard(recovery)
    _verify_recovery_payload(recovery, state)
    state["fallback_verified"] = True
    state["phase"] = "fallback-verified"
    return recovery


def reboot_alma(peer, state, *, guard, request_reboot=None):
    """Request the one-shot Alma tryboot from guarded RAM; caller saves state."""
    if state.get("phase") not in ("flash-verified", "fallback-verified"):
        raise BootAcceptanceError("Alma boot requires verified flash or fallback")
    guard(peer)
    _verify_recovery_payload(peer, state)
    if state["phase"] == "flash-verified":
        peer.run("test -f /run/alma-flash-verified && sync")
    else:
        peer.run("sync")
    state["phase"] = "alma-boot-requested"
    if request_reboot is None:
        return peer.run("/bin/reboot-tryboot", timeout=20)
    return request_reboot(peer)


def _retry_boot_inspection_command(state):
    target = _target(state)
    mountpoint = "/run/backup-boot"
    names = PAYLOAD_FILES + (CHECKSUM_FILE,)
    checks = "\n".join(
        f"test -f {shlex.quote(mountpoint + '/' + name)} && test ! -L {shlex.quote(mountpoint + '/' + name)}"
        for name in names
    )
    required_tools = " ".join(_RETRY_REQUIRED_TOOLS)
    return f'''set -eu
cid={shlex.quote(target["cid"])}
size={target["size_bytes"]}
mountpoint={shlex.quote(mountpoint)}
for utility in {required_tools}; do
    if ! command -v "$utility" >/dev/null 2>&1; then
        echo "required recovery utility is unavailable: $utility" >&2
        exit 1
    fi
done
test "$(cat /sys/block/mmcblk0/device/cid)" = "$cid"
test "$(($(cat /sys/block/mmcblk0/size) * 512))" -eq "$size"
test -b /dev/mmcblk0p1
test "$(blkid -s LABEL -o value /dev/mmcblk0p1)" = CIDATA
test "$(blkid -s TYPE -o value /dev/mmcblk0p1)" = vfat
mount_state() {{
    if findmnt -rn --mountpoint "$mountpoint" >/dev/null 2>&1; then
        return 0
    else
        mount_status=$?
        if [ "$mount_status" -eq 1 ]; then
            return 1
        fi
        echo "could not determine whether backup boot mountpoint is mounted" >&2
        return 2
    fi
}}
if mount_state; then
    echo "backup boot mountpoint is already mounted" >&2
    exit 1
else
    mount_status=$?
    if [ "$mount_status" -ne 1 ]; then exit 1; fi
fi
if test -e "$mountpoint" || test -L "$mountpoint"; then
    echo "backup boot mountpoint already exists" >&2
    exit 1
fi
mkdir -m 700 "$mountpoint"
cleanup() {{
    cleanup_result=$?
    trap - EXIT HUP INT TERM
    if ! cd /; then
        echo "could not leave temporary boot mountpoint" >&2
        exit 1
    fi
    if mount_state; then
        mount_info=$(findmnt -rn -o SOURCE,FSTYPE,OPTIONS --mountpoint "$mountpoint") || {{
            echo "could not inspect mounted SD boot partition" >&2
            exit 1
        }}
        set -- $mount_info
        if [ "$#" -ne 3 ] || [ "$1" != /dev/mmcblk0p1 ] || [ "$2" != vfat ]; then
            echo "mounted SD boot partition identity is unexpected" >&2
            exit 1
        fi
        case ",$3," in
            *,ro,*) ;;
            *) echo "mounted SD boot partition is not read-only" >&2; exit 1 ;;
        esac
        if ! umount "$mountpoint"; then
            echo "could not unmount SD boot partition" >&2
            exit 1
        fi
    else
        mount_status=$?
        if [ "$mount_status" -ne 1 ]; then exit 1; fi
    fi
    if mount_state; then
        echo "SD boot partition remains mounted" >&2
        exit 1
    else
        mount_status=$?
        if [ "$mount_status" -ne 1 ]; then exit 1; fi
    fi
    if ! rmdir "$mountpoint"; then
        echo "could not remove temporary boot mountpoint" >&2
        exit 1
    fi
    exit "$cleanup_result"
}}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
mount -t vfat -o ro,nosuid,nodev,noexec /dev/mmcblk0p1 "$mountpoint"
mount_info=$(findmnt -rn -o SOURCE,FSTYPE,OPTIONS --mountpoint "$mountpoint")
set -- $mount_info
if [ "$#" -ne 3 ] || [ "$1" != /dev/mmcblk0p1 ] || [ "$2" != vfat ]; then
    echo "mounted SD boot partition identity is unexpected" >&2
    exit 1
fi
case ",$3," in
    *,ro,*) ;;
    *) echo "SD boot partition is not mounted read-only" >&2; exit 1 ;;
esac
{checks}
cd "$mountpoint"
sha256sum -c -
'''


def _capture_retry_boot_event(peer):
    """Capture only public boot-selection facts, preserving chosen bytes as hex."""
    command = r'''set -eu
printf 'kernel=%s\n' "$(uname -r)"
printf 'boot_id=%s\n' "$(cat /proc/sys/kernel/random/boot_id)"
printf 'uptime=%s\n' "$(cat /proc/uptime)"
printf 'observed_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
base=/proc/device-tree/chosen/bootloader
for name in boot-count tryboot arg1 rsts partition; do
    value=
    if test -f "$base/$name"; then
        value=$(od -An -tx1 "$base/$name" | tr -d ' \n')
    fi
    printf 'chosen_%s_hex=%s\n' "$name" "$value"
done
'''
    try:
        output = peer.run(command, timeout=30).decode("ascii")
        fields = {}
        for line in output.splitlines():
            key, value = line.split("=", 1)
            if key in fields:
                raise ValueError
            fields[key] = value
        expected = {"kernel", "boot_id", "uptime", "observed_at_utc"} | {
            "chosen_" + name + "_hex" for name in ("boot-count", "tryboot", "arg1", "rsts", "partition")
        }
        if set(fields) != expected or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", fields["kernel"]):
            raise ValueError
        if not re.fullmatch(r"[0-9a-f-]{36}", fields["boot_id"]):
            raise ValueError
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)? [0-9]+(?:\.[0-9]+)?", fields["uptime"]):
            raise ValueError
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", fields["observed_at_utc"]):
            raise ValueError
        if any(not re.fullmatch(r"(?:[0-9a-f]{2})*", fields[key])
               for key in expected - {"kernel", "boot_id", "uptime", "observed_at_utc"}):
            raise ValueError
    except (UnicodeError, ValueError):
        raise BootAcceptanceError("invalid public boot-selection observation") from None
    return fields


def retry_alma(peer, state, *, guard, request_reboot):
    """Retry the requested one-shot boot after read-only frozen-payload proof.

    The caller persists boot_retry_events from request_reboot before it issues
    the reboot. This deliberately keeps the session in alma-boot-requested.
    """
    if state.get("phase") != "alma-boot-requested" or state.get("boot_promoted") is True:
        raise BootAcceptanceError("Alma boot retry requires an unpromoted alma-boot-requested session")
    payload = _payload(state)
    if request_reboot is None:
        raise BootAcceptanceError("Alma boot retry requires a persist-before-reboot callback")
    guard(peer)
    _verify_recovery_payload(peer, state)
    command = _retry_boot_inspection_command(state)
    manifest = "".join(payload["sha256"][name] + "  " + name + "\n"
                       for name in PAYLOAD_FILES + (CHECKSUM_FILE,)).encode("ascii")
    peer.run(command, manifest, timeout=120)
    # Successful inspection means its EXIT trap unmounted the partition and
    # removed the RAM mountpoint. The guard proves that state before any reboot.
    guard(peer)
    event = _capture_retry_boot_event(peer)
    events = state.setdefault("boot_retry_events", [])
    if not isinstance(events, list):
        raise BootAcceptanceError("invalid persisted boot retry event history")
    events.append(event)
    return request_reboot(peer)


def promote_boot(peer, state):
    """Atomically select Alma only after fallback and environment verification.

    An interrupted reply can be retried: the final config is accepted only when
    it equals the frozen Alma config. No reboot is performed by promotion.
    """
    if state.get("fallback_verified") is not True or state.get("environment_verified") is not True:
        raise BootAcceptanceError("boot promotion requires verified fallback and restored environment")
    payload = _payload(state)
    # Repeat the complete inspection in the modifying process before replacement.
    verify_boot_payload(peer, state, require_rescue_default=None)
    report = _remote_report(peer, payload, promote=True, require_rescue_default=None)
    state["boot_promoted"] = True
    state["phase"] = "alma-promoted"
    return report
