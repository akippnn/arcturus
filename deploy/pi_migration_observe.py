#!/usr/bin/env python3
"""Sanitized read-only observation for an existing Pi migration session.

This standalone observer reuses the coordinator-side read-only observation
foundation from the Antigravity workflow. That earlier work produced an
adapter and coordinator observations, not an Antigravity boot diagnosis or a
Pi boot-selection patch. This module deliberately has no migration, discovery,
or state-persistence operation.
"""
from __future__ import annotations

import argparse
import copy
import ipaddress
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Callable

import pi_migrate


HERE = Path(__file__).resolve().parent
PHASES = frozenset({
    "inventory", "workloads-stopping", "workloads-stopped", "images-exported",
    "ram-prepared", "ram-boot-requested", "ram-running", "backup-verified",
    "flash-started", "flash-verified", "fallback-boot-requested",
    "fallback-verified", "alma-boot-requested", "alma-promoted", "alma-running",
    "environment-restoring", "environment-restored", "final-boot-requested",
    "rollback-verified", "source-resumed",
})

# These commands are intentionally fixed. They inspect the pinned host through
# its saved SSH alias and never invoke migration helpers or search for peers.
RECOVERY_READ = """printf 'nonce\\n'; cat /run/arcturus-ram-ready
printf 'cid\\n'; cat /sys/block/mmcblk0/device/cid
printf 'size\\n'; blockdev --getsize64 /dev/mmcblk0
printf 'root\\n'; findmnt -n -o FSTYPE,SOURCE /
printf 'os_begin\\n'; if test -r /etc/os-release; then cat /etc/os-release; fi; printf '\\nos_end\\n'
printf 'kernel\\n'; uname -r
printf 'mounts_begin\\n'; findmnt -rn -o SOURCE,TARGET,FSTYPE; printf 'mounts_end\\n'
printf 'mac\\n'; cat /sys/class/net/{interface}/address"""

ALMA_READ = r'''import json, pathlib, subprocess
def run(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()
addresses = json.loads(run(["ip", "-j", "address"]))
macs = {item.get("ifname", ""): item.get("address", "").lower()
        for item in addresses if item.get("ifname") and item.get("address")}
print(json.dumps({
    "os": pathlib.Path("/etc/os-release").read_text(),
    "cid": pathlib.Path("/sys/block/mmcblk0/device/cid").read_text().strip(),
    "size_bytes": int(run(["blockdev", "--getsize64", "/dev/mmcblk0"])),
    "instance_id": pathlib.Path("/var/lib/cloud/data/instance-id").read_text().strip(),
    "root": run(["findmnt", "-n", "-o", "FSTYPE,SOURCE", "/"]),
    "mounts": run(["findmnt", "-rn", "-o", "SOURCE,TARGET,FSTYPE"]),
    "macs": macs,
    "kernel": run(["uname", "-r"]),
}, sort_keys=True))
'''


class ObservationError(RuntimeError):
    """A local validation error whose message must not enter public output."""


def _regular_private_file(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ObservationError(f"private {label} is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size == 0 or metadata.st_mode & 0o077:
        raise ObservationError(f"private {label} must be a regular mode-0600 file")


def _validate_session_file(session: Path) -> Path:
    if not session.is_absolute():
        raise ObservationError("session path must be absolute")
    try:
        resolved = session.resolve(strict=True)
        if not resolved.is_dir():
            raise ObservationError("session path is not a directory")
        if resolved.stat().st_mode & 0o077:
            raise ObservationError("session directory permissions are too broad")
        session_file = resolved / "session.json"
        _regular_private_file(session_file, "session state")
    except OSError as exc:
        raise ObservationError("session path is unavailable") from exc
    return resolved


def _validate_state(state: Any, session: Path) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ObservationError("session state is not an object")
    required = ("directory", "key", "host_key_alias", "known_hosts", "host", "nonce", "user", "target", "network", "phase")
    if any(key not in state for key in required):
        raise ObservationError("session state is incomplete")
    if state["directory"] != str(session):
        raise ObservationError("session path differs from recorded state")
    if state["phase"] not in PHASES:
        raise ObservationError("session phase is unsupported")
    if not re.fullmatch(r"[0-9a-f]{32}", str(state["nonce"])):
        raise ObservationError("session nonce is malformed")

    target, network = state["target"], state["network"]
    if not isinstance(target, dict) or not isinstance(network, dict):
        raise ObservationError("pinned target or network is malformed")
    if target.get("device") != "/dev/mmcblk0":
        raise ObservationError("pinned target device is unsupported")
    if not re.fullmatch(r"[0-9a-f]{32}", str(target.get("cid", ""))):
        raise ObservationError("pinned card identity is malformed")
    if type(target.get("size_bytes")) is not int or target["size_bytes"] <= 0:
        raise ObservationError("pinned card size is malformed")
    try:
        ipaddress.ip_address(state["host"])
        ipaddress.ip_address(network["address"])
    except (TypeError, ValueError) as exc:
        raise ObservationError("pinned host or network address is malformed") from exc
    if type(network.get("prefix")) is not int or network["prefix"] not in range(1, 33):
        raise ObservationError("pinned network prefix is malformed")
    if not isinstance(network.get("interface"), str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", network["interface"]):
        raise ObservationError("pinned interface is malformed")
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", str(network.get("mac", "")).lower()):
        raise ObservationError("pinned wired MAC is malformed")
    if not isinstance(state["user"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,31}", state["user"]):
        raise ObservationError("pinned SSH user is malformed")
    alias = state["host_key_alias"]
    if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,255}", alias):
        raise ObservationError("pinned SSH alias is malformed")

    key_path = Path(state["key"]).expanduser()
    known_hosts = Path(state["known_hosts"]).expanduser()
    if key_path.is_symlink() or known_hosts.is_symlink():
        raise ObservationError("SSH key and known-hosts must not be symlinks")
    _regular_private_file(key_path, "SSH key")
    _regular_private_file(known_hosts, "known-hosts file")
    if known_hosts.resolve().parent != session:
        raise ObservationError("known-hosts file is outside the private session")
    try:
        pinned = subprocess.run(
            ["ssh-keygen", "-F", alias, "-f", str(known_hosts)],
            capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ObservationError("pinned SSH alias could not be checked") from exc
    if pinned.returncode != 0 or not pinned.stdout.strip():
        raise ObservationError("pinned SSH alias is absent")
    return state


def load_session(session: Path) -> dict[str, Any]:
    """Load validated canonical state, retaining the migration mount guard."""
    session = _validate_session_file(Path(session).expanduser())
    # state_load checks that the private session remains on its recorded mount.
    # Do not call the migration module's discovery, peer-selection, or save APIs.
    state = pi_migrate.state_load(session)
    return _validate_state(state, session)


def _os_id(os_release: str) -> str:
    match = re.search(r"^ID=(?:\"([^\"]*)\"|'([^']*)'|([^\s]+))$", os_release, re.M)
    return ((match.group(1) or match.group(2) or match.group(3)).lower() if match else "")


def _parse_recovery(output: bytes) -> dict[str, Any]:
    try:
        text = output.decode("utf-8", "strict")
        lines = text.splitlines()
        scalars: dict[str, str] = {}
        for name in ("nonce", "cid", "size", "root", "kernel"):
            marker = name + "\n"
            if marker not in text:
                raise ValueError("missing marker")
            scalars[name] = text.split(marker, 1)[1].splitlines()[0].strip()
        os_release = text.split("os_begin\n", 1)[1].split("\nos_end\n", 1)[0]
        mounts_text = text.split("mounts_begin\n", 1)[1].split("mounts_end\n", 1)[0]
        root_fields = scalars["root"].split()
        if len(root_fields) != 2:
            raise ValueError("invalid root")
        mac = text.split("mac\n", 1)[1].splitlines()[0].strip().lower()
        return {
            "nonce": scalars["nonce"], "cid": scalars["cid"].lower(),
            "size_bytes": int(scalars["size"]), "root_fstype": root_fields[0],
            "root_source": root_fields[1], "os": os_release,
            "mounts": [line.split() for line in mounts_text.splitlines() if line.split()],
            "mac": mac, "kernel": scalars["kernel"],
        }
    except (UnicodeDecodeError, IndexError, ValueError) as exc:
        raise ObservationError("recovery observation was malformed") from exc


def _normalize_alma(report: dict[str, Any]) -> dict[str, Any]:
    mounts_value = report.get("mounts", "")
    mounts = mounts_value if isinstance(mounts_value, list) else [line.split() for line in str(mounts_value).splitlines() if line.split()]
    root_fields = str(report.get("root", "")).split()
    root_fstype, root_source = (root_fields + ["", ""])[:2]
    return {**report, "mounts": mounts, "root_fstype": root_fstype, "root_source": root_source,
            "cid": str(report.get("cid", "")).lower(), "nonce": report.get("instance_id")}


def _assess(report: dict[str, Any], state: dict[str, Any], kind: str) -> dict[str, Any]:
    target, network = state["target"], state["network"]
    root_fstype = report.get("root_fstype", "")
    root_source = report.get("root_source", "")
    os_id = _os_id(str(report.get("os", "")))
    mounts = report.get("mounts", [])
    macs = report.get("macs", {})
    sd_source = re.compile(r"^/dev/mmcblk0(?:p\d+)?(?:$|\[)")
    device_mounted = bool(sd_source.match(str(root_source))) or any(
        row and sd_source.match(str(row[0])) for row in mounts
    )
    expected_alma_root = root_source == "/dev/mmcblk0p2"
    recovery_os_ok = os_id in ("", "busybox")
    checks = {
        "ssh_pin": True,  # SSH completed through the validated saved alias.
        "nonce": report.get("nonce") == state["nonce"],
        "mac": (str(report.get("mac", "")).lower() == network["mac"].lower()) if kind == "recovery"
               else isinstance(macs, dict) and str(macs.get(network["interface"], "")).lower() == network["mac"].lower(),
        "cid": str(report.get("cid", "")).lower() == target["cid"].lower(),
        "size": report.get("size_bytes") == target["size_bytes"],
        "root": (root_fstype in {"rootfs", "tmpfs", "ramfs"} and not device_mounted) if kind == "recovery" else expected_alma_root,
        "sd_unmounted": not device_mounted,
        "os": recovery_os_ok if kind == "recovery" else os_id == "almalinux",
    }
    identity_checks = checks.values() if kind == "recovery" else (
        value for key, value in checks.items() if key != "sd_unmounted"
    )
    return {"verified_identity": all(identity_checks), "checks": checks,
            "root_type": root_fstype, "kind": kind}


def _observe_state(
    state: dict[str, Any], peer_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    if peer_factory is None:
        peer_factory = pi_migrate.Peer
    results: list[tuple[str, dict[str, Any]]] = []
    errors: dict[str, str | None] = {"recovery": None, "alma": None}
    for kind, recovery, port in (("recovery", True, 2222), ("alma", False, 22)):
        try:
            peer_state = copy.deepcopy(state)
            peer = peer_factory(peer_state, recovery=recovery, port=port)
            if recovery:
                command = RECOVERY_READ.format(interface=state["network"]["interface"])
                report = _parse_recovery(peer.run(command, timeout=20))
            else:
                raw = peer.run("sudo -n /usr/bin/python3 -", ALMA_READ.encode(), timeout=20)
                report = _normalize_alma(json.loads(raw))
            results.append((kind, _assess(report, state, kind)))
        except (OSError, ValueError, subprocess.SubprocessError, TimeoutError, RuntimeError) as exc:
            # Peer errors can include addresses, paths, or remote stderr. Publish
            # only the exception class and never its text.
            errors[kind] = type(exc).__name__

    selected = next(((kind, result) for kind, result in results if result["verified_identity"]), None)
    if selected is None:
        selected = next(((kind, result) for kind, result in results if result["kind"] == "recovery"), None)
    if selected is None:
        selected = next(((kind, result) for kind, result in results if result["kind"] == "alma"), None)
    phase = state.get("phase") if state.get("phase") in PHASES else "unknown"
    if selected:
        kind, result = selected
        public_state = kind if result["verified_identity"] else "identity_mismatch"
        return {"state": public_state, "verified_identity": result["verified_identity"],
                "phase": phase, "checks": result["checks"], "errors": errors}
    return {"state": "unreachable", "verified_identity": False, "phase": phase,
            "checks": {}, "errors": errors}


def observe(session: Path) -> dict[str, Any]:
    try:
        state = load_session(Path(session))
    except Exception as exc:
        return {"state": "invalid_session", "verified_identity": False,
                "phase": "unknown", "error_class": type(exc).__name__}
    return _observe_state(state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sanitized read-only Pi migration observation")
    parser.add_argument("--session", type=Path, required=True, help="absolute path to an existing migration session")
    args = parser.parse_args(argv)
    result = observe(args.session)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["state"] in {"recovery", "alma"} and result["verified_identity"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
