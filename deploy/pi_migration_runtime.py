#!/usr/bin/env python3
"""Portable plan and rootful Podman restore for a Docker Pi migration.

Planning is side-effect free.  The apply command is deliberately narrow: it
accepts a reviewed JSON plan, validates every required input and collision
before writing, then creates only the resources described by that plan.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import ipaddress
import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import zlib
from pathlib import Path
from typing import Any


class RuntimeMigrationError(RuntimeError):
    """An unsupported source setting or unsafe restore condition."""


_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALLOWED_BIND_ROOT = Path("/home/aki")
_PODMAN_VOLUME_ROOT = Path("/var/lib/containers/storage/volumes")
_UNIT_DIR = Path("/etc/systemd/system")
_ENV_DIR = Path("/etc/arcturus-migrated")


def _safe_name(value: str, what: str) -> str:
    if not isinstance(value, str) or not _SAFE.fullmatch(value) or value in (".", ".."):
        raise RuntimeMigrationError(f"unsafe {what} name")
    return value


def volume_archive_name(name: str) -> str:
    """Return a stable, filesystem-safe archive basename for a Docker volume."""
    if not isinstance(name, str) or not name or "\x00" in name:
        raise RuntimeMigrationError("invalid volume name")
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return f"volume-{digest}.tar.gz"


def _unsupported(container: dict[str, Any], network_records: dict[str, dict[str, Any]]) -> list[str]:
    config = container.get("Config") or {}
    host = container.get("HostConfig") or {}
    state = container.get("State") or {}
    blockers: list[str] = []
    if host.get("Privileged"):
        blockers.append("privileged container")
    for field in ("Devices", "DeviceRequests", "CapAdd", "CapDrop", "SecurityOpt", "Sysctls", "Tmpfs"):
        if host.get(field):
            blockers.append("custom " + field)
    if host.get("CgroupnsMode") not in (None, "", "private"):
        blockers.append("custom CgroupnsMode")
    if host.get("Runtime") not in (None, "", "runc"):
        blockers.append("unsupported Docker runtime")
    for field in ("PidMode", "IpcMode", "UTSMode", "UsernsMode"):
        value = host.get(field)
        if value and value not in ("", "private", "default"):
            blockers.append("custom " + field)
    if host.get("NetworkMode") == "host":
        blockers.append("host networking")
    if host.get("NetworkMode") == "container" or str(host.get("NetworkMode", "")).startswith("container:"):
        blockers.append("container network namespace sharing")
    networks = (container.get("NetworkSettings") or {}).get("Networks") or {}
    if len(networks) > 1:
        blockers.append("multiple networks on one container")
    for net_name, endpoint in networks.items():
        network = network_records.get(net_name)
        if network is None:
            blockers.append("missing network inspect data for " + str(net_name))
            continue
        if (network.get("Driver") or "bridge") != "bridge":
            blockers.append("non-bridge network " + str(net_name))
        if network.get("Internal"):
            blockers.append("internal network " + str(net_name))
        if network.get("EnableIPv6"):
            blockers.append("IPv6 network " + str(net_name))
        docker_default_bridge_options = {
            "com.docker.network.bridge.default_bridge": "true",
            "com.docker.network.bridge.enable_icc": "true",
            "com.docker.network.bridge.enable_ip_masquerade": "true",
            "com.docker.network.bridge.host_binding_ipv4": "0.0.0.0",
            "com.docker.network.bridge.name": "docker0",
            "com.docker.network.driver.mtu": "1500",
        }
        options = network.get("Options") or {}
        if options and not (net_name == "bridge" and all(docker_default_bridge_options.get(k) == v for k, v in options.items())):
            blockers.append("custom network options " + str(net_name))
        if endpoint.get("Links"):
            blockers.append("network links on " + str(net_name))
        if endpoint.get("DriverOpts"):
            blockers.append("custom endpoint options on " + str(net_name))
    if host.get("VolumesFrom"):
        blockers.append("VolumesFrom")
    if host.get("AutoRemove"):
        blockers.append("automatic removal")
    if host.get("ReadonlyRootfs"):
        blockers.append("read-only root filesystem")
    if host.get("Init"):
        blockers.append("Docker init process")
    for field in ("ExtraHosts", "Dns", "DnsSearch", "DnsOptions", "Links", "GroupAdd", "Ulimits",
                  "CpusetCpus", "CpusetMems", "CpuShares", "NanoCpus", "Memory", "MemorySwap",
                  "MemoryReservation", "MemorySwappiness", "OomKillDisable", "OomScoreAdj", "PidsLimit"):
        value = host.get(field)
        if value not in (None, False, 0, "", [], {}):
            blockers.append("custom " + field)
    if config.get("Healthcheck"):
        blockers.append("custom container healthcheck")
    if host.get("Binds"):
        for bind in host["Binds"]:
            if not isinstance(bind, str):
                blockers.append("invalid bind mount")
                continue
            source = bind.split(":", 1)[0]
            if not _under_home(source):
                blockers.append("bind path outside /home/aki")
    for mount in container.get("Mounts") or []:
        if mount.get("Type") == "bind" and not _under_home(mount.get("Source", "")):
            blockers.append("bind path outside /home/aki")
        if mount.get("Type") not in ("bind", "volume"):
            blockers.append("unsupported mount type")
        if mount.get("Type") == "bind" and mount.get("Propagation") not in (None, "", "rprivate"):
            blockers.append("custom bind propagation")
    if state.get("Error"):
        blockers.append("container has a Docker runtime error")
    if (host.get("RestartPolicy") or {}).get("Name") not in (None, "", "no", "always", "unless-stopped", "on-failure"):
        blockers.append("unsupported restart policy")
    if int((host.get("RestartPolicy") or {}).get("MaximumRetryCount") or 0) > 0:
        blockers.append("limited on-failure retry count")
    return blockers


def _under_home(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        return False
    if ":" in value or any(part in (".", "..") for part in value.split("/")):
        return False
    path = Path(value)
    return path == _ALLOWED_BIND_ROOT or _ALLOWED_BIND_ROOT in path.parents


def _normalized_absolute(value: str) -> bool:
    return (isinstance(value, str) and value.startswith("/") and "\x00" not in value
            and all(part not in ("", ".", "..") for part in value.split("/")[1:]))


def _bind_source_stays_home(value: str) -> bool:
    """Resolve a restored bind source and reject symlinks escaping /home/aki."""
    if not _under_home(value):
        return False
    try:
        root = _ALLOWED_BIND_ROOT.resolve(strict=True)
        resolved = Path(value).resolve(strict=True)
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _docker_env(config: dict[str, Any]) -> list[tuple[str, str]]:
    env: list[tuple[str, str]] = []
    for item in config.get("Env") or []:
        if not isinstance(item, str) or "=" not in item:
            raise RuntimeMigrationError("invalid environment entry")
        key, value = item.split("=", 1)
        if not _ENV_KEY.fullmatch(key) or "\n" in value or "\r" in value or "\x00" in value:
            raise RuntimeMigrationError("environment keys must be valid and values cannot contain line breaks")
        env.append((key, value))
    return env


def _env_file_text(env: list[tuple[str, str]]) -> str:
    # Podman env files accept KEY=value. Newlines have been rejected, so each
    # variable occupies exactly one line; values are never placed in argv.
    lines = []
    for key, value in env:
        if not isinstance(key, str) or not _ENV_KEY.fullmatch(key):
            raise RuntimeMigrationError("invalid environment key")
        if not isinstance(value, str) or any(char in value for char in "\r\n\x00"):
            raise RuntimeMigrationError("environment values cannot contain line breaks")
        lines.append(f"{key}={value}\n")
    return "".join(lines)


def _network_records(runtime: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = runtime.get("network")
    if not isinstance(records, list):
        raise RuntimeMigrationError("Docker root network inventory is missing or invalid")
    result: dict[str, dict[str, Any]] = {}
    for item in records:
        if isinstance(item, dict):
            name = item.get("Name")
            if isinstance(name, str):
                result[name] = item
    return result


def _volume_records(runtime: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = runtime.get("volume")
    if not isinstance(records, list):
        raise RuntimeMigrationError("Docker root volume inventory is missing or invalid")
    result: dict[str, dict[str, Any]] = {}
    for item in records:
        if isinstance(item, dict) and isinstance(item.get("Name"), str):
            result[item["Name"]] = item
    return result


def _port_args(bindings: dict[str, Any]) -> list[str]:
    args: list[str] = []
    for container_port, entries in sorted(bindings.items()):
        # Docker keys look like 8080/tcp. Podman accepts HOSTIP:HOSTPORT:PORT.
        if not re.fullmatch(r"[0-9]{1,5}/(?:tcp|udp|sctp)", container_port):
            raise RuntimeMigrationError("invalid published container port")
        if not (1 <= int(container_port.split("/", 1)[0]) <= 65535):
            raise RuntimeMigrationError("invalid published container port")
        if not entries:
            continue
        for entry in entries or []:
            host_port = str(entry.get("HostPort", ""))
            host_ip = entry.get("HostIp") or "0.0.0.0"
            if not host_port.isdecimal() or not (1 <= int(host_port) <= 65535):
                raise RuntimeMigrationError("invalid published host port")
            if not isinstance(host_ip, str) or any(c in host_ip for c in " \t\r\n\x00"):
                raise RuntimeMigrationError("invalid published host address")
            try:
                parsed_host = ipaddress.ip_address(host_ip)
            except ValueError as exc:
                raise RuntimeMigrationError("invalid published host address") from exc
            if parsed_host.version == 6:
                host_ip = f"[{host_ip}]"
            args += ["--publish", f"{host_ip}:{host_port}:{container_port}"]
    return args


def _network_spec(name: str, network: dict[str, Any]) -> dict[str, Any]:
    _safe_name(name, "network")
    ipam = network.get("IPAM") or {}
    configs = ipam.get("Config") or []
    if len(configs) > 1:
        raise RuntimeMigrationError(f"network {name} has multiple IPAM subnets")
    # Podman already reserves its built-in `bridge` network name. Give the
    # Docker default bridge a stable explicit name while retaining its IPAM.
    target_name = "arcturus-docker-bridge" if name == "bridge" else name
    result = {"name": target_name, "source_name": name, "subnet": None, "gateway": None, "ip_range": None}
    if configs:
        cfg = configs[0]
        result.update(subnet=cfg.get("Subnet"), gateway=cfg.get("Gateway"), ip_range=cfg.get("IPRange"))
        try:
            subnet = ipaddress.ip_network(result["subnet"], strict=False) if result["subnet"] else None
            if result["gateway"] and (subnet is None or ipaddress.ip_address(result["gateway"]) not in subnet):
                raise ValueError
            if result["ip_range"] and (subnet is None or not ipaddress.ip_network(result["ip_range"], strict=False).subnet_of(subnet)):
                raise ValueError
        except ValueError as exc:
            raise RuntimeMigrationError(f"network {name} has invalid IPAM settings") from exc
    return result


def build_plan(inventory: dict[str, Any], snapshots: dict[str, str]) -> dict[str, Any]:
    """Build a portable rootful Docker-to-Podman plan from an inventory.

    ``snapshots`` maps each Docker container ID to its already-committed image
    tag. Image archives are named by a stable digest of the tag and must be
    copied to the target before ``apply``.
    """
    if not isinstance(inventory, dict) or not isinstance(snapshots, dict):
        raise RuntimeMigrationError("inventory and snapshots must be objects")
    runtimes = inventory.get("runtimes") or {}
    docker = runtimes.get("docker:root")
    if not isinstance(docker, dict):
        raise RuntimeMigrationError("rootful Docker inventory is missing")
    rootful_podman = runtimes.get("podman:root")
    if isinstance(rootful_podman, dict) and rootful_podman.get("container"):
        raise RuntimeMigrationError("populated rootful Podman runtime is unsupported")
    rootless = [(label, value) for label, value in runtimes.items()
                if isinstance(label, str) and label.startswith("podman:") and label != "podman:root"
                and isinstance(value, dict) and value.get("container")]
    if rootless:
        raise RuntimeMigrationError("populated rootless runtime is unsupported")
    containers = docker.get("container")
    if not isinstance(containers, list):
        raise RuntimeMigrationError("Docker root container inventory is missing or invalid")
    networks = _network_records(docker)
    volumes = _volume_records(docker)
    planned_containers: list[dict[str, Any]] = []
    planned_networks: dict[str, dict[str, Any]] = {}
    planned_volumes: dict[str, dict[str, Any]] = {}
    image_exports: dict[str, str] = {}
    blockers: list[str] = []
    for container in containers:
        cid = container.get("Id")
        if not isinstance(cid, str) or not cid:
            raise RuntimeMigrationError("Docker container is missing an ID")
        tag = snapshots.get(cid)
        if not isinstance(tag, str) or not tag or tag.startswith("-") or any(c in tag for c in "\x00\r\n\t "):
            blockers.append(f"container {cid[:12]} has no valid committed image snapshot")
            continue
        name = _safe_name(str(container.get("Name", "")).lstrip("/"), "container")
        config = container.get("Config") or {}
        host = container.get("HostConfig") or {}
        state = container.get("State") or {}
        unsupported = _unsupported(container, networks)
        blockers.extend(f"container {name}: {reason}" for reason in unsupported)
        try:
            env = _docker_env(config)
        except RuntimeMigrationError as exc:
            blockers.append(f"container {name}: {exc}")
            env = []
        if config.get("Tty") or config.get("OpenStdin"):
            blockers.append(f"container {name}: interactive TTY/stdin")
        entrypoint = config.get("Entrypoint")
        cmd = config.get("Cmd")
        workdir = config.get("WorkingDir") or ""
        user = config.get("User") or ""
        if any(not isinstance(x, str) or "\x00" in x for x in (workdir, user)):
            blockers.append(f"container {name}: invalid working directory or user")
            workdir, user = "", ""
        args = ["podman", "create", "--name", name, "--pull=never"]
        args += ["--cgroupns", "private"]
        if workdir:
            args += ["--workdir", workdir]
        hostname = config.get("Hostname") or ""
        if hostname:
            try:
                _safe_name(hostname, "hostname")
            except RuntimeMigrationError:
                blockers.append(f"container {name}: invalid hostname")
            else:
                args += ["--hostname", hostname]
        if user:
            args += ["--user", user]
        args += ["--env-file", f"{_ENV_DIR}/{name}.env"]
        restart = (host.get("RestartPolicy") or {}).get("Name") or "no"
        # systemd owns restart and boot behavior for restored workloads.
        args += ["--restart", "no"]
        args += _port_args((host.get("PortBindings") or {}))

        net_map = (container.get("NetworkSettings") or {}).get("Networks") or {}
        mode_name = host.get("NetworkMode")
        if not net_map and mode_name in networks:
            net_map = {mode_name: {}}
        if net_map:
            net_name, endpoint = next(iter(net_map.items()))
            network = networks.get(net_name)
            if network is not None:
                spec = _network_spec(net_name, network)
                planned_networks[net_name] = spec
                args += ["--network", spec["name"]]
                for alias in endpoint.get("Aliases") or []:
                    if isinstance(alias, str) and alias and alias != name:
                        args += ["--network-alias", alias]
                ip = endpoint.get("IPAddress") or ""
                if ip:
                    try:
                        ipaddress.ip_address(ip)
                    except ValueError:
                        blockers.append(f"container {name}: invalid network address")
                    else:
                        args += ["--ip", ip]
        elif host.get("NetworkMode") == "none":
            args += ["--network", "none"]

        for mount in container.get("Mounts") or []:
            kind = mount.get("Type")
            destination = mount.get("Destination")
            if not _normalized_absolute(destination):
                blockers.append(f"container {name}: invalid mount destination")
                continue
            if ":" in destination:
                blockers.append(f"container {name}: mount destination contains an unsupported colon")
                continue
            mode = "rw" if mount.get("RW", True) else "ro"
            if kind == "bind":
                source = mount.get("Source") or ""
                if _under_home(source):
                    args += ["--volume", f"{source}:{destination}:{mode},Z"]
                else:
                    blockers.append(f"container {name}: bind path outside /home/aki")
            elif kind == "volume":
                vol_name = mount.get("Name")
                record = volumes.get(vol_name)
                if not isinstance(vol_name, str) or record is None:
                    blockers.append(f"container {name}: missing local Docker volume inspect data")
                    continue
                if (record.get("Driver") or "local") != "local" or (record.get("Scope") or "local") != "local":
                    blockers.append(f"container {name}: remote Docker volume is unsupported")
                    continue
                if record.get("Options"):
                    blockers.append(f"container {name}: custom Docker volume options are unsupported")
                    continue
                source_path = record.get("Mountpoint")
                if not isinstance(source_path, str) or not source_path.startswith("/"):
                    blockers.append(f"container {name}: invalid Docker volume mountpoint")
                    continue
                _safe_name(vol_name, "volume")
                planned_volumes[vol_name] = {"name": vol_name, "source_path": source_path,
                                             "archive": volume_archive_name(vol_name)}
                args += ["--volume", f"{vol_name}:{destination}:{mode}"]
            else:
                blockers.append(f"container {name}: unsupported mount type")

        if entrypoint is None:
            entrypoint_args: list[str] = []
        elif isinstance(entrypoint, str):
            entrypoint_args = [entrypoint]
        elif isinstance(entrypoint, list) and all(isinstance(x, str) for x in entrypoint):
            entrypoint_args = entrypoint
        else:
            blockers.append(f"container {name}: invalid entrypoint")
            entrypoint_args = []
        if isinstance(cmd, str):
            cmd_args = shlex.split(cmd)
        elif isinstance(cmd, list) and all(isinstance(x, str) for x in cmd):
            cmd_args = cmd
        elif cmd is None:
            cmd_args = []
        else:
            blockers.append(f"container {name}: invalid command")
            cmd_args = []
        if entrypoint is None:
            args += ["--entrypoint", ""]
        elif entrypoint_args:
            args += ["--entrypoint", json.dumps(entrypoint_args, ensure_ascii=False)]
        args.append(tag)
        args.extend(cmd_args)
        label_digest = hashlib.sha256(tag.encode("utf-8")).hexdigest()[:16]
        export_name = f"image-{label_digest}.tar.gz"
        image_exports.setdefault(tag, export_name)
        planned_containers.append({
            "id": cid, "name": name, "image": tag, "image_archive": export_name,
            "desired_running": bool(state.get("Running")), "restart_policy": restart,
            "command": args, "environment": [{"key": k, "value": v} for k, v in env],
            "unit": f"arcturus-migrated-{name}.service",
        })
    if blockers:
        # Keep blocker output free of environment values.
        raise RuntimeMigrationError("unsupported or incomplete runtime inventory: " + "; ".join(sorted(set(blockers))))
    target_network_names = [network["name"] for network in planned_networks.values()]
    if len(target_network_names) != len(set(target_network_names)):
        raise RuntimeMigrationError("Docker network names collide after Podman compatibility mapping")
    return {
        "schemaVersion": 1,
        "runtime": "podman:root",
        "containers": planned_containers,
        "networks": [planned_networks[n] for n in sorted(planned_networks)],
        "volumes": [planned_volumes[n] for n in sorted(planned_volumes)],
        "image_exports": image_exports,
        "dependencies": ["rootful Podman", "systemd", "GNU tar with ACL and xattr support"],
        "blockers": [],
    }


def _run(args: list[str], *, check: bool = True, **kwargs: Any) -> subprocess.CompletedProcess:
    result = subprocess.run(args, check=False, text=True, capture_output=True, **kwargs)
    if check and result.returncode:
        # subprocess output may contain secrets, so only surface the safe argv.
        raise RuntimeMigrationError("command failed: " + shlex.join(args))
    return result


def _os_id() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("ID="):
                return line[3:].strip().strip('"\'')
    except OSError:
        return ""
    return ""


def _validate_archive(directory: Path, filename: str) -> Path:
    if not isinstance(filename, str) or Path(filename).name != filename or filename in ("", ".", ".."):
        raise RuntimeMigrationError("plan contains an unsafe archive filename")
    path = directory / filename
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise RuntimeMigrationError(f"required archive is missing: {filename}") from exc
    if not stat.S_ISREG(mode):
        raise RuntimeMigrationError(f"archive must be a regular file: {filename}")
    return path


def _archive_path(name: str) -> str:
    """Normalize a tar member path while rejecting absolute/traversal names."""
    if not isinstance(name, str) or not name or "\x00" in name or name.startswith("/"):
        raise RuntimeMigrationError("volume archive contains an unsafe member path")
    parts = name.split("/")
    if ".." in parts:
        raise RuntimeMigrationError("volume archive contains a traversal member path")
    normalized = "/".join(part for part in parts if part not in ("", "."))
    return normalized or "."


def _link_target_path(member_path: str, target: str, *, hardlink: bool) -> str:
    """Resolve an archive link lexically and require it to stay under its root."""
    if not isinstance(target, str) or not target or "\x00" in target or target.startswith("/"):
        raise RuntimeMigrationError("volume archive contains an unsafe link target")
    base = "" if hardlink else posixpath.dirname(member_path)
    resolved = posixpath.normpath(posixpath.join(base, target))
    if resolved in ("..", ".") and hardlink:
        raise RuntimeMigrationError("volume archive hardlink target is not a file")
    if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
        raise RuntimeMigrationError("volume archive contains an escaping link")
    return resolved


def _validate_volume_archive(directory: Path, filename: str) -> Path:
    """Validate compressed volume tar paths and links before GNU tar extracts."""
    path = _validate_archive(directory, filename)
    try:
        # Consume the entire gzip stream first so its trailer CRC is checked,
        # including bytes after tar's end markers.
        with gzip.open(path, "rb") as stream:
            while stream.read(1024 * 1024):
                pass
        with tarfile.open(path, mode="r:gz") as archive:
            members: dict[str, tarfile.TarInfo] = {}
            for member in archive:
                member_path = _archive_path(member.name)
                if member_path in members:
                    raise RuntimeMigrationError("volume archive contains duplicate member paths")
                if member_path == "." and not member.isdir():
                    raise RuntimeMigrationError("volume archive root entry must be a directory")
                if member.isfile():
                    if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                        raise RuntimeMigrationError("volume archive contains an unsupported file type")
                elif not (member.isdir() or member.issym() or member.islnk()):
                    raise RuntimeMigrationError("volume archive contains a special or unsupported file type")
                if member.issym():
                    _link_target_path(member_path, member.linkname, hardlink=False)
                elif member.islnk():
                    _link_target_path(member_path, member.linkname, hardlink=True)
                members[member_path] = member

            # GNU tar can consume concatenated compressed streams. Permit only
            # zero padding after the first tar end marker so a second archive
            # cannot hide unchecked members behind a valid prefix.
            tail_size = 0
            while True:
                tail = archive.fileobj.read(64 * 1024)
                if not tail:
                    break
                tail_size += len(tail)
                if any(tail):
                    raise RuntimeMigrationError("volume archive has nonzero data after its tar end marker")
            if tail_size < 512:
                raise RuntimeMigrationError("volume archive has an incomplete tar end marker")

            # Extraction must never write through a link or regular-file parent,
            # regardless of archive member order.
            for member_path in members:
                parent = posixpath.dirname(member_path)
                while parent and parent != ".":
                    ancestor = members.get(parent)
                    if ancestor is not None and not ancestor.isdir():
                        raise RuntimeMigrationError("volume archive has a non-directory member ancestor")
                    parent = posixpath.dirname(parent)

            def archived_regular_file(member_path: str, seen: set[str]) -> bool:
                if member_path in seen:
                    raise RuntimeMigrationError("volume archive contains a cyclic hardlink")
                member = members.get(member_path)
                if member is None:
                    return False
                if member.isfile():
                    return True
                if not member.islnk():
                    return False
                seen.add(member_path)
                target = _link_target_path(member_path, member.linkname, hardlink=True)
                return archived_regular_file(target, seen)

            for member_path, member in members.items():
                if member.islnk():
                    target = _link_target_path(member_path, member.linkname, hardlink=True)
                    if not archived_regular_file(target, {member_path}):
                        raise RuntimeMigrationError("volume archive hardlink target is not an archived file")
    except RuntimeMigrationError:
        raise
    except (OSError, EOFError, gzip.BadGzipFile, tarfile.TarError, ValueError, zlib.error) as exc:
        raise RuntimeMigrationError(f"volume archive is not a valid gzip tar: {filename}") from exc
    return path


def _validated_volume_mountpoint(mountpoint: str, name: str) -> Path:
    """Require Podman to return its canonical, real volume data directory."""
    expected = _PODMAN_VOLUME_ROOT / name / "_data"
    try:
        actual = Path(mountpoint)
        if not actual.is_absolute() or actual != expected:
            raise RuntimeMigrationError("Podman returned an invalid volume mountpoint")
        current = Path("/")
        for part in actual.parts[1:]:
            current /= part
            if current.is_symlink():
                raise RuntimeMigrationError("Podman volume mountpoint traverses a symlink")
        if actual.resolve(strict=True) != actual:
            raise RuntimeMigrationError("Podman returned an invalid volume mountpoint")
        if not actual.is_dir():
            raise RuntimeMigrationError("Podman returned an invalid volume mountpoint")
    except OSError as exc:
        raise RuntimeMigrationError("Podman returned an invalid volume mountpoint") from exc
    return actual


def _service(container: dict[str, Any]) -> str:
    name = container["name"]
    unit = container["unit"]
    # Validate every value used in a systemd directive; no shell is involved.
    _safe_name(name, "container")
    if unit != f"arcturus-migrated-{name}.service":
        raise RuntimeMigrationError("plan contains an unexpected unit name")
    podman = "/usr/bin/podman"
    start = f"{podman} start --attach --sig-proxy {name}"
    stop = f"{podman} stop -t 60 {name}"
    policy = container.get("restart_policy")
    restart = "on-failure" if policy == "on-failure" else ("always" if policy in ("always", "unless-stopped") else "no")
    lines = ["[Unit]", f"Description=Restored Arcturus container {name}", "After=network-online.target", "Wants=network-online.target", "", "[Service]", "Type=simple", f"ExecStart={start}", f"ExecStop={stop}", f"Restart={restart}", "TimeoutStopSec=70"]
    if policy == "unless-stopped":
        lines.append("# systemd enablement records whether this workload should start at boot")
    lines += ["", "[Install]", "WantedBy=multi-user.target", ""]
    return "\n".join(lines)


def apply_plan(plan_path: Path, backup_dir: Path, *, consume_images: bool = False) -> None:
    """Apply a reviewed plan on the target AlmaLinux host as root."""
    if os.geteuid() != 0:
        raise RuntimeMigrationError("apply must run as root")
    if _os_id().lower() != "almalinux":
        raise RuntimeMigrationError("apply target must report ID=almalinux")
    try:
        plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeMigrationError("plan is not readable UTF-8 JSON") from exc
    if not isinstance(plan, dict) or plan.get("schemaVersion") != 1 or plan.get("runtime") != "podman:root":
        raise RuntimeMigrationError("unsupported runtime plan")
    backup_dir = Path(backup_dir)
    if backup_dir.is_symlink() or not backup_dir.is_dir():
        raise RuntimeMigrationError("backup directory must be a real directory")
    backup_dir = backup_dir.resolve(strict=True)
    containers = plan.get("containers")
    networks = plan.get("networks")
    volumes = plan.get("volumes")
    exports = plan.get("image_exports")
    if not all(isinstance(x, list) for x in (containers, networks, volumes)) or not isinstance(exports, dict):
        raise RuntimeMigrationError("runtime plan has invalid resource lists")
    # Fully validate archives and all names before invoking Podman or writing.
    archives: dict[str, Path] = {}
    for label, filename in exports.items():
        if not isinstance(label, str) or not label or filename != "image-" + hashlib.sha256(label.encode("utf-8")).hexdigest()[:16] + ".tar.gz":
            raise RuntimeMigrationError("plan contains an unexpected image archive mapping")
        path = _validate_archive(backup_dir, filename)
        archives[filename] = path
    for volume in volumes:
        _safe_name(volume.get("name"), "volume")
        if volume.get("archive") != volume_archive_name(volume["name"]):
            raise RuntimeMigrationError("plan contains an unexpected volume archive mapping")
        _validate_volume_archive(backup_dir, volume.get("archive"))
    network_names: set[str] = set()
    for network in networks:
        name = _safe_name(network.get("name"), "network")
        if name in network_names:
            raise RuntimeMigrationError("duplicate network name in plan")
        network_names.add(name)
        for field in ("subnet", "gateway", "ip_range"):
            value = network.get(field)
            if value:
                try:
                    ipaddress.ip_network(value, strict=False) if field != "gateway" else ipaddress.ip_address(value)
                except ValueError as exc:
                    raise RuntimeMigrationError("invalid network IPAM value in plan") from exc
    volume_names: set[str] = set()
    for volume in volumes:
        name = _safe_name(volume.get("name"), "volume")
        if name in volume_names:
            raise RuntimeMigrationError("duplicate volume name in plan")
        volume_names.add(name)
    seen: set[str] = set()
    expected_images: set[str] = set()
    for container in containers:
        name = _safe_name(container.get("name"), "container")
        if name in seen:
            raise RuntimeMigrationError("duplicate container name in plan")
        seen.add(name)
        _service(container)
        if not isinstance(container.get("command"), list) or not all(isinstance(x, str) for x in container["command"]):
            raise RuntimeMigrationError("invalid container command in plan")
        command = container["command"]
        if len(command) < 3 or command[:2] != ["podman", "create"]:
            raise RuntimeMigrationError("invalid container create command in plan")
        allowed_options = {"--name", "--pull=never", "--cgroupns", "--workdir", "--hostname", "--user", "--env-file", "--restart",
                           "--publish", "--network", "--network-alias", "--ip", "--volume", "--entrypoint"}
        singleton_options = {"--name", "--pull=never", "--cgroupns", "--workdir", "--hostname", "--user", "--env-file", "--restart", "--network", "--ip", "--entrypoint"}
        seen_options: set[str] = set()
        i = 2
        while i < len(command) and command[i].startswith("--"):
            option = command[i]
            if option not in allowed_options:
                raise RuntimeMigrationError("unsupported option in container create command")
            if option in singleton_options and option in seen_options:
                raise RuntimeMigrationError("duplicate option in container create command")
            seen_options.add(option)
            i += 1
            if option != "--pull=never":
                if i >= len(command):
                    raise RuntimeMigrationError("incomplete option in container create command")
                value = command[i]
                if option == "--name" and value != name:
                    raise RuntimeMigrationError("container name does not match its create command")
                if option == "--env-file" and value != f"{_ENV_DIR}/{name}.env":
                    raise RuntimeMigrationError("environment path does not match its create command")
                if option == "--restart" and value != "no":
                    raise RuntimeMigrationError("container restart policy must be managed by systemd")
                if option == "--cgroupns" and value != "private":
                    raise RuntimeMigrationError("only private cgroup namespaces are supported")
                if option == "--network" and value not in network_names | {"none"}:
                    raise RuntimeMigrationError("container references an unknown network")
                if option == "--network-alias":
                    _safe_name(value, "network alias")
                if option == "--ip":
                    try:
                        ipaddress.ip_address(value)
                    except ValueError as exc:
                        raise RuntimeMigrationError("invalid container IP address") from exc
                if option == "--publish" and not re.fullmatch(r"(?:\[[0-9a-fA-F:]+\]|[^:\s]+):[0-9]{1,5}:[0-9]{1,5}/(?:tcp|udp|sctp)", value):
                    raise RuntimeMigrationError("invalid published port in plan")
                if option == "--volume":
                    parts = value.split(":")
                    if len(parts) != 3 or not _normalized_absolute(parts[1]):
                        raise RuntimeMigrationError("invalid volume mount in plan")
                    source, _, mode = parts
                    allowed_modes = {"ro", "rw", "ro,Z", "rw,Z"}
                    if mode not in allowed_modes or not (_under_home(source) or source in volume_names):
                        raise RuntimeMigrationError("unsupported volume source in plan")
                    if source in volume_names and mode.endswith(",Z"):
                        raise RuntimeMigrationError("named volume cannot use bind relabeling")
                    if _under_home(source) and not _bind_source_stays_home(source):
                        raise RuntimeMigrationError("bind source is missing or resolves outside /home/aki")
                if option == "--entrypoint":
                    try:
                        entrypoint = json.loads(value)
                    except json.JSONDecodeError as exc:
                        if value:
                            raise RuntimeMigrationError("invalid entrypoint in plan") from exc
                    else:
                        if not isinstance(entrypoint, list) or not all(isinstance(x, str) for x in entrypoint):
                            raise RuntimeMigrationError("invalid entrypoint in plan")
                i += 1
        if i >= len(command) or command[i] != container.get("image"):
            raise RuntimeMigrationError("container image does not match its create command")
        env = container.get("environment")
        if not isinstance(env, list):
            raise RuntimeMigrationError("invalid environment in plan")
        try:
            _env_file_text([(x["key"], x["value"]) for x in env])
        except (KeyError, TypeError) as exc:
            raise RuntimeMigrationError("invalid environment in plan") from exc
        if container.get("image_archive") not in archives:
            raise RuntimeMigrationError("container image archive is absent from plan exports")
        if exports.get(container.get("image")) != container.get("image_archive"):
            raise RuntimeMigrationError("container image does not match its archive mapping")
        expected_images.add(container["image"])
    if expected_images != set(exports):
        raise RuntimeMigrationError("image archive exports do not match planned containers")
    podman = shutil.which("podman") or "/usr/bin/podman"
    if not Path("/usr/bin/podman").is_file() or not os.access("/usr/bin/podman", os.X_OK):
        raise RuntimeMigrationError("rootful Podman must be installed at /usr/bin/podman")
    for dependency in ("systemctl", "tar"):
        if shutil.which(dependency) is None:
            raise RuntimeMigrationError("missing runtime dependency: " + dependency)
    # Collision checks happen as a complete preflight, before any writes.
    for container in containers:
        exists = _run([podman, "container", "exists", container["name"]], check=False).returncode
        if exists == 0:
            raise RuntimeMigrationError("container name collision: " + container["name"])
        if exists != 1:
            raise RuntimeMigrationError("cannot preflight container name: " + container["name"])
        unit_path = _UNIT_DIR / container["unit"]
        if unit_path.exists() or unit_path.is_symlink():
            raise RuntimeMigrationError("systemd unit collision: " + container["unit"])
        env_path = _ENV_DIR / f"{container['name']}.env"
        if env_path.exists() or env_path.is_symlink():
            raise RuntimeMigrationError("environment file collision: " + env_path.name)
    for network in networks:
        name = _safe_name(network.get("name"), "network")
        exists = _run([podman, "network", "exists", name], check=False).returncode
        if exists == 0:
            raise RuntimeMigrationError("network name collision: " + name)
        if exists != 1:
            raise RuntimeMigrationError("cannot preflight network name: " + name)
    for volume in volumes:
        name = _safe_name(volume.get("name"), "volume")
        exists = _run([podman, "volume", "exists", name], check=False).returncode
        if exists == 0:
            raise RuntimeMigrationError("volume name collision: " + name)
        if exists != 1:
            raise RuntimeMigrationError("cannot preflight volume name: " + name)
    for image in expected_images:
        exists = _run([podman, "image", "exists", image], check=False).returncode
        if exists == 0:
            raise RuntimeMigrationError("image tag collision: " + image)
        if exists != 1:
            raise RuntimeMigrationError("cannot preflight image tag: " + image)
    if _ENV_DIR.is_symlink() or (_ENV_DIR.exists() and not _ENV_DIR.is_dir()):
        raise RuntimeMigrationError("environment directory is not a real directory")
    # All inputs and collision checks are complete. Import snapshot images.
    for filename, path in archives.items():
        # Podman detects gzip-compressed archives from the file supplied with
        # --input. Passing a Python gzip stream to subprocess.run would hand
        # it the compressed file descriptor instead of decompressed bytes.
        _run([podman, "load", "--input", str(path)])
        if consume_images:
            # Only the plan-validated, hashed export in this backup directory
            # is eligible, and only after Podman has imported it successfully.
            validated_path = _validate_archive(backup_dir, filename)
            validated_path.unlink()
    for network in networks:
        args = [podman, "network", "create", "--driver", "bridge"]
        for field, flag in (("subnet", "--subnet"), ("gateway", "--gateway"), ("ip_range", "--ip-range")):
            if network.get(field):
                args += [flag, network[field]]
        args.append(_safe_name(network.get("name"), "network"))
        _run(args)
    for volume in volumes:
        name = _safe_name(volume.get("name"), "volume")
        _run([podman, "volume", "create", name])
        mountpoint = _run([podman, "volume", "inspect", "--format", "{{.Mountpoint}}", name]).stdout.strip()
        mountpoint_path = _validated_volume_mountpoint(mountpoint, name)
        if next(mountpoint_path.iterdir(), None) is not None:
            raise RuntimeMigrationError("new Podman volume mountpoint is not empty")
        archive = _validate_volume_archive(backup_dir, volume["archive"])
        _run(["tar", "--numeric-owner", "--acls", "--xattrs", "--xattrs-exclude=security.selinux", "-xpf", str(archive), "-C", str(mountpoint_path)])
    _ENV_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for container in containers:
        name = container["name"]
        env_path = _ENV_DIR / f"{name}.env"
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(_env_file_text([(x["key"], x["value"]) for x in container["environment"]]))
        _run(container["command"])
        unit_path = _UNIT_DIR / container["unit"]
        unit_path.write_text(_service(container), encoding="utf-8")
        unit_path.chmod(0o644)
    _run(["systemctl", "daemon-reload"])
    for container in containers:
        unit = container["unit"]
        if container.get("desired_running"):
            _run(["systemctl", "enable", "--now", unit])
            active = _run(["systemctl", "is-active", unit], check=False)
            inspect = _run([podman, "inspect", "--format", "{{.State.Running}}", container["name"]], check=False)
            if active.returncode or active.stdout.strip() != "active" or inspect.returncode or inspect.stdout.strip() != "true":
                raise RuntimeMigrationError("restored container did not become active: " + container["name"])
        # Newly written units are disabled unless explicitly enabled above.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("apply",))
    parser.add_argument("plan_json")
    parser.add_argument("backup_dir")
    parser.add_argument("--consume-images", action="store_true",
                         help="delete each validated image export after Podman loads it successfully")
    args = parser.parse_args(argv)
    try:
        apply_plan(Path(args.plan_json), Path(args.backup_dir), consume_images=args.consume_images)
    except RuntimeMigrationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
