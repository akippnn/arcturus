#!/usr/bin/env python3
"""Wired, resumable Pi 5 backup and Debian -> AlmaLinux migration.

Destructive operations are separate commands. Each flash requires a complete
verified offline backup, a card CID confirmation and a RAM independence check.
No Tailscale or third-party Python packages are used.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import gzip
import hashlib
import importlib.util
import io
import ipaddress
import json
import lzma
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

from pi_migration_archive import MigrationArchiveError, inspect_restore_selection, sha256_file, verify_backup
from provision_rpi_sd import checksum_from_manifest, parse_image_index
import pi_migration_runtime as runtime_acceptance
from pi_migration_runtime import RuntimeMigrationError, build_plan, volume_archive_name, _validate_volume_archive
from pi_migration_files import FilesMigrationError, build_bundle, validate_bundle
import pi_migration_boot as boot_acceptance

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "pi-migration"
ARCTURUS_MIGRATION_ROOT = "/var/lib/arcturus-migration"
CLOUDFLARED_PATH = "/usr/local/bin/cloudflared"
RUNTIME_UNIT_DIR = "/etc/systemd/system"
RUNTIME_ENV_DIR = "/etc/arcturus-migrated"


class MigrationError(RuntimeError):
    pass


def host_adapter():
    spec = importlib.util.spec_from_file_location("arcturus_host_restore", ASSETS / "restore-host.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def durable_json(path, data):
    part = path.with_name(path.name + ".partial")
    with part.open("w", encoding="utf-8") as out:
        os.chmod(part, 0o600)
        json.dump(data, out, indent=2)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    part.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def wired_identity(inventory, host):
    if "Raspberry Pi 5" not in inventory.get("model", ""):
        raise MigrationError("SSH peer is not a Raspberry Pi 5")
    for address in inventory["addresses"]:
        if address["ifname"].startswith(("lo", "tailscale", "wlan", "docker", "br-", "veth")):
            continue
        for item in address.get("addr_info", []):
            if item.get("local") == host and item.get("family") == "inet":
                route = next((r for r in inventory["routes"] if r.get("dst") == "default" and r.get("dev") == address["ifname"]), None)
                if route:
                    return {"interface": address["ifname"], "mac": address["address"], "address": host,
                            "prefix": item["prefixlen"], "gateway": route["gateway"]}
    raise MigrationError("SSH address is not on a wired interface with a default route")


def validate_source(inventory, host):
    network = wired_identity(inventory, host)
    if not re.search(r'^ID=debian$', inventory.get("os", ""), re.M):
        raise MigrationError("RAM builder currently supports Debian only")
    target = inventory["target"]
    if target["device"] != "/dev/mmcblk0" or not re.fullmatch(r"[0-9a-f]{32}", target["cid"]):
        raise MigrationError("unsupported card identity")
    # This first implementation must account for every persistent filesystem.
    def walk(items):
        for item in items:
            yield item
            yield from walk(item.get("children", []))
    mounts = list(walk(inventory["mounts"]["filesystems"]))
    if next((m.get("source") for m in mounts if m["target"] == "/"), None) != "/dev/mmcblk0p2":
        raise MigrationError("unsupported root layout; expected SD ext4 partition 2")
    for mount in mounts:
        source = mount.get("source", "")
        if source.startswith("/dev/") and source not in ("/dev/mmcblk0p1", "/dev/mmcblk0p2"):
            raise MigrationError("additional mounted device needs its own backup: " + source)
    return network


class Peer:
    def __init__(self, state, recovery=False, port=None):
        self.state = state
        self.recovery = recovery
        self.port = port

    def command(self, command):
        s = self.state
        if self.recovery:
            command = "export PATH=/bin:/usr/bin:/sbin:/usr/sbin; " + command
        args = ["ssh", "-T", "-i", s["key"], "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "IPQoS=none", "-o", "Compression=no", "-o", "ControlPath=none",
                "-o", "StrictHostKeyChecking=yes", "-o", "HostKeyAlias=" + s["host_key_alias"],
                "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
                "-p", str(self.port or (2222 if self.recovery else 22)),
                ("root" if self.recovery else s["user"]) + "@" + s["host"], command]
        if "known_hosts" in s:
            args[1:1] = ["-o", "UserKnownHostsFile=" + json.dumps(s["known_hosts"]), "-o", "GlobalKnownHostsFile=/dev/null"]
        return args

    def run(self, command, data=None, timeout=180):
        proc = subprocess.run(self.command(command), input=data, capture_output=True, timeout=timeout)
        if proc.returncode:
            # Inventory may contain credentials; report stderr only.
            raise MigrationError("remote command failed: " + proc.stderr.decode(errors="replace")[-2500:])
        return proc.stdout

    def stream(self, command, destination, compress=False):
        check_destination(self.state)
        part = destination.with_name(destination.name + ".partial")
        if destination.exists() or part.exists():
            raise MigrationError("refusing to overwrite backup artifact: " + destination.name)
        raw_hash = hashlib.sha256()
        raw_bytes = 0
        last = 0
        with tempfile.TemporaryFile() as errors, part.open("xb") as output:
            os.chmod(part, 0o600)
            proc = subprocess.Popen(self.command(command), stdout=subprocess.PIPE, stderr=errors)
            try:
                sink = gzip.GzipFile(fileobj=output, mode="wb", compresslevel=1, mtime=0) if compress else output
                try:
                    while chunk := proc.stdout.read(1024 * 1024):
                        check_destination(self.state)
                        sink.write(chunk)
                        raw_hash.update(chunk)
                        raw_bytes += len(chunk)
                        if raw_bytes - last >= 512 * 1024**2:
                            print(f"{destination.name}: {raw_bytes / 1024**3:.1f} GiB transferred", flush=True)
                            last = raw_bytes
                finally:
                    if compress:
                        sink.close()
                if proc.wait() != 0:
                    errors.seek(0)
                    raise MigrationError("backup transfer failed: " + errors.read().decode(errors="replace")[-2500:])
                output.flush()
                os.fsync(output.fileno())
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            finally:
                proc.stdout.close()
        part.replace(destination)
        metadata = {"bytes": destination.stat().st_size, "sha256": sha256_file(destination)}
        if compress:
            metadata.update(uncompressed_bytes=raw_bytes, uncompressed_sha256=raw_hash.hexdigest())
        return metadata

    def upload(self, source, destination, *, sudo=False, resume=False):
        # Resume is confined to the public signed image in guarded recovery RAM.
        # Other payloads are rewritten in their coordinator-owned destination.
        offset = 0
        if resume:
            if not self.recovery or sudo or destination != "/run/alma.raw.xz":
                raise MigrationError("resumed uploads are limited to the RAM OS image")
            quoted = shlex.quote(destination)
            command = "if test -L " + quoted + " || { test -e " + quoted + " && ! test -f " + quoted + "; }; then exit 1; fi; if test -f " + quoted + "; then stat -c %s " + quoted + "; sha256sum " + quoted + "; else echo 0; fi"
            lines = self.run(command).decode().splitlines()
            offset = int(lines[0])
            if offset < 0 or offset > source.stat().st_size:
                raise MigrationError("existing RAM image has an invalid length")
            if offset:
                digest = hashlib.sha256()
                with source.open("rb") as original:
                    remaining = offset
                    while remaining:
                        chunk = original.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise MigrationError("source image changed during prefix verification")
                        digest.update(chunk); remaining -= len(chunk)
                if len(lines) != 2 or lines[1].split()[0] != digest.hexdigest():
                    raise MigrationError("existing RAM image prefix differs from the signed source; refusing to append")
            if offset == source.stat().st_size:
                print("Complete RAM image already matches the signed source.", flush=True)
                return
        command = ("sudo -n tee " + shlex.quote(destination) + " > /dev/null") if sudo else ("cat " + (">> " if offset else "> ") + shlex.quote(destination))
        total = offset
        last = offset
        deadline = time.monotonic() + 3600
        with source.open("rb") as stream, tempfile.TemporaryFile() as errors:
            stream.seek(offset)
            proc = subprocess.Popen(self.command(command), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors, bufsize=0)
            try:
                os.set_blocking(proc.stdin.fileno(), False)
                with selectors.DefaultSelector() as writable:
                    writable.register(proc.stdin, selectors.EVENT_WRITE)
                    while chunk := stream.read(1024 * 1024):
                        check_destination(self.state)
                        pending = memoryview(chunk)
                        while pending:
                            if time.monotonic() > deadline:
                                raise MigrationError("upload timed out; any partial RAM image can be checksum-checked and resumed")
                            if proc.poll() is not None:
                                errors.seek(0)
                                raise MigrationError("upload SSH exited before the complete artifact was sent: " + errors.read().decode(errors="replace")[-1000:])
                            if not writable.select(timeout=1):
                                continue
                            try:
                                sent = os.write(proc.stdin.fileno(), pending)
                            except BlockingIOError:
                                continue
                            pending = pending[sent:]
                            total += sent
                        if total - last >= 8 * 1024**2:
                            print(f"{source.name}: {total / 1024**2:.0f}/{source.stat().st_size / 1024**2:.0f} MiB sent", flush=True)
                            last = total
                proc.stdin.close()
                if proc.wait(timeout=180) != 0:
                    errors.seek(0)
                    raise MigrationError("upload failed: " + errors.read().decode(errors="replace")[-1000:])
            except BrokenPipeError:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
                errors.seek(0)
                raise MigrationError("upload connection closed: " + errors.read().decode(errors="replace")[-1000:]) from None
            except BaseException:
                proc.kill(); proc.wait()
                raise
            finally:
                if not proc.stdin.closed:
                    proc.stdin.close()
        check_destination(self.state)


def check_destination(state):
    directory = Path(state["directory"])
    anchor = Path(state["storage_anchor"])
    if not anchor.is_mount() or directory.stat().st_dev != state["storage_device"]:
        raise MigrationError("backup volume was disconnected or replaced; refusing to continue")
    if shutil.disk_usage(directory).free < 512 * 1024**2:
        raise MigrationError("backup volume has less than 512 MiB free")


def state_load(path):
    if path is None:
        raise MigrationError("--session is required for this command")
    path = Path(path).expanduser().resolve()
    state = json.loads((path / "session.json").read_text())
    if state["directory"] != str(path):
        raise MigrationError("session path changed; verify the original backup volume")
    check_destination(state)
    return state


def state_save(state):
    durable_json(Path(state["directory"]) / "session.json", state)


def runtime_signature(inventory):
    records = {}
    for label, runtime in inventory["runtimes"].items():
        containers = runtime.get("container")
        if isinstance(containers, list):
            records[label] = sorted(({
                "Id": c["Id"], "Image": c["Image"], "Config": c["Config"],
                "HostConfig": c["HostConfig"], "Mounts": c.get("Mounts"),
                "networks": {name: {key: endpoint.get(key) for key in ("Aliases", "IPAMConfig", "Links", "DriverOpts")}
                             for name, endpoint in c.get("NetworkSettings", {}).get("Networks", {}).items()},
            } for c in containers), key=lambda c: c["Id"])
    return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()


def recovery_peer(state):
    peer = Peer(state, recovery=True)
    try:
        if peer.run("cat /run/arcturus-ram-ready", timeout=12).decode().strip() == state["nonce"]:
            return peer
    except (MigrationError, subprocess.TimeoutExpired):
        pass
    network = ipaddress.ip_network(f'{state["network"]["address"]}/{state["network"]["prefix"]}', strict=False)
    if network.num_addresses > 1024:
        raise MigrationError("recovery DHCP rediscovery is limited to /22 or narrower LANs")
    def port_open(host):
        try:
            with socket.create_connection((str(host), 2222), timeout=.3):
                return str(host)
        except OSError:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        hosts = [host for host in pool.map(port_open, network.hosts()) if host]
    for host in hosts:
        temporary = dict(state, host=host)
        candidate = Peer(temporary, recovery=True)
        try:
            if candidate.run("cat /run/arcturus-ram-ready", timeout=12).decode().strip() == state["nonce"]:
                state["host"] = host
                state_save(state)
                return Peer(state, recovery=True)
        except (MigrationError, subprocess.TimeoutExpired):
            continue
    raise MigrationError("no RAM recovery peer with the pinned SSH key and session nonce is reachable")


def guard(peer):
    s = peer.state
    t = s["target"]
    print(peer.run(shlex.join(["/recovery/ram-guard.sh", s["nonce"], t["cid"], str(t["size_bytes"])] )).decode().strip(), flush=True)


def discover(args):
    candidates = {args.host}
    try:
        candidates.update(a[4][0] for a in socket.getaddrinfo(args.hostname + ".local", 22, socket.AF_INET))
    except socket.gaierror:
        pass
    arp = subprocess.run(["arp", "-an"], capture_output=True, text=True)
    candidates.update(re.findall(r"\(([0-9.]+)\)", arp.stdout))
    if args.scan_subnet:
        network = ipaddress.ip_network(args.scan_subnet)
        if not network.is_private or network.num_addresses > 1024:
            raise MigrationError("scan must be a private LAN subnet of at most 1024 addresses")
        def open_port(host):
            try:
                with socket.create_connection((str(host), 22), timeout=.4):
                    return str(host)
            except OSError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            candidates.update(x for x in pool.map(open_port, network.hosts()) if x)
    base = {"key": str(args.key.expanduser().resolve()), "user": args.user, "host_key_alias": args.host_key_alias}
    found = []
    for host in sorted(candidates):
        try:
            addr = ipaddress.ip_address(host)
            if not addr.is_private or addr.is_multicast:
                continue
            state = dict(base, host=host)
            data = Peer(state).run("hostname; cat /proc/device-tree/model; ip -j -4 address", timeout=12).decode()
            if data.splitlines()[0] != args.hostname or "Raspberry Pi 5" not in data:
                continue
            interfaces = json.loads(data[data.index("["):])
            wired = any(i["ifname"].startswith(("eth", "en")) and any(a.get("local") == host for a in i.get("addr_info", [])) for i in interfaces)
            if wired:
                found.append(host)
        except (OSError, ValueError, subprocess.TimeoutExpired, MigrationError):
            continue
    if not found:
        raise MigrationError("no wired Pi verified against the pinned existing SSH host key; add --scan-subnet for DHCP discovery")
    print(json.dumps({"verified_wired_addresses": found, "host_key_alias": args.host_key_alias}, indent=2))
    return found


def initialize(args):
    hosts = discover(args)
    if len(hosts) != 1:
        raise MigrationError("multiple wired addresses; choose the correct --host explicitly")
    base = args.backup_dir.expanduser().resolve()
    anchor = base
    while not anchor.is_mount() and anchor != anchor.parent:
        anchor = anchor.parent
    if anchor == Path("/"):
        raise MigrationError("backup must be on an independently mounted external volume")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = base / (datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    state = {"schemaVersion": 1, "directory": str(directory), "storage_anchor": str(anchor),
             "storage_device": directory.stat().st_dev, "host": hosts[0], "user": args.user,
             "key": str(args.key.expanduser().resolve()), "host_key_alias": args.host_key_alias,
             "nonce": uuid.uuid4().hex, "phase": "inventory"}
    inventory = json.loads(Peer(state).run("sudo -n python3 -", (ASSETS / "inventory.py").read_bytes(), timeout=900))
    state["network"] = validate_source(inventory, hosts[0])
    state["target"] = inventory["target"]
    if shutil.disk_usage(directory).free < 3 * state["target"]["size_bytes"]:
        raise MigrationError("require at least three card capacities free for raw backup, archives and runtime exports")
    durable_json(directory / "inventory.json", inventory)
    alias = "arcturus-pi-" + state["nonce"]
    public = inventory["ssh_host_public_key"].split()
    if public[0] != "ssh-ed25519" or len(public) < 2:
        raise MigrationError("source does not provide the required Ed25519 SSH host identity")
    known = directory / "known_hosts"
    known.write_text(alias + " " + " ".join(public[:2]) + "\n" + "".join("[" + alias + "]:" + str(port) + " " + " ".join(public[:2]) + "\n" for port in (2222, 2223)))
    known.chmod(0o600)
    state["host_key_alias"] = alias
    state["known_hosts"] = str(known)
    state_save(state)
    print("Session: " + str(directory))
    print("Sensitive inventory saved; no container environment values are printed.")
    return state


def export_images(state):
    directory = Path(state["directory"])
    peer = Peer(state)
    if state.get("ram_probe_checksums") != state.get("boot_checksums"):
        raise MigrationError("verified recovery SSH and tools are required before quiescing workloads")
    if state.get("image_exports"):
        raise MigrationError("image snapshots already exist; refusing to replace a recorded export")
    inventory = json.loads(peer.run("sudo -n python3 -", (ASSETS / "inventory.py").read_bytes(), timeout=900))
    validate_source(inventory, state["host"])
    # Validate the full portability contract before stopping any workload.
    containers = inventory["runtimes"]["docker:root"]["container"]
    if not isinstance(containers, list):
        raise MigrationError("Docker runtime inventory is unavailable; portable restore needs manual review")
    tags = {c["Id"]: "localhost/arcturus-migration/" + c["Name"].lstrip("/").lower() + ":" + state["nonce"] for c in containers}
    plan = build_plan(inventory, tags)
    adapter = host_adapter()
    try:
        host_plan = adapter.build_plan(inventory, inventory["dependencies"], verify_target=False)
    except adapter.HostRestoreError as exc:
        raise MigrationError("source host restoration plan failed: " + str(exc)) from exc
    if not host_plan["ready"]:
        raise MigrationError("source host restoration needs an adapter: " + "; ".join(host_plan["blockers"]))
    source_units = []
    for name, unit in inventory.get("custom_units", {}).items():
        content = unit["content"]
        if re.search(r"^ExecStart=.*\b(?:docker|podman)\b", content, re.M):
            if not re.fullmatch(r"[a-zA-Z0-9_.@-]+\.service", name):
                raise MigrationError("source runtime unit has an unsafe name")
            stop_lines = [line for line in content.splitlines() if line.startswith(("ExecStop=", "ExecStopPost="))]
            if any(re.search(r"\b(?:down|rm|remove)\b", line) for line in stop_lines):
                raise MigrationError("source unit removes containers during stop; requires a separate quiesce adapter: " + name)
            if unit.get("active") == "active\n":
                source_units.append(name)
    durable_json(directory / "inventory.json", inventory)
    durable_json(directory / "runtime-plan.json", plan)
    durable_json(directory / "host-plan.json", host_plan)
    durable_json(directory / "dependencies.json", inventory["dependencies"])
    state["runtime_signature"] = runtime_signature(inventory)
    state["snapshot_tags"] = tags
    state["source_running"] = [c["Id"] for c in containers if c["State"].get("Running")]
    state["source_runtime_units"] = source_units
    state["phase"] = "workloads-stopping"
    state_save(state)
    if source_units:
        peer.run(shlex.join(["sudo", "-n", "systemctl", "stop"] + source_units), timeout=300)
    if state["source_running"]:
        peer.run(shlex.join(["sudo", "-n", "docker", "stop", "-t", "60"] + state["source_running"]), timeout=300)
    state["phase"] = "workloads-stopped"
    state_save(state)
    outputs = {}
    for cid, tag in tags.items():
        peer.run(shlex.join(["sudo", "-n", "docker", "commit", cid, tag]), timeout=300)
        name = plan["image_exports"][tag]
        if (directory / name).exists():
            raise MigrationError("runtime export already exists; use a new inventory session")
        print("Exporting stopped container snapshot: " + cid[:12], flush=True)
        outputs[name] = peer.stream(shlex.join(["sudo", "-n", "docker", "save", tag]), directory / name, compress=True)
        state["image_exports"] = outputs
        state_save(state)
    state["image_exports"] = outputs
    state["phase"] = "images-exported"
    state_save(state)


def capture_source(state):
    inventory = json.loads(Peer(state).run("sudo -n python3 -", (ASSETS / "inventory.py").read_bytes(), timeout=900))
    if validate_source(inventory, state["host"]) != state["network"] or inventory["target"] != state["target"]:
        raise MigrationError("source identity or wired lease differs from staged recovery")
    tags = {c["Id"]: "localhost/arcturus-migration/" + c["Name"].lstrip("/").lower() + ":" + state["nonce"] for c in inventory["runtimes"]["docker:root"]["container"]}
    build_plan(inventory, tags)
    durable_json(Path(state["directory"]) / "source-preflight.json", inventory)
    print("Fresh private source inventory and supported runtime plan captured; workloads remain running.")


def preflight_source(state):
    capture_source(state)
    inventory = json.loads((Path(state["directory"]) / "source-preflight.json").read_text())
    plan = host_adapter().build_plan(inventory, inventory["dependencies"], verify_target=False)
    if not plan["ready"]:
        raise MigrationError("source host restoration needs an adapter: " + "; ".join(plan["blockers"]))
    durable_json(Path(state["directory"]) / "host-preflight.json", plan)
    print("Source host services, timers, dependency pins and user lifecycle passed portable planning.")


def prepare_ram(state, refresh=False):
    if state["phase"] not in ("inventory", "images-exported", "ram-prepared"):
        raise MigrationError("RAM preparation requires a live inventory phase")
    peer = Peer(state)
    if refresh:
        if not state.get("ram_prepared"):
            raise MigrationError("there is no previously verified RAM build to refresh")
        peer.run("sudo -n sha256sum -c -", state["boot_checksums"].encode())
    elif state.get("ram_prepared"):
        raise MigrationError("use refresh-ram to replace an owned staged recovery image")
    peer.run("sudo -n apt-get update && sudo -n apt-get install -y --no-install-recommends dropbear-bin parted busybox-static gcc libc6-dev", timeout=900)
    stage = "/run/arcturus-pi-stage." + state["nonce"] + "." + uuid.uuid4().hex[:8]
    public = subprocess.run(["ssh-keygen", "-y", "-f", state["key"]], capture_output=True, check=True).stdout
    with tempfile.TemporaryFile() as bundle:
        with tarfile.open(fileobj=bundle, mode="w") as archive:
            for path in list(ASSETS.glob("*.sh")) + list(ASSETS.glob("*.c")):
                archive.add(path, arcname=path.name)
            import io
            info = tarfile.TarInfo("authorized_keys")
            info.size = len(public)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(public))
        bundle.seek(0)
        peer.run("sudo -n mkdir -m 700 " + shlex.quote(stage) + " && sudo -n tar -xf - -C " + shlex.quote(stage), bundle.read())
    if refresh:
        peer.run("sudo -n tee " + shlex.quote(stage + "/boot-checksums") + " > /dev/null", state["boot_checksums"].encode())
    n, t = state["network"], state["target"]
    command = ["sudo", "-n", "bash", stage + "/build-ram.sh", stage, state["nonce"], n["mac"], n["address"], str(n["prefix"]), n["gateway"], t["cid"], str(t["size_bytes"])]
    if refresh:
        command.append(state["nonce"])
    print(peer.run(shlex.join(command), timeout=900).decode(), flush=True)
    state["boot_checksums"] = peer.run("sudo -n cat " + stage + "/boot-checksums").decode()
    state["phase"] = "ram-prepared"
    state["ram_prepared"] = True
    state_save(state)


def boot_ram(state):
    if not state.get("ram_prepared"):
        raise MigrationError("prepare-ram must complete first")
    if state.get("ram_probe_checksums") != state.get("boot_checksums"):
        raise MigrationError("probe-ram must verify SSH authentication against this exact recovery build first")
    if "runtime_signature" not in state:
        raise MigrationError("export-images must quiesce and snapshot the source runtime first")
    # Re-check the live network lease before changing boot state.
    inventory = json.loads(Peer(state).run("sudo -n python3 -", (ASSETS / "inventory.py").read_bytes(), timeout=900))
    if validate_source(inventory, state["host"]) != state["network"] or inventory["target"] != state["target"]:
        raise MigrationError("wired lease or target changed; create a fresh RAM environment")
    if runtime_signature(inventory) != state.get("runtime_signature", runtime_signature(inventory)):
        raise MigrationError("runtime definitions changed since image export; make a fresh backup session")
    for runtime in inventory["runtimes"].values():
        if isinstance(runtime.get("container"), list) and any(c.get("State", {}).get("Running") for c in runtime["container"]):
            raise MigrationError("a container resumed running after quiescing; stop and recapture before RAM boot")
    Peer(state).run("sudo -n sha256sum -c -", state["boot_checksums"].encode())
    state["phase"] = "ram-boot-requested"
    state_save(state)
    try:
        Peer(state).run("sudo -n reboot '0 tryboot'", timeout=20)
    except (MigrationError, subprocess.TimeoutExpired):
        pass  # SSH may disconnect during the authorized reboot.
    for _ in range(36):
        time.sleep(5)
        try:
            peer = recovery_peer(state)
            guard(peer)
            state["phase"] = "ram-running"
            state_save(state)
            print("RAM recovery verified on wired SSH port 2222. SD remains unmounted.")
            return
        except (MigrationError, subprocess.TimeoutExpired):
            continue
    raise MigrationError("RAM SSH not verified; no SD write occurred. Power-cycle to boot the original Debian config.")


def probe_ram(state):
    peer = Peer(state)
    peer.run("sudo -n sha256sum -c -", state["boot_checksums"].encode())
    command = shlex.join(["sudo", "-n", "bash", "-s", "--", state["nonce"], state["target"]["cid"]])
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(peer.command(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        try:
            process.stdin.write((ASSETS / "probe-ram.sh").read_bytes())
            process.stdin.close()
            probe = Peer(state, recovery=True, port=2223)
            deadline = time.monotonic() + 40
            last_error = ""
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    errors.seek(0)
                    raise MigrationError("recovery SSH probe exited early: " + errors.read().decode(errors="replace")[-1500:])
                try:
                    nonce = probe.run("cat /recovery/nonce; id -u; tar --version | head -n 1; dd --version | head -n 1; losetup --version", timeout=8).decode().splitlines()
                    if nonce[:2] != [state["nonce"], "0"] or "GNU tar" not in nonce[2] or "coreutils" not in nonce[3] or "util-linux" not in nonce[4]:
                        raise MigrationError("staged recovery tool identity differs from the required payload")
                    print("Recovery SSH key authentication and full backup/flash tools verified.", flush=True)
                    break
                except (MigrationError, subprocess.TimeoutExpired) as exc:
                    last_error = str(exc)
                    time.sleep(2)
            else:
                errors.seek(0)
                raise MigrationError("staged RAM SSH key authentication did not pass; reboot remains disabled: " + last_error + "\n" + errors.read().decode(errors="replace")[-1500:])
            process.wait(timeout=60)
            if process.returncode:
                errors.seek(0)
                raise MigrationError("recovery probe cleanup failed: " + errors.read().decode(errors="replace")[-1500:])
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            process.stdout.close()
    state["ram_probe_checksums"] = state["boot_checksums"]
    state_save(state)


def offline_backup(state):
    peer = recovery_peer(state)
    guard(peer)
    directory = Path(state["directory"])
    t = state["target"]
    files = dict(state.get("image_exports", {}))
    files["inventory.json"] = {"bytes": (directory / "inventory.json").stat().st_size, "sha256": sha256_file(directory / "inventory.json")}
    files["whole-card.raw.gz"] = peer.stream("dd if=/dev/mmcblk0 bs=4M status=none", directory / "whole-card.raw.gz", compress=True)
    if files["whole-card.raw.gz"]["uncompressed_bytes"] != t["size_bytes"]:
        raise MigrationError("raw backup did not cover the entire card")
    remote_hash = peer.run("sha256sum /dev/mmcblk0", timeout=1800).decode().split()[0]
    if remote_hash != files["whole-card.raw.gz"]["uncompressed_sha256"]:
        raise MigrationError("offline card checksum disagrees with backup")
    try:
        # Replay would modify the saved source. Require clean ext4 before noload.
        peer.run("e2fsck -fn /dev/mmcblk0p2", timeout=1800)
        peer.run("mount -t ext4 -o ro,noload /dev/mmcblk0p2 /backup-root && mount -t vfat -o ro /dev/mmcblk0p1 /backup-boot")
        for name, mount in (("rootfs.tar.gz", "/backup-root"), ("bootfs.tar.gz", "/backup-boot")):
            files[name] = peer.stream(shlex.join(["tar", "--acls", "--xattrs", "--numeric-owner", "--sparse", "--one-file-system", "-C", mount, "-cf", "-", "."]), directory / name, compress=True)
        plan_path = directory / "runtime-plan.json"
        if plan_path.exists():
            plan = json.loads(plan_path.read_text())
            files["runtime-plan.json"] = {"bytes": plan_path.stat().st_size, "sha256": sha256_file(plan_path)}
            for volume in plan["volumes"]:
                source = Path(volume["source_path"])
                expected = Path("/var/lib/docker/volumes") / volume["name"] / "_data"
                if source != expected:
                    raise MigrationError("volume lies outside canonical Docker local storage; preserve separately")
                command = ["tar", "--acls", "--xattrs", "--numeric-owner", "--sparse", "-C", "/backup-root" + str(source), "-cf", "-", "."]
                files[volume["archive"]] = peer.stream(shlex.join(command), directory / volume["archive"], compress=True)
        # Keep a self-contained on-card rescue ready before replacing the boot FS.
        peer.run("mkdir -p /run/persistent-recovery; cp /backup-boot/arcturus-recovery.img /run/persistent-recovery/recovery.img")
    finally:
        peer.run("umount /backup-boot 2>/dev/null; umount /backup-root 2>/dev/null; true")
    guard(peer)
    manifest = {"schemaVersion": 1, "offline": True, "target": t, "files": files,
                "source": {"hostname": state["host_key_alias"], "wired": state["network"]}}
    durable_json(directory / "manifest.json", manifest)
    print("Verifying all backup artifacts, gzip integrity and tar metadata...", flush=True)
    verify_backup(directory)
    state["phase"] = "backup-verified"
    state_save(state)
    print("Complete offline backup verified: " + str(directory))


def image_metadata(path):
    digest = hashlib.sha256()
    size = 0
    with lzma.open(path) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return {"compressed_sha256": sha256_file(path), "raw_sha256": digest.hexdigest(), "raw_bytes": size}


def fetch_image(state, args):
    # Verification key is supplied independently by the operator, fingerprint pinned.
    if not re.fullmatch(r"[0-9A-Fa-f]{40}", args.signing_fingerprint):
        raise MigrationError("pin a full 40-character official signing-key fingerprint")
    directory = Path(state["directory"])
    repository = "https://repo.almalinux.org/almalinux/10/raspberrypi/images/"
    with urllib.request.urlopen(repository, timeout=30) as response:
        choices = parse_image_index(repository, response.read().decode(), 10)
    choices = [c for c in choices if c.scheme == "gpt" and "latest" not in c.url.lower() and "beta" not in c.url.lower()]
    if not choices:
        raise MigrationError("no versioned official headless GPT Pi image")
    # Numeric version/date ordering, not lexicographic (10.10 must beat 10.9).
    choice = max(choices, key=lambda c: tuple(map(int, re.findall(r"\d+", Path(c.url).name))))
    name = Path(urllib.parse.urlparse(choice.url).path).name
    for item in ("CHECKSUM", "CHECKSUM.asc"):
        with urllib.request.urlopen(repository + item, timeout=30) as response:
            (directory / item).write_bytes(response.read())
    with tempfile.TemporaryDirectory() as home:
        os.chmod(home, 0o700)
        base = ["gpg", "--batch", "--homedir", home]
        subprocess.run(base + ["--import", str(args.signing_key.expanduser().resolve())], check=True, capture_output=True)
        proc = subprocess.run(base + ["--status-fd", "1", "--verify", str(directory / "CHECKSUM.asc"), str(directory / "CHECKSUM")], capture_output=True, check=True)
        wanted = args.signing_fingerprint.upper()
        signatures = [line.split() for line in proc.stdout.decode().splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
        if not any(wanted in (line[2], line[-1]) for line in signatures):
            raise MigrationError("checksum signature did not match the pinned official signing key")
    expected = checksum_from_manifest((directory / "CHECKSUM").read_text(), name)
    destination = directory / name
    if destination.exists():
        if sha256_file(destination) != expected:
            raise MigrationError("existing OS image differs from official signed checksum")
    else:
        part = destination.with_suffix(destination.suffix + ".partial")
        with urllib.request.urlopen(choice.url, timeout=120) as response, part.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                check_destination(state)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if sha256_file(part) != expected:
            raise MigrationError("OS image checksum mismatch")
        part.replace(destination)
    state["image"] = dict(image_metadata(destination), filename=name, source=choice.url, signer=wanted)
    if state["image"]["raw_bytes"] > state["target"]["size_bytes"]:
        raise MigrationError("expanded OS image exceeds SD capacity")
    state_save(state)
    print("Signed official image verified: " + name)


def cloud_seed(state, inventory, public_key):
    n = state["network"]
    users = [u for u in inventory["users"] if u["name"] != "root"]
    owner = next(u for u in users if u["name"] == state["user"])
    if owner["uid"] != owner["gid"]:
        raise MigrationError("non-matching owner UID/GID needs an explicit AlmaLinux account mapping")
    groups = sorted({g["name"] for g in inventory.get("groups", []) if owner["name"] in g.get("members", [])} | {"wheel"})
    if any(not re.fullmatch(r"[a-z_][a-z0-9_-]*", name) for name in groups + [owner["name"]]):
        raise MigrationError("source account or supplementary group name is unsupported")
    # Use JSON syntax inside YAML: keys/content are safely quoted without PyYAML.
    lines = ["#cloud-config", "hostname: " + json.dumps(inventory["hostname"].strip()), "manage_etc_hosts: true",
             "ssh_pwauth: false", "disable_root: true", "ssh_deletekeys: false", "users:",
             "  - name: " + json.dumps(owner["name"]), "    uid: " + str(owner["uid"]),
             "    primary_group: " + json.dumps(owner["name"]), "    groups: " + json.dumps(groups), "    lock_passwd: true",
             "    shell: /bin/bash", "    sudo: [\"ALL=(ALL) NOPASSWD:ALL\"]", "    ssh_authorized_keys:",
             "      - " + json.dumps(public_key.strip()), "packages: [podman, tar, gzip, python3, policycoreutils]",
             "package_update: true", "package_upgrade: false", "runcmd:",
             "  - [systemctl, set-default, multi-user.target]",
             "  - [systemctl, enable, --now, sshd]", "  - [touch, /var/lib/arcturus-migration-firstboot]", "write_files:"]
    for path, content in (("/etc/subuid", inventory["subuid"]), ("/etc/subgid", inventory["subgid"])):
        lines += ["  - path: " + json.dumps(path), "    permissions: '0644'", "    content: " + json.dumps(content)]
    # Image account evidence was checked in RAM. Rename only the known default
    # account before cloud-init's users_groups step; preserve the source UID/GID.
    lines += ["groups: " + json.dumps(groups)]
    commands = []
    if state.get("owner_image_group_rename"):
        commands.append("if getent group almalinux >/dev/null; then groupmod -n " + shlex.quote(owner["name"]) + " almalinux; fi")
    # users_groups creates supplementary groups before users. Reserve the
    # owner's exact primary GID first, otherwise a new supplementary group can
    # take GID 1000 and silently change every restored home file's ownership.
    commands.append("if ! getent group " + shlex.quote(owner["name"]) + " >/dev/null; then groupadd --gid " + str(owner["gid"]) + " " + shlex.quote(owner["name"]) + "; fi")
    commands.append("test \"$(getent group " + shlex.quote(owner["name"]) + " | cut -d: -f3)\" = " + str(owner["gid"]))
    if state.get("owner_image_rename"):
        commands.append("if getent passwd almalinux >/dev/null; then usermod -l " + shlex.quote(owner["name"]) + " -d " + shlex.quote(owner["home"]) + " -m almalinux; usermod -L " + shlex.quote(owner["name"]) + "; fi")
    lines += ["bootcmd:", "  - " + json.dumps(["bash", "-ec", "; ".join(commands)])]
    network = "version: 2\nethernets:\n  wired:\n    match:\n      macaddress: " + json.dumps(n["mac"]) + "\n    set-name: " + json.dumps(n["interface"]) + "\n    dhcp4: true\n    optional: false\n"
    return {"user-data": "\n".join(lines) + "\n", "meta-data": "instance-id: " + state["nonce"] + "\nlocal-hostname: " + inventory["hostname"].strip() + "\n", "network-config": network}


def flash(state, args):
    if args.confirm_cid != state["target"]["cid"]:
        raise MigrationError("--confirm-cid must exactly match the backed-up SD card CID")
    if state["phase"] != "backup-verified" or "image" not in state:
        raise MigrationError("require verified offline backup and signed OS image first")
    # A recoverable OS write alone is insufficient: the complete environment
    # restoration path must pass its acceptance review before erasing Debian.
    if state.get("restoration_acceptance") != "verified":
        raise MigrationError("environment restoration is not yet verified; flashing remains disabled")
    directory = Path(state["directory"])
    verify_backup(directory)  # Repeat just before erasing, do not trust cached success.
    for name, digest in state.get("restoration_artifacts", {}).items():
        if sha256_file(directory / name) != digest:
            raise MigrationError("restoration artifact changed before flash: " + name)
    peer = recovery_peer(state)
    guard(peer)
    image = state["image"]
    source = directory / image["filename"]
    if image_metadata(source) != {key: image[key] for key in ("compressed_sha256", "raw_sha256", "raw_bytes")}:
        raise MigrationError("image changed after signature verification")
    required = {"files-plan.json", "portable-files.tar.gz", "runtime-plan.json", "host-plan.json", "dependencies.json", "image-inspection.json"}
    if not required.issubset(state.get("restoration_artifacts", {})):
        raise MigrationError("restoration acceptance is missing pinned artifacts")
    # Pin the freshly verified source inventory, snapshots and volumes for
    # the later restore; it must consume the same artifacts accepted here.
    manifest = json.loads((directory / "manifest.json").read_text())
    for name, metadata in manifest["files"].items():
        if name == "inventory.json" or name.startswith(("image-", "volume-")):
            state["restoration_artifacts"][name] = metadata["sha256"]
    available = int(peer.run("awk '/MemAvailable:/ {print $2 * 1024}' /proc/meminfo").decode().strip())
    if available < image["raw_bytes"] + 2 * source.stat().st_size + 512 * 1024**2:
        raise MigrationError("insufficient recovery RAM for image and write buffers")
    peer.upload(source, "/run/alma.raw.xz", resume=True)
    inventory = json.loads((directory / "inventory.json").read_text())
    public = subprocess.run(["ssh-keygen", "-y", "-f", state["key"]], capture_output=True, check=True).stdout.decode()
    seed = cloud_seed(state, inventory, public)
    upload_seed(peer, seed)
    state["phase"] = "flash-started"
    state_save(state)
    t = state["target"]
    command = shlex.join(["/bin/bash", "-s", "--", state["nonce"], t["cid"], str(t["size_bytes"]), image["compressed_sha256"], image["raw_sha256"], str(image["raw_bytes"])])
    # Persist the observation identity before launching. A lost launch reply
    # must never cause a second writer; finish-flash only observes this job.
    state["flash_job"] = "/run/alma-flash-job-" + state["nonce"]
    state_save(state)
    try:
        print(peer.run(command, (ASSETS / "ram-flash-job.sh").read_bytes(), timeout=30).decode(), flush=True)
    except (MigrationError, subprocess.TimeoutExpired) as error:
        print("Launch reply unavailable; observing the existing job: " + str(error), flush=True)
    finish_flash(state)


def finish_flash(state, *, timeout=1800):
    if state.get("phase") != "flash-started" or state.get("flash_job") != "/run/alma-flash-job-" + state["nonce"]:
        raise MigrationError("no detached flash job to observe; refusing to launch a writer")
    peer = recovery_peer(state)
    job = shlex.quote(state["flash_job"])
    status = "if test -f " + job + "/exit; then printf 'EXIT '; cat " + job + "/exit; elif test -f " + job + "/pid; then kill -0 $(cat " + job + "/pid) 2>/dev/null && echo RUNNING || echo LOST; elif test -d " + job + "; then echo STARTING; else echo MISSING; fi"
    deadline = time.monotonic() + timeout
    last_notice = 0
    while time.monotonic() < deadline:
        check_destination(state)
        try:
            result = peer.run(status, timeout=20).decode().strip()
        except (MigrationError, subprocess.TimeoutExpired):
            result = "UNREACHABLE"
        if result == "EXIT 0":
            break
        if result.startswith("EXIT ") or result == "LOST":
            raise MigrationError("detached flash job failed (" + result + "); keep RAM powered and inspect its private log")
        if result not in ("RUNNING", "STARTING", "MISSING", "UNREACHABLE"):
            raise MigrationError("unexpected flash job status; keep RAM powered")
        if time.monotonic() - last_notice >= 45:
            print("Detached SD preparation/write status: " + result + ". Keep the Pi powered.", flush=True)
            last_notice = time.monotonic()
        time.sleep(5)
    else:
        raise MigrationError("flash observation timed out; the RAM job may still be running. Keep power on and use finish-flash; do not relaunch")
    peer.run("test -f /run/alma-flash-verified")
    guard(peer)
    boot_acceptance.record_boot_payload(state, peer.run("cat /run/alma-prepared-boot.checksums"), peer.run("cat /recovery/kernel-version"))
    state["phase"] = "flash-verified"
    state_save(state)
    print("SD image verified. RAM SSH remains up. Use boot-alma when ready.")


def upload_seed(peer, seed):
    # Each attempt rewrites all seed files before appending the host key locally.
    # Retrying after a lost SSH reply cannot append duplicate YAML host keys.
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for name in ("user-data", "meta-data", "network-config"):
            data = seed[name].encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(data))
    command = """set -e
test ! -L /run/alma-seed
mkdir -p /run/alma-seed
chmod 700 /run/alma-seed
for name in user-data meta-data network-config; do
    test ! -L /run/alma-seed/$name
done
tar -xf - -C /run/alma-seed
printf '\\nssh_keys:\\n  ed25519_private: |\\n' >> /run/alma-seed/user-data
sed 's/^/    /' /recovery/ssh_host_ed25519_key >> /run/alma-seed/user-data
printf '  ed25519_public: ' >> /run/alma-seed/user-data
cat /recovery/ssh_host_ed25519_key.pub >> /run/alma-seed/user-data
"""
    for attempt in range(3):
        try:
            peer.run(command, payload.getvalue(), timeout=30)
            return
        except (MigrationError, subprocess.TimeoutExpired):
            if attempt == 2:
                raise
            time.sleep(5)


def reboot_alma(state):
    if state["phase"] not in ("flash-verified", "fallback-verified"):
        raise MigrationError("a successful readback and cloud-init seed are required")
    peer = recovery_peer(state)
    def request_reboot(recovery):
        state_save(state)
        try:
            recovery.run("/bin/reboot-tryboot", timeout=20)
        except (MigrationError, subprocess.TimeoutExpired):
            pass
    boot_acceptance.reboot_alma(peer, state, guard=guard, request_reboot=request_reboot)
    print("One-shot AlmaLinux boot requested. Use wait-alma to verify Ethernet and first-boot completion.")


def retry_boot_alma(state):
    if state.get("phase") != "alma-boot-requested" or state.get("boot_promoted") is True:
        raise MigrationError("retry requires an unpromoted session whose Alma boot was already requested")
    # Reject stale or absent flash evidence before attempting recovery discovery.
    boot_acceptance._payload(state)
    peer = recovery_peer(state)
    def request_reboot(recovery):
        state_save(state)
        try:
            recovery.run("/bin/reboot-tryboot", timeout=20)
        except (MigrationError, subprocess.TimeoutExpired):
            pass
    boot_acceptance.retry_alma(peer, state, guard=guard, request_reboot=request_reboot)
    print("Verified SD boot payload in RAM; AlmaLinux tryboot retry requested. Session remains alma-boot-requested.")


def stage_image(state):
    if state.get("phase") != "backup-verified" or "image" not in state:
        raise MigrationError("stage the OS image only after verified offline backup")
    image = state["image"]
    path = Path(state["directory"]) / image["filename"]
    if image_metadata(path) != {key: image[key] for key in ("compressed_sha256", "raw_sha256", "raw_bytes")}:
        raise MigrationError("signed image changed before RAM transfer")
    peer = recovery_peer(state)
    guard(peer)
    peer.upload(path, "/run/alma.raw.xz", resume=True)
    digest = peer.run("sha256sum /run/alma.raw.xz").decode().split()[0]
    if digest != image["compressed_sha256"]:
        raise MigrationError("completed RAM image differs from signed source")
    print("Signed image transfer into RAM verified; the SD card remains unchanged.")


def resume_source(state):
    peer = Peer(state)
    os_release = peer.run("cat /etc/os-release").decode()
    if not re.search(r"^ID=debian$", os_release, re.M):
        raise MigrationError("resume-source is only for the original Debian OS")
    if state.get("source_running"):
        peer.run(shlex.join(["sudo", "-n", "docker", "start"] + state["source_running"]), timeout=300)
    if state.get("source_runtime_units"):
        peer.run(shlex.join(["sudo", "-n", "systemctl", "start"] + state["source_runtime_units"]), timeout=300)
    state["phase"] = "source-resumed"
    state_save(state)
    print("Original source container lifecycle resumed. A fresh snapshot session is required before migration.")


_CLOUDFLARED_TOKEN_FILE = "/etc/cloudflared/token"


def _verify_cloudflared_token_archive(rootfs_tar, token_path):
    """Require the reviewed token and its directory to be safe in the backup."""
    if token_path != _CLOUDFLARED_TOKEN_FILE:
        raise MigrationError("cloudflared token path is outside the reviewed portable allowlist")
    wanted = {"etc", "etc/cloudflared", "etc/cloudflared/token"}
    found = {}
    try:
        with tarfile.open(rootfs_tar, "r|gz") as archive:
            for member in archive:
                name = member.name.removeprefix("./").rstrip("/")
                if name not in wanted:
                    continue
                if name in found:
                    raise MigrationError("cloudflared token archive contains duplicate entries")
                found[name] = member
                if name == "etc/cloudflared/token":
                    if (not member.isfile() or member.uid != 0 or member.gid != 0
                            or member.size <= 0 or member.mode & 0o077):
                        raise MigrationError("cloudflared token must be a nonempty protected root-owned regular file")
                    if any("acl" in key.lower() for key in member.pax_headers):
                        raise MigrationError("cloudflared token with extended ACL metadata requires manual review")
                    stream = archive.extractfile(member)
                    if stream is None or not stream.read(1):
                        raise MigrationError("cloudflared token must be nonempty")
                elif (not member.isdir() or member.uid != 0 or member.gid != 0
                      or member.mode & 0o022):
                    raise MigrationError("cloudflared token ancestors must be protected root-owned directories")
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise MigrationError("could not verify cloudflared token from the offline rootfs backup") from exc
    if set(found) != wanted:
        raise MigrationError("cloudflared token or its directory is absent from the offline rootfs backup")


def prepare_restore(state):
    if state.get("phase") != "backup-verified":
        raise MigrationError("prepare restoration only after complete offline backup verification")
    directory = Path(state["directory"])
    # The complete raw/gzip verification already passed in backup. Recheck the
    # pinned compressed inputs being consumed here; flash repeats full backup
    # verification immediately before the destructive operation.
    check_backup_inputs(state, ("rootfs.tar.gz", "inventory.json", "runtime-plan.json"))
    inventory = json.loads((directory / "inventory.json").read_text())
    plan = json.loads((directory / "runtime-plan.json").read_text())
    host_plan = json.loads((directory / "host-plan.json").read_text())
    expected_host_plan = host_adapter().build_plan(
        inventory, inventory["dependencies"], verify_target=False)
    if not expected_host_plan["ready"] or expected_host_plan != host_plan:
        raise MigrationError("host restoration plan differs from the reviewed source units")
    owner = next(u for u in inventory["users"] if u["name"] == state["user"])
    selected = [owner["home"], "/root", "/opt", "/srv", "/usr/local"]
    runtime_units = set(state.get("source_runtime_units", []))
    host_units = []
    for name, unit in inventory.get("custom_units", {}).items():
        if name not in runtime_units:
            selected.append("/etc/systemd/system/" + name)
            if unit.get("active") == "active\n":
                host_units.append(name)
    token_paths = {unit.get("facts", {}).get("token_file") for unit in host_plan.get("units", [])
                   if unit.get("name") == "cloudflared.service"}
    token_paths.discard(None)
    if len(token_paths) > 1:
        raise MigrationError("cloudflared units reference different token files")
    if token_paths:
        token_path = next(iter(token_paths))
        _verify_cloudflared_token_archive(directory / "rootfs.tar.gz", token_path)
        selected.append(token_path)
    bundle = directory / "portable-files.tar.gz"
    report = build_bundle(directory / "rootfs.tar.gz", bundle, selected)
    durable_json(directory / "files-plan.json", report)
    dependencies = inventory["dependencies"]
    descriptor = dependencies.get("cloudflared")
    if descriptor:
        if "statically linked" not in descriptor.get("description", ""):
            raise MigrationError("cloudflared requires portable binary dependencies before restoration")
        found = False
        with tarfile.open(directory / "rootfs.tar.gz", "r|gz") as archive:
            for member in archive:
                if member.name.removeprefix("./") != "usr/bin/cloudflared":
                    continue
                if found or not member.isfile():
                    raise MigrationError("cloudflared backup is not one regular executable")
                found = True
                source = archive.extractfile(member)
                target = directory / "cloudflared"
                with target.open("xb") as out:
                    shutil.copyfileobj(source, out, 1024 * 1024)
                    out.flush(); os.fsync(out.fileno())
                if sha256_file(target) != descriptor["sha256"]:
                    raise MigrationError("cloudflared differs from its source dependency pin")
        if not found:
            raise MigrationError("cloudflared executable was not found in the full backup")
    # Full rootfs remains the fallback for packages, special configurations and
    # services whose distro-specific dependencies need reconstruction.
    acceptance = {"schemaVersion": 1, "files_plan_sha256": sha256_file(directory / "files-plan.json"),
                  "runtime_plan_sha256": sha256_file(directory / "runtime-plan.json"),
                  "host_units": host_units, "owner": owner, "status": "prepared"}
    durable_json(directory / "restoration-plan.json", acceptance)
    state["restoration_plan"] = acceptance
    state["restoration_acceptance"] = "prepared"
    state_save(state)
    print("Portable filesystem bundle and runtime restore plans prepared; host dependency verification remains required.")


def inspect_image(state):
    if state["phase"] != "backup-verified" or "image" not in state:
        raise MigrationError("image inspection requires verified offline backup and signed image")
    directory = Path(state["directory"])
    image = state["image"]
    path = directory / image["filename"]
    if image_metadata(path) != {key: image[key] for key in ("compressed_sha256", "raw_sha256", "raw_bytes")}:
        raise MigrationError("signed image changed before inspection")
    peer = recovery_peer(state)
    guard(peer)
    peer.upload(path, "/run/alma.raw.xz")
    command = ["bash", "-s", "--", state["nonce"], state["target"]["cid"], str(state["target"]["size_bytes"]),
               image["compressed_sha256"], image["raw_sha256"], str(image["raw_bytes"])]
    report = json.loads(peer.run(shlex.join(command), (ASSETS / "inspect-alma.sh").read_bytes(), timeout=900))
    if report.get("nonce") != state["nonce"] or report.get("target") != state["target"] or report.get("image") != {key: image[key] for key in ("compressed_sha256", "raw_sha256", "raw_bytes")} or not report.get("is_almalinux"):
        raise MigrationError("Alma image inspection identity differs from this session")
    for name in ("python3", "rpm", "cloud-init", "systemctl", "sshd", "useradd", "usermod", "groupmod"):
        if not report.get("tools", {}).get(name):
            raise MigrationError("Alma image lacks a headless bootstrap dependency: " + name)
    durable_json(directory / "image-inspection.json", report)
    state["image_inspection_sha256"] = sha256_file(directory / "image-inspection.json")
    state_save(state)
    print("Signed Alma image inspected in read-only RAM; account, package and kernel evidence saved. GNU tar and Podman are installed and checked during first boot/restoration.")


def verify_restoration(state):
    directory = Path(state["directory"])
    if state.get("phase") != "backup-verified":
        raise MigrationError("restoration acceptance requires verified offline backup")
    check_backup_inputs(state)
    inventory = json.loads((directory / "inventory.json").read_text())
    files = json.loads((directory / "files-plan.json").read_text())
    scan = validate_bundle(directory / files["bundle"], files["selected_paths"])
    if sha256_file(directory / files["bundle"]) != files["bundle_sha256"]:
        raise MigrationError("portable filesystem bundle changed")
    owner = next(u for u in inventory["users"] if u["name"] == state["user"])
    if set(scan["numeric_ids"]["uids"]) - {0, owner["uid"]} or set(scan["numeric_ids"]["gids"]) - {0, owner["gid"]}:
        raise MigrationError("portable data needs additional account ownership mapping")
    adapter = host_adapter()
    host_plan = adapter.build_plan(inventory, inventory["dependencies"], verify_target=False)
    if not host_plan["ready"] or host_plan != json.loads((directory / "host-plan.json").read_text()):
        raise MigrationError("host restoration plan is incomplete or changed")
    plan = json.loads((directory / "runtime-plan.json").read_text())
    if build_plan(inventory, state["snapshot_tags"]) != plan:
        raise MigrationError("runtime restoration plan changed after source snapshot")
    for volume in plan["volumes"]:
        _validate_volume_archive(directory, volume["archive"])
    inspected = directory / "image-inspection.json"
    if sha256_file(inspected) != state.get("image_inspection_sha256"):
        raise MigrationError("read-only image inspection has not passed")
    image = json.loads(inspected.read_text())
    expected_image = {key: state["image"][key] for key in ("compressed_sha256", "raw_sha256", "raw_bytes")}
    if image.get("image") != expected_image or image.get("target") != state["target"] or image.get("nonce") != state["nonce"]:
        raise MigrationError("image inspection does not describe this signed image and card session")
    user_collision = [u for u in image["users"] if u["uid"] == owner["uid"] and u["name"] != owner["name"]]
    group_collision = [g for g in image["groups"] if g["gid"] == owner["gid"] and g["name"] != owner["name"]]
    if any(u["name"] != "almalinux" for u in user_collision) or any(g["name"] != "almalinux" for g in group_collision):
        raise MigrationError("Alma image has an unsupported owner UID/GID collision")
    if any(u["name"] == owner["name"] and u["uid"] != owner["uid"] for u in image["users"]):
        raise MigrationError("Alma image already has the owner with a different UID")
    if any(g["name"] == owner["name"] and g["gid"] != owner["gid"] for g in image["groups"]):
        raise MigrationError("Alma image already has the owner group with a different GID")
    state["owner_image_rename"] = bool(user_collision)
    state["owner_image_group_rename"] = bool(group_collision)
    hashes = {name: sha256_file(directory / name) for name in ("files-plan.json", "portable-files.tar.gz", "runtime-plan.json", "host-plan.json", "dependencies.json", "image-inspection.json")}
    manifest = json.loads((directory / "manifest.json").read_text())
    hashes.update({name: metadata["sha256"] for name, metadata in manifest["files"].items()
                   if name == "inventory.json" or name.startswith(("image-", "volume-"))})
    if (directory / "cloudflared").exists():
        hashes["cloudflared"] = sha256_file(directory / "cloudflared")
    state["restoration_artifacts"] = hashes
    state["restoration_acceptance"] = "verified"
    state_save(state)
    print("Restoration preflight verified: portable metadata, ownership mapping, image snapshots, local volumes and host dependency plans. Live Alma service health remains a post-boot gate.")


def check_backup_inputs(state, names=None):
    """Check unchanged compressed artifacts after the initial full verification."""
    check_destination(state)
    directory = Path(state["directory"])
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("offline") is not True or manifest.get("target") != state["target"]:
        raise MigrationError("backup manifest does not describe the pinned offline card")
    for name in names if names is not None else manifest["files"]:
        entry = manifest["files"][name]
        path = directory / name
        if path.stat().st_size != entry["bytes"] or sha256_file(path) != entry["sha256"]:
            raise MigrationError("verified backup input changed: " + name)
        check_destination(state)


def alma_peer(state):
    def check(host):
        temporary = dict(state, host=host)
        peer = Peer(temporary)
        program = """import json, pathlib, pwd, subprocess
release=pathlib.Path('/etc/os-release').read_text()
owner=pwd.getpwnam(%s)
print(json.dumps({'os':release, 'cid':pathlib.Path('/sys/block/mmcblk0/device/cid').read_text().strip(), 'instance_id':pathlib.Path('/var/lib/cloud/data/instance-id').read_text().strip(), 'owner_uid':owner.pw_uid, 'owner_gid':owner.pw_gid, 'model':pathlib.Path('/proc/device-tree/model').read_text().strip('\\x00'), 'addresses':json.loads(subprocess.check_output(['ip','-j','address'])), 'routes':json.loads(subprocess.check_output(['ip','-j','route']))}))
""" % repr(state["user"])
        report = json.loads(peer.run("sudo -n /usr/bin/python3 -", program.encode(), timeout=15))
        if not re.search(r'^ID=[\"\']?almalinux[\"\']?$', report["os"], re.M) or report["cid"] != state["target"]["cid"] or report["instance_id"] != state["nonce"]:
            raise MigrationError("normal SSH peer is not this session's new AlmaLinux installation")
        inventory = json.loads((Path(state["directory"]) / "inventory.json").read_text())
        owner = next(u for u in inventory["users"] if u["name"] == state["user"])
        if (report["owner_uid"], report["owner_gid"]) != (owner["uid"], owner["gid"]):
            raise MigrationError("Alma bootstrap owner does not match preserved numeric identity")
        if wired_identity(report, host)["mac"] != state["network"]["mac"]:
            raise MigrationError("Alma SSH is not over the pinned Ethernet interface")
        state["host"] = host
        state_save(state)
        return Peer(state)
    try:
        return check(state["host"])
    except (MigrationError, OSError, ValueError, subprocess.TimeoutExpired):
        pass
    network = ipaddress.ip_network(f'{state["network"]["address"]}/{state["network"]["prefix"]}', strict=False)
    if network.num_addresses > 1024:
        raise MigrationError("Alma DHCP rediscovery is limited to /22 or narrower LANs")
    def open_host(host):
        try:
            with socket.create_connection((str(host), 22), timeout=.3):
                return str(host)
        except OSError:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        hosts = [h for h in pool.map(open_host, network.hosts()) if h]
    for host in hosts:
        try:
            return check(host)
        except (MigrationError, OSError, ValueError, subprocess.TimeoutExpired):
            continue
    raise MigrationError("no keyed Alma peer with the session nonce and wired identity is reachable")


def verified_cloud_init_status(peer, state):
    """Accept only completed cloud-init and the bounded existing-group warning."""
    try:
        inventory = json.loads((Path(state["directory"]) / "inventory.json").read_text())
    except (KeyError, OSError, ValueError, TypeError):
        raise MigrationError("verified source inventory is required for cloud-init acceptance") from None
    owners = [user for user in inventory.get("users", []) if user.get("name") == state["user"]]
    if len(owners) != 1:
        raise MigrationError("source inventory does not identify one migration owner")
    owner_name = owners[0]["name"]
    groups = sorted({group["name"] for group in inventory.get("groups", [])
                     if owner_name in group.get("members", [])} | {"wheel"})
    if (not re.fullmatch(r"[a-z_][a-z0-9_-]*", owner_name)
            or any(not isinstance(name, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]*", name)
                   for name in groups)):
        raise MigrationError("source owner or supplementary group name is unsupported")
    program = f'''import grp, json, pwd, re, subprocess
owner_name = {owner_name!r}
expected_groups = {groups!r}
def fail():
    raise RuntimeError("cloud-init status acceptance failed")
try:
    result = subprocess.run(
        ["/usr/bin/cloud-init", "status", "--wait", "--format", "json"],
        capture_output=True, text=True, timeout=1800)
except Exception:
    fail()
if type(result.returncode) is not int or result.returncode not in (0, 2):
    fail()
try:
    report = json.loads(result.stdout)
except (TypeError, ValueError):
    fail()
if not isinstance(report, dict) or report.get("status") != "done":
    fail()
def require_no_errors(container):
    errors = container.get("errors")
    if not isinstance(errors, list) or errors:
        fail()
known_messages = set()
def collect_warnings(container):
    recoverable = container.get("recoverable_errors")
    if not isinstance(recoverable, dict):
        fail()
    container_messages = set()
    for severity, messages in recoverable.items():
        if severity != "WARNING" or not isinstance(messages, list):
            fail()
        for message in messages:
            if not isinstance(message, str):
                fail()
            match = re.fullmatch(r"Skipping creation of existing group '([a-z_][a-z0-9_-]*)'", message)
            if not match or match.group(1) not in expected_groups:
                fail()
            known_messages.add(message)
            container_messages.add(message)
    return container_messages
require_no_errors(report)
aggregate_messages = collect_warnings(report)
for stage_name in ("init", "init-local", "modules-config", "modules-final"):
    stage = report.get(stage_name)
    if not isinstance(stage, dict):
        fail()
    require_no_errors(stage)
    stage_messages = collect_warnings(stage)
    if not stage_messages.issubset(aggregate_messages):
        fail()
if (result.returncode == 2 and not known_messages) or (result.returncode == 0 and known_messages):
    fail()
try:
    owner = pwd.getpwnam(owner_name)
    for name in expected_groups:
        group = grp.getgrnam(name)
        if owner_name not in group.gr_mem and group.gr_gid != owner.pw_gid:
            fail()
except KeyError:
    fail()
groups_with_warnings = sorted({{
    re.fullmatch(r"Skipping creation of existing group '([a-z_][a-z0-9_-]*)'", message).group(1)
    for message in known_messages
}})
print(json.dumps({{
    "status": "done",
    "returncode": result.returncode,
    "known_warning_count": len(known_messages),
    "known_warning_groups": groups_with_warnings,
}}, sort_keys=True))
'''
    output = peer.run("sudo -n /usr/bin/python3 -", program.encode(), timeout=1800)
    try:
        acceptance = json.loads(output)
    except (TypeError, ValueError):
        raise MigrationError("invalid cloud-init acceptance response") from None
    if (not isinstance(acceptance, dict)
            or set(acceptance) != {"status", "returncode", "known_warning_count", "known_warning_groups"}
            or acceptance.get("status") != "done"
            or acceptance.get("returncode") not in (0, 2)
            or type(acceptance.get("known_warning_count")) is not int
            or acceptance["known_warning_count"] < 0
            or not isinstance(acceptance.get("known_warning_groups"), list)
            or any(not isinstance(name, str) for name in acceptance["known_warning_groups"])
            or acceptance["known_warning_groups"] != sorted(set(acceptance["known_warning_groups"]))
            or acceptance["known_warning_count"] != len(acceptance["known_warning_groups"])
            or (acceptance["returncode"] == 2 and not acceptance["known_warning_count"])
            or (acceptance["returncode"] == 0 and acceptance["known_warning_count"])
            or any(name not in groups for name in acceptance["known_warning_groups"])):
        raise MigrationError("invalid cloud-init acceptance response")
    return acceptance


def wait_alma(state):
    for _ in range(24):
        try:
            peer = alma_peer(state)
        except (MigrationError, OSError, ValueError, subprocess.TimeoutExpired):
            time.sleep(5)
            continue
        # Once the pinned installation is reachable, a bootstrap failure needs
        # diagnosis. Repeated discovery must not conceal its actual error.
        cloud_init_acceptance = verified_cloud_init_status(peer, state)
        peer.run("test -f /var/lib/arcturus-migration-firstboot && sudo -n true")
        boot_acceptance.verify_boot_payload(peer, state, require_rescue_default=False if state.get("boot_promoted") else True)
        state["cloud_init_acceptance"] = cloud_init_acceptance
        state["phase"] = "alma-running"
        state_save(state)
        print("AlmaLinux headless SSH, Ethernet, account mapping and preserved recovery boot payload verified.")
        return peer
    raise MigrationError("new Alma bootstrap has not completed; default on-card recovery remains selected")


def upload_checked(peer, source, destination, mode="600"):
    check_destination(peer.state)
    peer.upload(source, destination, sudo=not peer.recovery)
    digest = peer.run(("" if peer.recovery else "sudo -n ") + "sha256sum " + shlex.quote(destination)).decode().split()[0]
    if digest != sha256_file(source):
        raise MigrationError("uploaded artifact checksum mismatch: " + source.name)
    peer.run(("" if peer.recovery else "sudo -n ") + "chmod " + mode + " " + shlex.quote(destination))
    check_destination(peer.state)


def _cloudflared_atomic_program(action, *, destination=CLOUDFLARED_PATH,
                                migration_root=ARCTURUS_MIGRATION_ROOT,
                                temporary=None, digest=None, source_name=None):
    """Build a fixed-purpose root helper; arguments are JSON literals, never shell text."""
    return f'''import hashlib, json, os, pathlib, stat, time
destination = pathlib.Path({destination!r})
migration_root = pathlib.Path({migration_root!r})
temporary = pathlib.Path({temporary!r}) if {temporary is not None!r} else None
expected = {digest!r}
source_name = {source_name!r}
provenance = migration_root / ('cloudflared-install-' + str(source_name) + '.json') if source_name is not None else None
def fail(message):
    raise RuntimeError(message)
def check_chain(path):
    current = pathlib.Path('/')
    for part in path.parts[1:]:
        current = current / part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            fail('unsafe protected directory chain')
def regular_hash(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
        fail('cloudflared temporary or destination is not root-owned regular file')
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            value.update(block)
    return value.hexdigest()
check_chain(destination.parent)
if {action!r} == 'prepare':
    check_chain(migration_root)
    if stat.S_IMODE(migration_root.lstat().st_mode) != 0o700 or temporary.parent != destination.parent:
        fail('cloudflared provenance directory or temporary path is unsafe')
    try:
        old = destination.lstat()
    except FileNotFoundError:
        old = None
    original = {{'path': str(destination), 'observed_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'state': 'absent'}}
    if old is not None:
        if stat.S_ISLNK(old.st_mode):
            if old.st_uid != 0 or os.readlink(destination) != '/usr/bin/cloudflared':
                fail('cloudflared destination has an unexpected symlink')
            original.update(state='known-source-symlink', target='/usr/bin/cloudflared', uid=old.st_uid)
        elif stat.S_ISREG(old.st_mode) and old.st_uid == 0:
            original.update(state='root-owned-regular', sha256=regular_hash(destination), mode=stat.S_IMODE(old.st_mode))
        else:
            fail('cloudflared destination is not an accepted file')
    if provenance.exists() or provenance.is_symlink():
        fail('cloudflared provenance record already exists')
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.close(fd)
    try:
        fd = os.open(str(provenance), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(original, output, sort_keys=True)
            output.write('\\n')
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        try:
            provenance.unlink()
        except FileNotFoundError:
            pass
        raise
    print(json.dumps({{'status': 'prepared', 'source_state': original['state']}}))
elif {action!r} == 'commit':
    if source_name is None:
        fail('cloudflared provenance identity is missing')
    info = temporary.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        fail('uploaded cloudflared temporary has unsafe ownership or mode')
    if regular_hash(temporary) != expected:
        fail('uploaded cloudflared temporary checksum mismatch')
    current = destination.lstat() if destination.exists() or destination.is_symlink() else None
    try:
        with provenance.open(encoding='utf-8') as stream:
            original = json.load(stream)
    except Exception:
        fail('cloudflared provenance record is unreadable')
    if original.get('state') == 'absent':
        if current is not None:
            fail('cloudflared destination changed during install')
    elif original.get('state') == 'known-source-symlink':
        if current is None or not stat.S_ISLNK(current.st_mode) or current.st_uid != 0 or os.readlink(destination) != '/usr/bin/cloudflared':
            fail('cloudflared destination changed during install')
    elif original.get('state') == 'root-owned-regular':
        if current is None or not stat.S_ISREG(current.st_mode) or regular_hash(destination) != original.get('sha256'):
            fail('cloudflared destination changed during install')
    else:
        fail('cloudflared provenance state is invalid')
    if current is not None:
        if stat.S_ISLNK(current.st_mode):
            if current.st_uid != 0 or os.readlink(destination) != '/usr/bin/cloudflared':
                fail('cloudflared destination changed during install')
        elif not (stat.S_ISREG(current.st_mode) and current.st_uid == 0):
            fail('cloudflared destination changed to an unsafe object')
    os.chmod(temporary, 0o755, follow_symlinks=False)
    fd = os.open(str(temporary), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o755:
            fail('cloudflared temporary changed before durable rename')
        value = hashlib.sha256()
        with os.fdopen(os.dup(fd), 'rb') as stream:
            for block in iter(lambda: stream.read(1024*1024), b''):
                value.update(block)
        if value.hexdigest() != expected:
            fail('cloudflared temporary changed before durable rename')
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, destination)
    directory_fd = os.open(str(destination.parent), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    if regular_hash(destination) != expected or stat.S_IMODE(destination.lstat().st_mode) != 0o755:
        fail('installed cloudflared did not pass final verification')
    print(json.dumps({{'status': 'installed', 'sha256': expected}}))
elif {action!r} == 'cleanup':
    if temporary is not None and (temporary.exists() or temporary.is_symlink()):
        info = temporary.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_uid == 0:
            temporary.unlink()
        else:
            fail('refusing to remove unsafe cloudflared temporary')
    print(json.dumps({{'status': 'cleaned'}}))
else:
    fail('unsupported cloudflared install action')
'''


def install_cloudflared_atomic(peer, state, source):
    """Install the pinned cloudflared binary without following the known source symlink."""
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise MigrationError('cloudflared source must be a regular local file')
    digest = sha256_file(source)
    token = re.sub(r'[^a-zA-Z0-9_-]', '', state['nonce'])
    if not token:
        raise MigrationError('cloudflared install requires the pinned session nonce')
    attempt = uuid.uuid4().hex
    temporary = CLOUDFLARED_PATH + '.arcturus-' + token + '-' + attempt + '.tmp'
    provenance_name = hashlib.sha256((token + '\0' + digest + '\0' + attempt).encode()).hexdigest()[:20]
    remote = 'sudo -n /usr/bin/python3 -'
    check_destination(peer.state)
    peer.run(remote, _cloudflared_atomic_program(
        'prepare', temporary=temporary, source_name=provenance_name).encode())
    try:
        peer.upload(source, temporary, sudo=True)
        check_destination(peer.state)
        peer.run(remote, _cloudflared_atomic_program(
            'commit', temporary=temporary, digest=digest,
            source_name=provenance_name).encode())
        peer.run('sudo -n restorecon -F ' + shlex.quote(CLOUDFLARED_PATH))
    except BaseException:
        try:
            peer.run(remote, _cloudflared_atomic_program('cleanup', temporary=temporary).encode())
        except Exception:
            pass
        raise
    check_destination(peer.state)


def _runtime_resume_inspection_program(plan, host_plan, staged_hashes, stage):
    """Read-only resume gate; prints only counts and a success marker."""
    expected_containers = []
    for container in plan["containers"]:
        env = runtime_acceptance._env_file_text(
            [(item["key"], item["value"]) for item in container["environment"]]).encode()
        expected_containers.append({
            "name": container["name"], "image": container["image"],
            "running": bool(container["desired_running"]), "unit": container["unit"],
            "unit_enabled": "enabled" if container["desired_running"] else "disabled",
            "unit_sha256": hashlib.sha256(runtime_acceptance._service(container).encode()).hexdigest(),
            "env_path": RUNTIME_ENV_DIR + "/" + container["name"] + ".env",
            "env_sha256": hashlib.sha256(env).hexdigest(),
        })
    expected = {
        "containers": expected_containers,
        "images": list(plan["image_exports"].keys()),
        "networks": [item["name"] for item in plan["networks"]],
        "volumes": [item["name"] for item in plan["volumes"]],
        "stage_hashes": staged_hashes,
    }
    return f'''import hashlib, json, os, pathlib, stat, subprocess
expected = json.loads({json.dumps(json.dumps(expected))})
stage = {stage!r}
def fail():
    raise RuntimeError('restored runtime state does not match pinned plans')
def run(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except Exception:
        fail()
    if result.returncode:
        fail()
    return result.stdout.strip()
def active_state(name):
    try:
        return subprocess.run(['/usr/bin/systemctl', 'is-active', name], capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        fail()
def is_enabled(name):
    try:
        result = subprocess.run(['/usr/bin/systemctl', 'is-enabled', name], capture_output=True, text=True, timeout=15)
    except Exception:
        fail()
    return result.stdout.strip()
root = pathlib.Path(stage)
current = pathlib.Path('/')
for part in root.parts[1:]:
    current = current / part
    try:
        info = current.lstat()
    except OSError:
        fail()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        fail()
for name, digest in expected['stage_hashes'].items():
    path = root / name
    try:
        info = path.lstat()
    except OSError:
        fail()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        fail()
    h = hashlib.sha256(path.read_bytes()).hexdigest()
    if h != digest:
        fail()
for item in expected['containers']:
    if run(['/usr/bin/podman', 'inspect', '--format', '{{{{.ImageName}}}}', item['name']]) != item['image']:
        fail()
    running = run(['/usr/bin/podman', 'inspect', '--format', '{{{{.State.Running}}}}', item['name']]) == 'true'
    if running != item['running']:
        fail()
    unit_path = pathlib.Path({RUNTIME_UNIT_DIR!r}) / item['unit']
    env_path = pathlib.Path(item['env_path'])
    for parent in (unit_path.parent, env_path.parent):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            fail()
    for path, digest in ((unit_path, item['unit_sha256']), (env_path, item['env_sha256'])):
        try:
            info = path.lstat()
        except OSError:
            fail()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            fail()
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            fail()
    activity = active_state(item['unit'])
    if activity not in ('active', 'inactive') or (activity == 'active') != item['running']:
        fail()
    if is_enabled(item['unit']) != item['unit_enabled']:
        fail()
for image in expected['images']:
    run(['/usr/bin/podman', 'image', 'exists', image])
for name in expected['networks']:
    run(['/usr/bin/podman', 'network', 'exists', name])
for name in expected['volumes']:
    run(['/usr/bin/podman', 'volume', 'exists', name])
print(json.dumps({{'status':'verified','containers':len(expected['containers']), 'networks':len(expected['networks']), 'volumes':len(expected['volumes'])}}))
'''


def verify_resume_runtime(peer, state, plan, host_plan, stage):
    directory = Path(state["directory"])
    hashes = {
        "runtime-plan.json": sha256_file(directory / "runtime-plan.json"),
        "host-plan.json": sha256_file(directory / "host-plan.json"),
    }
    program = _runtime_resume_inspection_program(plan, host_plan, hashes, stage)
    response = json.loads(peer.run("sudo -n /usr/bin/python3 -", program.encode(), timeout=90))
    if response.get("status") != "verified":
        raise MigrationError("restored runtime preflight did not verify")
    return response


def upload_resume_host_adapter(peer, source, stage, state):
    """Replace only the regular staged adapter through a protected sibling temp."""
    source = Path(source)
    digest = sha256_file(source)
    directory = stage + "/pi-migration"
    destination = directory + "/restore-host.py"
    temporary = directory + "/.restore-host.py." + state["nonce"] + ".tmp"
    preflight = f'''import os, pathlib, stat
directory=pathlib.Path({directory!r})
destination=pathlib.Path({destination!r})
temporary=pathlib.Path({temporary!r})
info=directory.lstat()
if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
    raise RuntimeError('unsafe staged host adapter directory')
if destination.exists() or destination.is_symlink():
    old=destination.lstat()
    if not stat.S_ISREG(old.st_mode) or old.st_uid != 0:
        raise RuntimeError('staged host adapter destination is unsafe')
fd=os.open(str(temporary), os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0), 0o600)
os.close(fd)
print('prepared')
'''
    commit = f'''import hashlib, os, pathlib, stat
destination=pathlib.Path({destination!r})
temporary=pathlib.Path({temporary!r})
expected={digest!r}
info=temporary.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
    raise RuntimeError('uploaded staged adapter temporary is unsafe')
if hashlib.sha256(temporary.read_bytes()).hexdigest() != expected:
    raise RuntimeError('staged adapter checksum mismatch')
if destination.exists() or destination.is_symlink():
    old=destination.lstat()
    if not stat.S_ISREG(old.st_mode) or old.st_uid != 0:
        raise RuntimeError('staged host adapter destination changed')
os.replace(temporary,destination)
if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
    raise RuntimeError('staged host adapter final checksum mismatch')
print('installed')
'''
    command = "sudo -n /usr/bin/python3 -"
    peer.run(command, preflight.encode())
    try:
        peer.upload(source, temporary, sudo=True)
        peer.run(command, commit.encode())
    except BaseException:
        cleanup = f'''import pathlib, stat
p=pathlib.Path({temporary!r})
try:
 i=p.lstat()
 if stat.S_ISREG(i.st_mode) and i.st_uid==0: p.unlink()
except FileNotFoundError: pass
'''
        try:
            peer.run(command, cleanup.encode())
        except Exception:
            pass
        raise


def _resume_restoration_inputs(state):
    if state.get("phase") != "environment-restoring":
        raise MigrationError("finish-restore is allowed only while environment restoration is incomplete")
    if state.get("restoration_acceptance") != "verified":
        raise MigrationError("verified restoration acceptance is required")
    directory = Path(state["directory"])
    required = {"files-plan.json", "portable-files.tar.gz", "runtime-plan.json", "host-plan.json"}
    if not required.issubset(state.get("restoration_artifacts", {})):
        raise MigrationError("restoration acceptance is missing pinned plans or files bundle")
    for name, digest in state.get("restoration_artifacts", {}).items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
            raise MigrationError("restoration artifact changed: " + name)
    files_result_path = directory / "files-restoration-result.json"
    if files_result_path.is_symlink() or not files_result_path.is_file():
        raise MigrationError("deployed files restoration result is required")
    files_result = json.loads(files_result_path.read_text())
    files_plan = json.loads((directory / "files-plan.json").read_text())
    if (files_result.get("status") != "deployed"
            or files_result.get("bootstrap_key") != "preserved"
            or files_result.get("selected_paths") != files_plan.get("selected_paths")):
        raise MigrationError("files restoration result does not match the frozen plan")
    host_result_path = directory / "host-restoration-result.json"
    attempt_path = directory / "host-restoration-attempt.json"
    if (host_result_path.exists() or host_result_path.is_symlink()
            or attempt_path.exists() or attempt_path.is_symlink()):
        raise MigrationError("host restoration result already exists; refusing to repeat host apply")
    plan = json.loads((directory / "runtime-plan.json").read_text())
    host_plan = json.loads((directory / "host-plan.json").read_text())
    if (sha256_file(directory / "runtime-plan.json") != state["restoration_artifacts"].get("runtime-plan.json")
            or sha256_file(directory / "host-plan.json") != state["restoration_artifacts"].get("host-plan.json")):
        raise MigrationError("runtime or host plan changed after restoration acceptance")
    return directory, plan, host_plan


def finish_restore(state):
    directory, plan, host_plan = _resume_restoration_inputs(state)
    peer = alma_peer(state)
    cloud_init_acceptance = verified_cloud_init_status(peer, state)
    peer.run("test -f /var/lib/arcturus-migration-firstboot && sudo -n true")
    boot_acceptance.verify_boot_payload(peer, state)
    stage = "/var/lib/arcturus-migration/session-" + state["nonce"]
    verify_resume_runtime(peer, state, plan, host_plan, stage)
    state["cloud_init_acceptance"] = cloud_init_acceptance
    state_save(state)
    if (directory / "cloudflared").is_file():
        install_cloudflared_atomic(peer, state, directory / "cloudflared")
    # Upload only the current host adapter after verifying the existing pinned stage.
    upload_resume_host_adapter(peer, ASSETS / "restore-host.py", stage, state)
    durable_json(directory / "host-restoration-attempt.json", {
        "status": "started", "stage": stage,
        "adapter_sha256": sha256_file(ASSETS / "restore-host.py"),
        "host_plan_sha256": sha256_file(directory / "host-plan.json"),
    })
    host_result = json.loads(peer.run(shlex.join([
        "sudo", "-n", "/usr/bin/python3", stage + "/pi-migration/restore-host.py",
        "apply", stage + "/host-plan.json"]), timeout=1800))
    durable_json(directory / "host-restoration-result.json", host_result)
    if host_result.get("status") != "restored":
        raise MigrationError("host restoration did not report successful completion")
    state["phase"] = "environment-restored"
    state_save(state)
    configure_exposure(state, peer, plan, host_plan)
    verify_environment(state)
def restore_environment(state):
    if state.get("restoration_acceptance") != "verified":
        raise MigrationError("verified restoration preflight is required")
    peer = alma_peer(state)
    cloud_init_acceptance = verified_cloud_init_status(peer, state)
    peer.run("test -f /var/lib/arcturus-migration-firstboot && sudo -n true")
    boot_acceptance.verify_boot_payload(peer, state)
    state["cloud_init_acceptance"] = cloud_init_acceptance
    state_save(state)
    directory = Path(state["directory"])
    for name, digest in state["restoration_artifacts"].items():
        if sha256_file(directory / name) != digest:
            raise MigrationError("restoration artifact changed: " + name)
    peer.run("sudo -n dnf -y install podman tar gzip python3 python3-pip policycoreutils iproute-tc cronie", timeout=1800)
    stage = "/var/lib/arcturus-migration/session-" + state["nonce"]
    peer.run("sudo -n install -d -m 700 " + shlex.quote(stage + "/pi-migration"))
    for source in (HERE / "pi_migration_archive.py", HERE / "pi_migration_files.py", HERE / "pi_migration_runtime.py"):
        upload_checked(peer, source, stage + "/" + source.name)
    for name in ("deploy-files.py", "restore-host.py"):
        upload_checked(peer, ASSETS / name, stage + "/pi-migration/" + name)
    for name in ("files-plan.json", "portable-files.tar.gz", "runtime-plan.json", "host-plan.json"):
        upload_checked(peer, directory / name, stage + "/" + name)
    public = subprocess.run(["ssh-keygen", "-y", "-f", state["key"]], capture_output=True, check=True).stdout
    peer.run("sudo -n tee " + shlex.quote(stage + "/bootstrap.pub") + " > /dev/null", public)
    command = ["sudo", "-n", "/usr/bin/python3", stage + "/pi-migration/deploy-files.py", stage + "/files-plan.json", stage + "/portable-files.tar.gz", stage + "/bootstrap.pub", state["user"]]
    result = json.loads(peer.run(shlex.join(command), timeout=1800))
    durable_json(directory / "files-restoration-result.json", result)
    if (directory / "cloudflared").exists():
        install_cloudflared_atomic(peer, state, directory / "cloudflared")
    plan = json.loads((directory / "runtime-plan.json").read_text())
    for name in list(plan["image_exports"].values()) + [v["archive"] for v in plan["volumes"]]:
        upload_checked(peer, directory / name, stage + "/" + name)
    state["phase"] = "environment-restoring"
    state_save(state)
    peer.run(shlex.join(["sudo", "-n", "/usr/bin/python3", stage + "/pi_migration_runtime.py", "apply", stage + "/runtime-plan.json", stage, "--consume-images"]), timeout=1800)
    host_result = json.loads(peer.run(shlex.join(["sudo", "-n", "/usr/bin/python3", stage + "/pi-migration/restore-host.py", "apply", stage + "/host-plan.json"]), timeout=1800))
    durable_json(directory / "host-restoration-result.json", host_result)
    state["phase"] = "environment-restored"
    state_save(state)
    configure_exposure(state, peer, plan, json.loads((directory / "host-plan.json").read_text()))
    verify_environment(state)


def configure_exposure(state, peer, runtime, host):
    """Allow restored listeners through an already-running target firewall."""
    ports = set()
    for container in runtime["containers"]:
        if container["desired_running"]:
            for index, option in enumerate(container["command"][:-1]):
                if option == "--publish":
                    match = re.search(r":([0-9]+):[0-9]+/tcp$", container["command"][index + 1])
                    if match:
                        ports.add(int(match[1]))
    homekit = any(unit["name"] == "homekit-wol.service" and unit["active"] for unit in host["units"])
    if homekit:
        program = """import ast, json, pathlib
path=pathlib.Path('/opt/homekit-wol/homekit_service.py')
if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024*1024:
    raise RuntimeError('restored HomeKit source is not a bounded regular file')
ports={keyword.value.value for node in ast.walk(ast.parse(path.read_bytes())) if isinstance(node,ast.Call)
       for keyword in node.keywords if keyword.arg=='port' and isinstance(keyword.value,ast.Constant)
       and type(keyword.value.value) is int and 1 <= keyword.value.value <= 65535}
if len(ports)!=1:
    raise RuntimeError('restored HomeKit listen port requires explicit review')
print(json.dumps(sorted(ports)))
"""
        ports.update(json.loads(peer.run("sudo -n /usr/bin/python3 -", program.encode())))
    active = peer.run("sudo -n systemctl is-active firewalld || true").decode().strip() == "active"
    if active:
        zone = peer.run("sudo -n firewall-cmd --get-zone-of-interface " + shlex.quote(state["network"]["interface"]) + " || true").decode().strip()
        if zone == "no zone" or not zone:
            zone = peer.run("sudo -n firewall-cmd --get-default-zone").decode().strip()
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", zone):
            raise MigrationError("cannot identify the Ethernet firewall zone")
        for permanent in (True, False):
            prefix = ["sudo", "-n", "firewall-cmd", "--zone=" + zone] + (["--permanent"] if permanent else [])
            for port in sorted(ports):
                peer.run(shlex.join(prefix + ["--add-port=" + str(port) + "/tcp"]))
            if homekit:
                peer.run(shlex.join(prefix + ["--add-service=mdns"]))
    state["host_tcp_ports"] = sorted(ports)
    state["firewall_configured"] = active
    state_save(state)


def verify_environment(state):
    state["environment_verified"] = False
    state_save(state)
    peer = alma_peer(state)
    directory = Path(state["directory"])
    plan = json.loads((directory / "runtime-plan.json").read_text())
    host = json.loads((directory / "host-plan.json").read_text())
    def check():
        restarts = {}
        for container in plan["containers"]:
            running = peer.run(shlex.join(["sudo", "-n", "podman", "inspect", "--format", "{{.State.Running}}", container["name"]])).decode().strip() == "true"
            if running != container["desired_running"]:
                raise MigrationError("restored container activity differs from source: " + container["name"])
            if running:
                peer.run(shlex.join(["sudo", "-n", "systemctl", "is-active", container["unit"]]))
                count = peer.run(shlex.join(["sudo", "-n", "podman", "inspect", "--format", "{{.RestartCount}}", container["name"]])).decode().strip()
                restarts[container["name"]] = int(count)
        for unit in host["units"]:
            activity = peer.run(shlex.join(["sudo", "-n", "systemctl", "is-active", unit["name"]]) + " || true").decode().strip()
            enabled = peer.run(shlex.join(["sudo", "-n", "systemctl", "is-enabled", unit["name"]]) + " || true").decode().strip()
            if (activity == "active") != unit["active"] or activity == "failed":
                raise MigrationError("restored host unit activity differs from source: " + unit["name"])
            if enabled != unit["enablement"]:
                raise MigrationError("restored host unit enablement differs from source: " + unit["name"])
        for account in host.get("linger", []):
            present = peer.run("test -f /var/lib/systemd/linger/" + shlex.quote(account["user"]) + " && echo yes || echo no").decode().strip() == "yes"
            if present != account["enabled"]:
                raise MigrationError("restored user linger state differs from source")
        return restarts
    before = check()
    print("Restored services are running; checking that they remain active...", flush=True)
    time.sleep(30)
    if check() != before:
        raise MigrationError("a restored container restarted during the health observation")
    for container in plan["containers"]:
        if container["desired_running"]:
            for index, arg in enumerate(container["command"][:-1]):
                if arg == "--publish":
                    binding = container["command"][index + 1]
                    match = re.search(r":([0-9]+):[0-9]+/tcp$", binding)
                    if match:
                        with socket.create_connection((state["host"], int(match[1])), timeout=10):
                            pass
    for port in state.get("host_tcp_ports", []):
        with socket.create_connection((state["host"], port), timeout=10):
            pass
    boot_acceptance.verify_boot_payload(peer, state, require_rescue_default=False if state.get("boot_promoted") else True)
    state["environment_verified"] = True
    state_save(state)
    print("Restored Podman containers and host service/timer activity verified.")


def resume_timers(state):
    """Reproduce active but disabled source timers after an intentional reboot."""
    if not (Path(state["directory"]) / "host-restoration-result.json").is_file():
        raise MigrationError("host restoration must complete before resuming source timers")
    peer = alma_peer(state)
    plan = json.loads((Path(state["directory"]) / "host-plan.json").read_text())
    for unit in plan["units"]:
        if unit["name"].endswith(".timer") and unit["active"] and not unit["enabled"]:
            peer.run(shlex.join(["sudo", "-n", "systemctl", "start", unit["name"]]))
    print("Source timers that were active but disabled have been resumed without changing enablement.")


def reboot_final(state):
    if state.get("boot_promoted") is not True or state.get("packages_upgraded") is not True:
        raise MigrationError("permanent boot promotion and repository update are required")
    peer = alma_peer(state)
    boot_acceptance.verify_boot_payload(peer, state, require_rescue_default=False)
    state["environment_verified"] = False
    state["phase"] = "final-boot-requested"
    state_save(state)
    try:
        peer.run("sudo -n reboot", timeout=20)
    except (MigrationError, subprocess.TimeoutExpired):
        pass
    print("Normal AlmaLinux reboot requested. Verify the permanent boot with wait-alma, resume-timers and verify-environment.")


def test_recovery(state):
    peer = alma_peer(state)
    def reboot(normal):
        state_save(state)
        try:
            normal.run("sudo -n reboot", timeout=20)
        except (MigrationError, subprocess.TimeoutExpired):
            pass
    def wait_recovery(saved):
        for _ in range(36):
            try:
                return recovery_peer(saved)
            except (MigrationError, subprocess.TimeoutExpired):
                time.sleep(5)
        raise MigrationError("persistent recovery did not return after normal reset; do not promote")
    boot_acceptance.test_fallback(peer, state, normal_reboot=reboot, wait_recovery=wait_recovery, guard=guard)
    state_save(state)
    print("Normal reset returned to keyed on-card RAM recovery. Persistent fallback verified.")


def promote(state):
    if not state.get("packages_upgraded"):
        raise MigrationError("current AlmaLinux repository packages must be installed before permanent promotion")
    verify_environment(state)
    peer = alma_peer(state)
    report = boot_acceptance.promote_boot(peer, state)
    durable_json(Path(state["directory"]) / "boot-promotion-result.json", report)
    state_save(state)
    print("Permanent AlmaLinux boot selected after recovery and environment verification. Reboot remains a separate action.")


def upgrade_alma(state):
    if not state.get("fallback_verified") or not state.get("environment_verified"):
        raise MigrationError("test recovery and restored service health before upgrading AlmaLinux")
    peer = alma_peer(state)
    boot_acceptance.verify_boot_payload(peer, state)
    peer.run("sudo -n dnf -y upgrade --refresh", timeout=3600)
    boot_acceptance.verify_boot_payload(peer, state)
    state["packages_upgraded"] = True
    state_save(state)
    verify_environment(state)
    print("AlmaLinux updated from the latest official Pi image to current repository packages; recovery payload remains verified.")


def rollback(state, args):
    if args.confirm_cid != state["target"]["cid"]:
        raise MigrationError("--confirm-cid must exactly match the original card")
    directory = Path(state["directory"])
    manifest = verify_backup(directory)
    peer = recovery_peer(state)
    guard(peer)
    print("Restoring the original Debian whole-card backup through RAM SSH...", flush=True)
    with gzip.open(directory / "whole-card.raw.gz", "rb") as source:
        t = state["target"]
        checked_write = shlex.join(["/recovery/ram-guard.sh", state["nonce"], t["cid"], str(t["size_bytes"])]) + " >&2 && dd of=/dev/mmcblk0 bs=4M conv=fsync status=none"
        proc = subprocess.Popen(peer.command(checked_write), stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            shutil.copyfileobj(source, proc.stdin, 1024 * 1024)
            proc.stdin.close()
            errors = proc.stderr.read()
            if proc.wait() != 0:
                raise MigrationError("rollback write failed: " + errors.decode(errors="replace"))
        except BaseException:
            proc.kill(); proc.wait(); raise
    digest = peer.run("sync; blockdev --flushbufs /dev/mmcblk0; sha256sum /dev/mmcblk0", timeout=1800).decode().split()[0]
    if digest != manifest["files"]["whole-card.raw.gz"]["uncompressed_sha256"]:
        raise MigrationError("rollback readback differs from backup; keep RAM powered")
    state["phase"] = "rollback-verified"
    state_save(state)
    print("Debian rollback readback verified. Recovery reboot: ssh root on port 2222, then reboot -f.")


def restore_plan(state, args):
    directory = Path(state["directory"])
    verify_backup(directory)
    report = inspect_restore_selection(directory / "rootfs.tar.gz", args.path)
    durable_json(directory / "restore-plan.json", report)
    print(json.dumps(report, indent=2))
    print("Plan only. Reinstall AlmaLinux packages, map all numeric owners, recreate runtime containers and review units before activation.")


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--session", type=Path)
    sub = p.add_subparsers(dest="action", required=True)
    for action in ("discover", "init"):
        child = sub.add_parser(action)
        child.add_argument("--host", default="192.168.68.58")
        child.add_argument("--hostname", default="hori")
        child.add_argument("--user", default="aki")
        child.add_argument("--key", type=Path, default=Path.home() / ".ssh/hori")
        child.add_argument("--host-key-alias", default="192.168.68.58", help="existing known_hosts identity; never accepts a new key")
        child.add_argument("--scan-subnet")
        if action == "init":
            child.add_argument("--backup-dir", type=Path, required=True)
    for action in ("capture-source", "preflight-source", "export-images", "prepare-ram", "refresh-ram", "probe-ram", "boot-ram", "backup", "verify", "boot-alma", "retry-boot-alma", "resume-source", "prepare-restore", "inspect-image", "verify-restoration", "wait-alma", "restore", "finish-restore", "verify-environment", "test-recovery", "upgrade-alma", "promote", "resume-timers", "reboot-final", "stage-image", "finish-flash"):
        sub.add_parser(action)
    child = sub.add_parser("fetch-image")
    child.add_argument("--signing-key", type=Path, required=True)
    child.add_argument("--signing-fingerprint", required=True)
    for action in ("flash", "rollback"):
        child = sub.add_parser(action)
        child.add_argument("--confirm-cid", required=True)
    child = sub.add_parser("restore-plan")
    child.add_argument("--path", action="append", required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    awake = None
    if sys.platform == "darwin" and args.action != "discover":
        # Keep an open Mac awake throughout a transfer/write. This assertion
        # expires with this process; it never changes system power preferences.
        awake = subprocess.Popen(["/usr/bin/caffeinate", "-is", "-w", str(os.getpid())])
    try:
        if args.action == "discover":
            discover(args); return 0
        if args.action == "init":
            initialize(args); return 0
        state = state_load(args.session)
        actions = {"export-images": export_images, "prepare-ram": prepare_ram, "boot-ram": boot_ram,
                   "capture-source": capture_source,
                   "preflight-source": preflight_source,
                   "refresh-ram": lambda s: prepare_ram(s, refresh=True), "resume-source": resume_source,
                   "prepare-restore": prepare_restore, "probe-ram": probe_ram, "backup": offline_backup, "boot-alma": reboot_alma,
                   "retry-boot-alma": retry_boot_alma,
                   "inspect-image": inspect_image, "verify-restoration": verify_restoration, "wait-alma": wait_alma,
                   "restore": restore_environment, "finish-restore": finish_restore,
                   "verify-environment": verify_environment,
                   "test-recovery": test_recovery, "upgrade-alma": upgrade_alma, "promote": promote,
                   "resume-timers": resume_timers, "reboot-final": reboot_final, "stage-image": stage_image,
                   "finish-flash": finish_flash}
        if args.action in actions:
            actions[args.action](state)
        elif args.action == "verify":
            verify_backup(Path(state["directory"]))
            print("Complete offline backup verified.")
        elif args.action == "fetch-image":
            fetch_image(state, args)
        elif args.action == "flash":
            flash(state, args)
        elif args.action == "rollback":
            rollback(state, args)
        elif args.action == "restore-plan":
            restore_plan(state, args)
        return 0
    except (MigrationError, MigrationArchiveError, RuntimeMigrationError, FilesMigrationError, boot_acceptance.BootAcceptanceError, OSError, ValueError, subprocess.SubprocessError, lzma.LZMAError) as e:
        print("STOP: " + str(e), file=sys.stderr)
        return 1
    finally:
        if awake is not None:
            awake.terminate()
            awake.wait()


if __name__ == "__main__":
    raise SystemExit(main())
