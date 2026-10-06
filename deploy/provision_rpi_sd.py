#!/usr/bin/env python3
"""Prepare an AlmaLinux Raspberry Pi SD card for an Arcturus worker.

The destructive path is intentionally macOS-specific because it relies on
Disk Arbitration metadata to reject unsafe targets. Selection, rendering, and
network logic are kept pure enough for deterministic tests.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from arcturus_paths import resolve_paths

MINIMUM_DEVICE_BYTES = 8 * 1024**3
DEVICE_PATTERN = re.compile(r"^/dev/disk[0-9]+$")
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DIGEST_REFERENCE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
PUBLIC_KEY_PATTERN = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com)\s+\S+(?:\s+.*)?$"
)


class ProvisionError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiskCandidate:
    device: str
    raw_device: str
    size_bytes: int
    media_name: str
    bus_protocol: str


@dataclass(frozen=True)
class NetworkContext:
    interface: str
    address: str
    network: str
    gateway: str
    dns: tuple[str, ...]

    @property
    def subnet(self) -> ipaddress.IPv4Network:
        return ipaddress.ip_network(self.network)


@dataclass(frozen=True)
class ImageChoice:
    url: str
    major: int | None
    scheme: str


def run(
    command: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=capture,
        input=input_text,
    )


def require_commands(commands: Iterable[str]) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        raise ProvisionError(f"required commands are missing: {', '.join(missing)}")


def parse_disk_plist(payload: bytes) -> DiskCandidate:
    info = plistlib.loads(payload)
    device = str(info.get("DeviceNode", ""))
    size = int(info.get("TotalSize") or info.get("Size") or 0)
    safe = (
        DEVICE_PATTERN.fullmatch(device)
        and info.get("WholeDisk") is True
        and info.get("Internal") is False
        and info.get("VirtualOrPhysical") == "Physical"
        and info.get("Writable") is True
        and info.get("RemovableMediaOrExternalDevice") is True
        and size >= MINIMUM_DEVICE_BYTES
    )
    if not safe:
        raise ProvisionError(f"refusing unsafe or unsuitable disk: {device or 'unknown'}")
    identifier = str(info["DeviceIdentifier"])
    return DiskCandidate(
        device=device,
        raw_device=f"/dev/r{identifier}",
        size_bytes=size,
        media_name=str(info.get("MediaName") or info.get("IORegistryEntryName") or "external media"),
        bus_protocol=str(info.get("BusProtocol") or "unknown"),
    )


def discover_macos_disks() -> list[DiskCandidate]:
    listing = run(["diskutil", "list", "external", "physical"]).stdout
    devices = sorted(set(re.findall(r"^(/dev/disk[0-9]+) ", listing, re.MULTILINE)))
    candidates: list[DiskCandidate] = []
    for device in devices:
        result = subprocess.run(
            ["diskutil", "info", "-plist", device], check=True, capture_output=True
        )
        try:
            candidates.append(parse_disk_plist(result.stdout))
        except ProvisionError:
            continue
    return candidates


def parse_dhcp_packet(interface: str, payload: str) -> NetworkContext:
    def scalar(pattern: str) -> str:
        match = re.search(pattern, payload, re.MULTILINE)
        if not match:
            raise ProvisionError(f"DHCP lease on {interface} lacks {pattern}")
        return match.group(1)

    addresses = re.findall(r"^(?:ciaddr|yiaddr) = ([0-9.]+)$", payload, re.MULTILINE)
    address = next((value for value in addresses if value != "0.0.0.0"), "")
    if not address:
        raise ProvisionError(f"DHCP lease on {interface} has no usable IPv4 address")
    mask = scalar(r"^subnet_mask \(ip\): ([0-9.]+)$")
    gateway = scalar(r"^router \(ip_mult\): \{([0-9.]+)")
    dns_match = re.search(r"^domain_name_server \(ip_mult\): \{([^}]+)\}$", payload, re.MULTILINE)
    dns = tuple(
        value.strip()
        for value in (dns_match.group(1).split(",") if dns_match else [gateway])
        if value.strip()
    )
    network = ipaddress.ip_network(f"{address}/{mask}", strict=False)
    return NetworkContext(interface, address, str(network), gateway, dns)


def discover_macos_network(interface: str | None = None) -> NetworkContext:
    route = run(["route", "-n", "get", "default"]).stdout
    default_interface = re.search(r"^\s*interface:\s*(\S+)", route, re.MULTILINE)
    if interface is None:
        if not default_interface:
            raise ProvisionError("could not determine the active default-route interface")
        interface = default_interface.group(1)
    packet = run(["ipconfig", "getpacket", interface], check=False)
    if packet.returncode == 0 and packet.stdout.strip():
        try:
            return parse_dhcp_packet(interface, packet.stdout)
        except ProvisionError:
            pass
    interface_details = run(["ifconfig", interface]).stdout
    dns_details = run(["scutil", "--dns"], check=False).stdout
    return parse_static_network(interface, route, interface_details, dns_details)


def parse_static_network(
    interface: str, route: str, interface_details: str, dns_details: str
) -> NetworkContext:
    address_match = re.search(
        r"^\s*inet\s+([0-9.]+)\s+netmask\s+(0x[0-9a-fA-F]+|[0-9.]+)",
        interface_details,
        re.MULTILINE,
    )
    gateway_match = re.search(r"^\s*gateway:\s*([0-9.]+)", route, re.MULTILINE)
    if not address_match or not gateway_match:
        raise ProvisionError(f"could not derive IPv4 network details for {interface}")
    address, mask_value = address_match.groups()
    if mask_value.lower().startswith("0x"):
        mask = str(ipaddress.IPv4Address(int(mask_value, 16)))
    else:
        mask = mask_value
    network = ipaddress.ip_network(f"{address}/{mask}", strict=False)
    dns: list[str] = []
    for value in re.findall(r"^\s*nameserver\[[0-9]+\]\s*:\s*(\S+)", dns_details, re.MULTILINE):
        try:
            parsed = ipaddress.ip_address(value)
        except ValueError:
            continue
        if isinstance(parsed, ipaddress.IPv4Address) and value not in dns:
            dns.append(value)
    gateway = gateway_match.group(1)
    return NetworkContext(interface, address, str(network), gateway, tuple(dns or [gateway]))


def parse_arp_addresses(payload: str) -> set[ipaddress.IPv4Address]:
    addresses: set[ipaddress.IPv4Address] = set()
    for value in re.findall(r"\(([0-9.]+)\)", payload):
        try:
            addresses.add(ipaddress.ip_address(value))
        except ValueError:
            pass
    return addresses


def address_responds(address: ipaddress.IPv4Address) -> bool:
    if sys.platform == "darwin":
        command = ["ping", "-n", "-c", "1", "-W", "300", str(address)]
    else:
        command = ["ping", "-n", "-c", "1", "-W", "1", str(address)]
    return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def candidate_addresses(
    context: NetworkContext,
    occupied: set[ipaddress.IPv4Address],
    *,
    excluded_range: tuple[ipaddress.IPv4Address, ipaddress.IPv4Address] | None = None,
    limit: int = 8,
    probe: bool = True,
) -> list[str]:
    subnet = context.subnet
    excluded = {ipaddress.ip_address(context.address), ipaddress.ip_address(context.gateway)} | occupied
    last_host = int(subnet.broadcast_address) - 1
    first_candidate = max(int(subnet.network_address) + 1, last_host - 95)
    pool = [ipaddress.IPv4Address(value) for value in range(last_host, first_candidate - 1, -1)]

    def allowed(address: ipaddress.IPv4Address) -> bool:
        if address in excluded:
            return False
        if excluded_range and excluded_range[0] <= address <= excluded_range[1]:
            return False
        return True

    possible = [address for address in pool if allowed(address)]
    if probe:
        with ThreadPoolExecutor(max_workers=min(16, len(possible) or 1)) as executor:
            active = dict(zip(possible, executor.map(address_responds, possible)))
        possible = [address for address in possible if not active[address]]
    return [str(address) for address in possible[:limit]]


def validate_static_address(value: str, context: NetworkContext) -> str:
    try:
        interface = ipaddress.ip_interface(value if "/" in value else f"{value}/{context.subnet.prefixlen}")
    except ValueError as exc:
        raise ProvisionError(f"invalid static address: {value}") from exc
    if not isinstance(interface, ipaddress.IPv4Interface) or interface.ip not in context.subnet:
        raise ProvisionError(f"static address {interface} is not inside detected network {context.network}")
    if interface.network.prefixlen != context.subnet.prefixlen:
        raise ProvisionError(
            f"static address prefix /{interface.network.prefixlen} does not match detected network {context.network}"
        )
    forbidden = {
        context.subnet.network_address,
        context.subnet.broadcast_address,
        ipaddress.ip_address(context.address),
        ipaddress.ip_address(context.gateway),
    }
    if interface.ip in forbidden:
        raise ProvisionError(f"static address {interface.ip} conflicts with a reserved/current address")
    return str(interface)


def supported_alma_majors(compatibility_file: Path) -> list[int]:
    payload = json.loads(compatibility_file.read_text(encoding="utf-8"))
    majors: set[int] = set()
    for feature in payload.get("features", []):
        value = str(feature)
        if "almalinux-" not in value:
            continue
        majors.update(int(match) for match in re.findall(r"([0-9]+)\.[0-9]+", value))
    if not majors:
        raise ProvisionError(f"no supported AlmaLinux host major found in {compatibility_file}")
    return sorted(majors, reverse=True)


def parse_image_index(base_url: str, html: str, major: int | None = None) -> list[ImageChoice]:
    choices: list[ImageChoice] = []
    for href in re.findall(r'href=["\']([^"\']+\.aarch64\.raw\.xz)["\']', html, re.IGNORECASE):
        filename = urllib.parse.unquote(Path(urllib.parse.urlparse(href).path).name)
        if "RaspberryPi" not in filename or "GNOME" in filename:
            continue
        scheme = "gpt" if "-gpt-" in filename else "mbr" if "-mbr-" in filename else "unspecified"
        choices.append(ImageChoice(urllib.parse.urljoin(base_url, href), major, scheme))
    return sorted({choice.url: choice for choice in choices}.values(), key=lambda item: item.url, reverse=True)


def discover_images(repository: str, majors: Iterable[int]) -> list[ImageChoice]:
    choices: list[ImageChoice] = []
    for major in majors:
        index = f"{repository.rstrip('/')}/{major}/raspberrypi/images/"
        with urllib.request.urlopen(index, timeout=30) as response:
            choices.extend(parse_image_index(index, response.read().decode("utf-8"), major))
    return choices


def checksum_from_manifest(manifest: str, filename: str) -> str:
    patterns = (
        rf"^SHA256 \({re.escape(filename)}\) = ([0-9a-fA-F]{{64}})$",
        rf"^([0-9a-fA-F]{{64}})\s+[* ]?{re.escape(filename)}$",
    )
    for pattern in patterns:
        match = re.search(pattern, manifest, re.MULTILINE)
        if match:
            return match.group(1).lower()
    raise ProvisionError(f"official CHECKSUM does not contain {filename}")


def resolve_checksum(image_url: str, supplied: str | None) -> str:
    if supplied:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", supplied):
            raise ProvisionError("--image-sha256 must contain 64 hexadecimal characters")
        return supplied.lower()
    checksum_url = urllib.parse.urljoin(image_url, "CHECKSUM")
    with urllib.request.urlopen(checksum_url, timeout=30) as response:
        manifest = response.read().decode("utf-8")
    filename = Path(urllib.parse.urlparse(image_url).path).name
    return checksum_from_manifest(manifest, filename)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_verified(image_url: str, checksum: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(urllib.parse.urlparse(image_url).path).name
    if not filename.endswith(".raw.xz"):
        raise ProvisionError("AlmaLinux image must end in .raw.xz")
    destination = cache_dir / filename
    if destination.exists() and sha256_file(destination) == checksum:
        return destination
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    try:
        with urllib.request.urlopen(image_url, timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
        actual = sha256_file(temporary)
        if actual != checksum:
            raise ProvisionError(f"image checksum mismatch: expected {checksum}, received {actual}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def verify_local_image(path: Path, checksum: str | None) -> Path:
    image = path.expanduser().resolve()
    if not image.is_file():
        raise ProvisionError(f"local image does not exist: {image}")
    if not (image.name.endswith(".raw") or image.name.endswith(".raw.xz")):
        raise ProvisionError("local AlmaLinux image must end in .raw or .raw.xz")
    if not checksum:
        raise ProvisionError("--image-sha256 is required with --image-file")
    expected = resolve_checksum(image.as_uri(), checksum)
    actual = sha256_file(image)
    if actual != expected:
        raise ProvisionError(f"image checksum mismatch: expected {expected}, received {actual}")
    return image


def image_scheme(path: Path) -> str:
    return "gpt" if "-gpt-" in path.name else "mbr" if "-mbr-" in path.name else "unspecified"


def discover_public_keys(home: Path) -> list[str]:
    keys: list[str] = []
    for path in sorted((home / ".ssh").glob("*.pub")):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if PUBLIC_KEY_PATTERN.fullmatch(value):
            keys.append(value)
    if shutil.which("ssh-add"):
        result = run(["ssh-add", "-L"], check=False)
        for value in result.stdout.splitlines():
            if PUBLIC_KEY_PATTERN.fullmatch(value.strip()):
                keys.append(value.strip())
    return list(dict.fromkeys(keys))


def validate_public_key(value: str) -> str:
    value = value.strip()
    if not PUBLIC_KEY_PATTERN.fullmatch(value):
        raise ProvisionError("selected file does not contain a supported OpenSSH public key")
    return value


def network_config(context: NetworkContext, static_address: str, target_interface: str) -> str:
    payload = {
        "version": 2,
        "ethernets": {
            target_interface: {
                "dhcp4": False,
                "optional": True,
                "addresses": [static_address],
                "gateway4": context.gateway,
                "nameservers": {"addresses": list(context.dns)},
            }
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def cloud_init_user_data(hostname: str, public_key: str) -> str:
    firstboot = (
        'seed="$(findmnt -nr -S LABEL=CIDATA -o TARGET | head -n1)"; '
        'test -n "$seed"; exec /bin/bash "$seed/arcturus-firstboot.sh"'
    )
    payload = {
        "hostname": hostname,
        "manage_etc_hosts": True,
        "ssh_pwauth": False,
        "ssh_authorized_keys": [public_key],
        "runcmd": [["/bin/bash", "-lc", firstboot]],
    }
    return "#cloud-config\n" + json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render_firstboot(
    *,
    host_user: str,
    worker_id: str,
    control_plane_url: str,
    bundle: str,
    bundle_delivery: str,
    allowed_bind_roots: list[str],
    service_tokens: list[str],
    registry_auth: bool,
    tailscale: bool = False,
    tailscale_ssh: bool = False,
    layout_args: list[str] | None = None,
    config_root: str | None = None,
) -> str:
    if bundle_delivery == "staged":
        installer = "/var/lib/arcturus-firstboot/payload/arcturus/deploy/install-host.sh"
        source_args = ["--source-dir", "/var/lib/arcturus-firstboot/payload/arcturus/deploy"]
    else:
        installer = "/var/lib/arcturus-firstboot/install-host.sh"
        source_args = ["--bundle", bundle]
    installer_args = [
        installer,
        *source_args,
        "--host-user",
        host_user,
        "--enable-worker-agent",
        "--control-plane-url",
        control_plane_url,
        "--worker-id",
        worker_id,
        "--worker-token-file",
        "/var/lib/arcturus-firstboot/worker.token",
    ]
    for root in allowed_bind_roots:
        installer_args.extend(["--allowed-bind-root", root])
    installer_args.extend(layout_args or [])
    command = shlex.join(installer_args)
    token_installs = "\n".join(
        f'install -m 0600 "$seed/.arcturus-lifecycle-{service}.token" '
        + f'"$target_config_dir/lifecycle-tokens/{service}.token"'
        for service in service_tokens
    )
    registry_block = ""
    if registry_auth:
        registry_block = f"""
install -d -m 0700 -o "$host_user" -g "$host_user" "$target_config_root/containers"
install -m 0600 "$seed/.arcturus-registry-auth.json" "$target_config_root/containers/auth.json"
chown "$host_user:$host_user" "$target_config_root/containers/auth.json"
"""
    staged_required = ""
    staged_extract = ""
    if bundle_delivery == "staged":
        staged_required = (
            '[[ -f "$seed/arcturus-payload.tar.gz" ]] || '
            '{ echo "CIDATA is missing staged Arcturus payload" >&2; exit 1; }'
        )
        staged_extract = """mkdir -p /var/lib/arcturus-firstboot/payload
	tar -xzf "$seed/arcturus-payload.tar.gz" -C /var/lib/arcturus-firstboot/payload"""
    tailscale_required = ""
    tailscale_block = ""
    if tailscale:
        tailscale_required = (
            '[[ -f "$seed/.tailscale-auth-key" ]] || '
            '{ echo "CIDATA is missing Tailscale auth key" >&2; exit 1; }'
        )
        ssh_flag = " --ssh" if tailscale_ssh else ""
        tailscale_block = f"""
dnf install -y dnf-plugins-core
rhel_major="$(rpm -E '%{{rhel}}')"
[[ "$rhel_major" =~ ^[0-9]+$ ]] || {{ echo "RHEL-compatible major could not be detected" >&2; exit 1; }}
dnf config-manager --add-repo "https://pkgs.tailscale.com/stable/rhel/$rhel_major/tailscale.repo"
dnf install -y tailscale
systemctl enable --now tailscaled
tailscale_auth_key=/var/lib/arcturus-firstboot/tailscale.authkey
install -m 0600 "$seed/.tailscale-auth-key" "$tailscale_auth_key"
trap 'rm -f "$tailscale_auth_key"' EXIT
if ! tailscale status --json 2>/dev/null | grep -Eq '"BackendState"[[:space:]]*:[[:space:]]*"Running"'; then
  tailscale up --auth-key="file:$tailscale_auth_key" --hostname={shlex.quote(worker_id)}{ssh_flag}
fi
rm -f "$tailscale_auth_key"
"""
    return f"""#!/usr/bin/env bash
set -euo pipefail
exec > >(tee -a /var/log/arcturus-firstboot.log) 2>&1

marker=/var/lib/arcturus-firstboot/complete
[[ ! -e "$marker" ]] || exit 0
seed="$(findmnt -nr -S LABEL=CIDATA -o TARGET | head -n1)"
[[ -n "$seed" ]] || {{ echo "CIDATA mount could not be discovered" >&2; exit 1; }}
[[ -f "$seed/install-host.sh" ]] || {{ echo "CIDATA is missing install-host.sh" >&2; exit 1; }}
[[ -f "$seed/arcturus_paths.py" ]] || {{ echo "CIDATA is missing arcturus_paths.py" >&2; exit 1; }}
[[ -f "$seed/.arcturus-worker-token" ]] || {{ echo "CIDATA is missing worker credential" >&2; exit 1; }}
{staged_required}
{tailscale_required}

dnf install -y python3.12 python3.12-pip podman sudo curl xz
{tailscale_block}
if ! command -v node >/dev/null 2>&1 || [[ "$(node -p 'Number(process.versions.node.split(".")[0])')" -lt 22 ]]; then
  if dnf -q module list nodejs:22 --available 2>/dev/null | grep -Eq '^nodejs[[:space:]]+22'; then
    dnf module reset -y nodejs
    dnf module enable -y nodejs:22
  fi
  dnf install -y nodejs
fi
[[ "$(node -p 'Number(process.versions.node.split(".")[0])')" -ge 22 ]] || {{ echo "Node.js 22 or newer is unavailable from the configured repositories" >&2; exit 1; }}
id {shlex.quote(host_user)} >/dev/null 2>&1 || useradd --create-home {shlex.quote(host_user)}
host_user={shlex.quote(host_user)}
host_home="$(getent passwd "$host_user" | cut -d: -f6)"
[[ "$host_home" == /* ]] || {{ echo "home directory for $host_user could not be resolved" >&2; exit 1; }}
target_config_root={shlex.quote(config_root) if config_root else '"$host_home/.config"'}
target_config_dir="$target_config_root/arcturus"
loginctl enable-linger "$host_user"
uid="$(id -u "$host_user")"
systemctl start "user-runtime-dir@$uid.service" "user@$uid.service"
install -d -m 0700 /var/lib/arcturus-firstboot
install -m 0755 "$seed/install-host.sh" /var/lib/arcturus-firstboot/install-host.sh
install -m 0755 "$seed/arcturus_paths.py" /var/lib/arcturus-firstboot/arcturus_paths.py
install -m 0600 "$seed/.arcturus-worker-token" /var/lib/arcturus-firstboot/worker.token
chown "$host_user:$host_user" /var/lib/arcturus-firstboot/worker.token
{staged_extract}
install -d -m 0700 -o "$host_user" -g "$host_user" "$target_config_dir/lifecycle-tokens"
{token_installs}
chown -R "$host_user:$host_user" "$target_config_dir"
{registry_block}
runuser -u "$host_user" -- env \
  HOME="$host_home" USER="$host_user" LOGNAME="$host_user" \
  XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
  {command}

touch "$marker"
rm -f "$seed/.arcturus-worker-token" "$seed"/.arcturus-lifecycle-*.token "$seed/.arcturus-registry-auth.json" "$seed/.tailscale-auth-key" "$seed/arcturus-payload.tar.gz"
echo "Arcturus worker first boot completed"
"""


def stage_arcturus_bundle(
    bundle: str,
    bundle_registry_auth_file: Path | None,
    destination: Path,
    container_cli: str = "podman",
) -> Path:
    executable = Path(container_cli).name
    if executable not in {"podman", "docker"}:
        raise ProvisionError("--container-cli must resolve to podman or docker")
    if bundle_registry_auth_file and executable != "podman":
        raise ProvisionError(
            "--bundle-registry-auth-file requires Podman; use the selected engine's login store otherwise"
        )
    pull = [container_cli, "pull", "--platform", "linux/arm64"]
    if bundle_registry_auth_file and executable == "podman":
        pull.extend(["--authfile", str(bundle_registry_auth_file)])
    pull.append(bundle)
    run(pull, capture=False)
    create = [container_cli, "create", "--platform", "linux/arm64", bundle]
    container = run(create).stdout.strip()
    if not container:
        raise ProvisionError("podman did not return a bundle container ID")
    payload = destination / "arcturus"
    payload.mkdir(parents=True)
    try:
        run([container_cli, "cp", f"{container}:/opt/arcturus/.", str(payload)], capture=False)
    finally:
        run([container_cli, "rm", container], check=False, capture=False)
    required = [payload / "deploy" / "install-host.sh", payload / "deploy" / "arcturus-agent"]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ProvisionError(f"arm64 bundle is missing required worker payload: {', '.join(missing)}")
    archive = destination / "arcturus-payload.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(payload, arcname="arcturus")
    return archive


def choose(prompt: str, values: list[Any], describe: Callable[[Any], str]) -> Any:
    if not values:
        raise ProvisionError(f"no choices available for {prompt}")
    print(f"\n{prompt}")
    for index, value in enumerate(values, 1):
        print(f"  {index}. {describe(value)}")
    while True:
        answer = input("Select a number: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(values):
            return values[int(answer) - 1]
        print("Enter one of the listed numbers.")


def human_size(value: int) -> str:
    return f"{value / 1000**3:.1f} GB"


def cache_default() -> Path:
    if value := os.getenv("ARCTURUS_PROVISION_CACHE"):
        return Path(value).expanduser()
    return resolve_paths().cache_dir


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="provision-rpi-sd",
        description="Prepare an AlmaLinux Raspberry Pi SD card for an Arcturus worker.",
    )
    result.add_argument("--write", action="store_true", help="perform the destructive image write")
    result.add_argument("--non-interactive", action="store_true")
    result.add_argument("--confirm-device", help="must exactly match --device for non-interactive writes")
    result.add_argument("--device", help="whole external disk, selected interactively when omitted")
    result.add_argument("--host-interface", help="active workstation interface; default route when omitted")
    result.add_argument("--static-ip", help="target IPv4 address, with optional CIDR prefix")
    result.add_argument("--dhcp-range", help="known DHCP pool as START-END, excluded from suggestions")
    result.add_argument("--target-interface", help="target wired interface name")
    result.add_argument("--hostname")
    result.add_argument("--ssh-public-key", help="OpenSSH public-key file")
    result.add_argument("--alma-major", type=int)
    result.add_argument("--partition-scheme", choices=("gpt", "mbr"))
    result.add_argument("--image-url")
    result.add_argument("--image-file", type=Path, help="existing .raw or .raw.xz image")
    result.add_argument("--image-sha256")
    result.add_argument(
        "--alma-repository",
        default=os.getenv("ARCTURUS_ALMA_REPOSITORY", "https://repo.almalinux.org/almalinux"),
    )
    result.add_argument("--compatibility-file", type=Path)
    result.add_argument("--cache-dir", type=Path, default=cache_default())
    result.add_argument("--arcturus-bundle", required=True, help="digest-pinned multi-architecture bundle")
    result.add_argument(
        "--bundle-delivery",
        choices=("staged", "first-boot-pull"),
        default="staged",
        help="stage the arm64 bundle on the card (default) or pull it on first boot",
    )
    result.add_argument(
        "--container-cli",
        help="Podman or Docker used to extract a staged arm64 bundle; discovered when omitted",
    )
    result.add_argument("--host-user")
    result.add_argument("--config-root")
    result.add_argument("--data-root")
    result.add_argument("--cache-root")
    result.add_argument("--runtime-root")
    result.add_argument("--bin-dir")
    result.add_argument("--workload-root")
    result.add_argument("--worker-id", required=True)
    result.add_argument("--control-plane-url", required=True)
    result.add_argument("--worker-token-file", type=Path, required=True)
    result.add_argument(
        "--tailscale-auth-key-file",
        type=Path,
        help="one-off or ephemeral Tailscale auth key file staged for first boot",
    )
    result.add_argument(
        "--tailscale-ssh",
        action="store_true",
        help="enable Tailscale SSH; disabled unless explicitly selected",
    )
    result.add_argument("--service-token", action="append", default=[], metavar="SERVICE=FILE")
    result.add_argument(
        "--bundle-registry-auth-file",
        type=Path,
        help="workstation-side Podman auth used only to extract the Arcturus bundle",
    )
    result.add_argument(
        "--target-registry-auth-file",
        type=Path,
        help="pull-only Podman auth deliberately installed on the target worker",
    )
    result.add_argument("--allowed-bind-root", action="append", default=[])
    return result


def prompt_value(value: str | None, prompt: str, suggestion: str | None = None) -> str:
    if value:
        return value
    suffix = f" [{suggestion}]" if suggestion else ""
    while True:
        answer = input(f"{prompt}{suffix}: ").strip()
        resolved = answer or suggestion
        if resolved:
            return resolved
        print(f"{prompt} is required.")


def parse_service_tokens(values: list[str]) -> list[tuple[str, str]]:
    parsed: list[tuple[str, str]] = []
    for value in values:
        service, separator, filename = value.partition("=")
        if not separator or not NAME_PATTERN.fullmatch(service):
            raise ProvisionError("--service-token must use valid-service=/path/to/token")
        path = Path(filename).expanduser()
        if not path.is_file():
            raise ProvisionError(f"service token does not exist: {path}")
        parsed.append((service, str(path)))
    return parsed


def validate_args(args: argparse.Namespace) -> None:
    if sys.platform != "darwin":
        raise ProvisionError("the destructive SD-card writer currently supports macOS only")
    if args.non_interactive:
        required = {
            "--device": args.device,
            "--static-ip": args.static_ip,
            "--target-interface": args.target_interface,
            "--hostname": args.hostname,
            "--ssh-public-key": args.ssh_public_key,
            "--host-user": args.host_user,
        }
        missing = [name for name, value in required.items() if not value]
        if not args.image_file and not args.image_url and not args.alma_major:
            missing.append("--image-file, --image-url, or --alma-major")
        if not args.image_file and not args.image_url and not args.partition_scheme:
            missing.append("--partition-scheme")
        if missing:
            raise ProvisionError(f"non-interactive mode requires: {', '.join(missing)}")
    if not DIGEST_REFERENCE.fullmatch(args.arcturus_bundle):
        raise ProvisionError("--arcturus-bundle must be IMAGE@sha256:<64 lowercase hex>")
    if not NAME_PATTERN.fullmatch(args.worker_id):
        raise ProvisionError("--worker-id must be a lowercase DNS-style identifier")
    if not re.fullmatch(r"https?://[^\s]+", args.control_plane_url):
        raise ProvisionError("--control-plane-url must be an HTTP(S) URL")
    if not args.worker_token_file.is_file():
        raise ProvisionError(f"worker token does not exist: {args.worker_token_file}")
    if args.image_file and args.image_url:
        raise ProvisionError("--image-file and --image-url are mutually exclusive")
    if args.image_file and not args.image_sha256:
        raise ProvisionError("--image-sha256 is required with --image-file")
    if args.tailscale_ssh and not args.tailscale_auth_key_file:
        raise ProvisionError("--tailscale-ssh requires --tailscale-auth-key-file")
    if args.tailscale_auth_key_file:
        auth_key_file = args.tailscale_auth_key_file.expanduser()
        if not auth_key_file.is_file():
            raise ProvisionError(f"Tailscale auth key file does not exist: {auth_key_file}")
        if not auth_key_file.read_text(encoding="utf-8").strip():
            raise ProvisionError("Tailscale auth key file is empty")
    if args.bundle_registry_auth_file and not args.bundle_registry_auth_file.is_file():
        raise ProvisionError(
            f"bundle registry auth file does not exist: {args.bundle_registry_auth_file}"
        )
    if args.target_registry_auth_file and not args.target_registry_auth_file.is_file():
        raise ProvisionError(
            f"target registry auth file does not exist: {args.target_registry_auth_file}"
        )
    for option in (
        "config_root",
        "data_root",
        "cache_root",
        "runtime_root",
        "bin_dir",
        "workload_root",
    ):
        value = getattr(args, option)
        if value and not Path(value).expanduser().is_absolute():
            raise ProvisionError(f"--{option.replace('_', '-')} must be an absolute path")


def select_container_cli(value: str | None) -> str | None:
    candidates = [value] if value else ["podman", "docker"]
    for candidate in candidates:
        if candidate and shutil.which(candidate):
            return candidate
    return None


def pick_disk(args: argparse.Namespace) -> DiskCandidate:
    candidates = discover_macos_disks()
    if args.device:
        for candidate in candidates:
            if candidate.device == args.device:
                return candidate
        raise ProvisionError(f"{args.device} is not a safe external physical disk")
    if args.non_interactive:
        raise ProvisionError("--device is required in non-interactive mode")
    return choose(
        "External removable disks",
        candidates,
        lambda item: f"{item.device} — {item.media_name}, {human_size(item.size_bytes)}, {item.bus_protocol}",
    )


def pick_image(args: argparse.Namespace, repository_root: Path) -> ImageChoice:
    if args.image_url:
        return ImageChoice(args.image_url, args.alma_major, args.partition_scheme or "unspecified")
    compatibility = args.compatibility_file or repository_root / "COMPATIBILITY.json"
    majors = [args.alma_major] if args.alma_major else supported_alma_majors(compatibility)
    choices = discover_images(args.alma_repository, majors)
    if args.partition_scheme:
        choices = [choice for choice in choices if choice.scheme == args.partition_scheme]
    if args.non_interactive:
        if not choices:
            raise ProvisionError("no official image matches the requested major and partition scheme")
        non_alias = [choice for choice in choices if "latest" not in choice.url]
        return (non_alias or choices)[0]
    return choose(
        "Official AlmaLinux Raspberry Pi images",
        choices,
        lambda item: f"AlmaLinux {item.major or '?'} {item.scheme}: {Path(urllib.parse.urlparse(item.url).path).name}",
    )


def dhcp_exclusion(value: str | None, subnet: ipaddress.IPv4Network) -> tuple[ipaddress.IPv4Address, ipaddress.IPv4Address] | None:
    if not value:
        return None
    start_text, separator, end_text = value.partition("-")
    if not separator:
        raise ProvisionError("--dhcp-range must use START-END")
    start, end = ipaddress.ip_address(start_text), ipaddress.ip_address(end_text)
    if not isinstance(start, ipaddress.IPv4Address) or not isinstance(end, ipaddress.IPv4Address):
        raise ProvisionError("--dhcp-range must contain IPv4 addresses")
    if start not in subnet or end not in subnet or start > end:
        raise ProvisionError("--dhcp-range must be ordered and inside the detected subnet")
    return start, end


def pick_static_address(args: argparse.Namespace, context: NetworkContext) -> str:
    if args.static_ip:
        selected = validate_static_address(args.static_ip, context)
        address = ipaddress.ip_interface(selected).ip
        exclusion = dhcp_exclusion(args.dhcp_range, context.subnet)
        if exclusion and exclusion[0] <= address <= exclusion[1]:
            raise ProvisionError(f"static address {address} falls inside the declared DHCP pool")
        occupied = parse_arp_addresses(run(["arp", "-an"], check=False).stdout)
        if address in occupied or address_responds(address):
            raise ProvisionError(f"static address {address} is currently in use")
        return selected
    arp = run(["arp", "-an"], check=False).stdout
    exclusion = dhcp_exclusion(args.dhcp_range, context.subnet)
    suggestions = candidate_addresses(context, parse_arp_addresses(arp), excluded_range=exclusion)
    print(f"\nDetected LAN: {context.network} via {context.gateway} on {context.interface}")
    print("Candidates below were unresponsive now; reserve the selected address in the router or exclude its DHCP pool.")
    for index, address in enumerate(suggestions, 1):
        print(f"  {index}. {address}/{context.subnet.prefixlen}")
    while True:
        answer = input("Select a number or enter another IPv4 address: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(suggestions):
            value = suggestions[int(answer) - 1]
        else:
            value = answer
        try:
            selected = validate_static_address(value, context)
        except ProvisionError as exc:
            print(exc)
            continue
        address = ipaddress.ip_interface(selected).ip
        if address in parse_arp_addresses(run(["arp", "-an"], check=False).stdout) or address_responds(address):
            print(f"{address} is currently in use; choose another address.")
            continue
        return selected


def pick_public_key(args: argparse.Namespace) -> str:
    if args.ssh_public_key:
        return validate_public_key(Path(args.ssh_public_key).expanduser().read_text(encoding="utf-8"))
    keys = discover_public_keys(Path.home())
    if args.non_interactive:
        raise ProvisionError("--ssh-public-key is required in non-interactive mode")
    return choose("SSH public keys", keys, lambda value: value)


def locate_cidata(device: str, timeout: int = 30) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        listing = subprocess.run(
            ["diskutil", "list", "-plist", device], check=False, capture_output=True
        )
        if listing.returncode == 0:
            payload = plistlib.loads(listing.stdout)
            identifiers: list[str] = []
            for partition in payload.get("AllDisksAndPartitions", []):
                identifiers.extend(item.get("DeviceIdentifier", "") for item in partition.get("Partitions", []))
            for identifier in filter(None, identifiers):
                info_result = subprocess.run(
                    ["diskutil", "info", "-plist", identifier], check=False, capture_output=True
                )
                if info_result.returncode != 0:
                    continue
                info = plistlib.loads(info_result.stdout)
                if info.get("VolumeName") == "CIDATA" and info.get("MountPoint"):
                    return Path(info["MountPoint"])
        time.sleep(1)
    raise ProvisionError("the flashed image's CIDATA volume did not mount")


def write_seed(
    mount: Path,
    *,
    repository_root: Path,
    args: argparse.Namespace,
    context: NetworkContext,
    static_address: str,
    public_key: str,
    firstboot: str,
    payload_archive: Path | None,
) -> None:
    files = {
        "user-data": cloud_init_user_data(args.hostname, public_key),
        "meta-data": json.dumps(
            {"instance-id": f"arcturus-{args.worker_id}", "local-hostname": args.hostname},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        "network-config": network_config(context, static_address, args.target_interface),
        "arcturus-firstboot.sh": firstboot,
        "install-host.sh": (repository_root / "deploy" / "install-host.sh").read_text(encoding="utf-8"),
        "arcturus_paths.py": (repository_root / "deploy" / "arcturus_paths.py").read_text(
            encoding="utf-8"
        ),
    }
    for name, content in files.items():
        (mount / name).write_text(content, encoding="utf-8")
    shutil.copyfile(args.worker_token_file, mount / ".arcturus-worker-token")
    if args.tailscale_auth_key_file:
        shutil.copyfile(args.tailscale_auth_key_file.expanduser(), mount / ".tailscale-auth-key")
    for service, value in parse_service_tokens(args.service_token):
        shutil.copyfile(value, mount / f".arcturus-lifecycle-{service}.token")
    if args.target_registry_auth_file:
        shutil.copyfile(args.target_registry_auth_file, mount / ".arcturus-registry-auth.json")
    if payload_archive:
        required = payload_archive.stat().st_size + 16 * 1024**2
        available = shutil.disk_usage(mount).free
        if required > available:
            raise ProvisionError(
                f"CIDATA lacks space for staged Arcturus payload: need {human_size(required)}, have {human_size(available)}"
            )
        shutil.copyfile(payload_archive, mount / "arcturus-payload.tar.gz")


def flash_image(image: Path, disk: DiskCandidate) -> None:
    run(["diskutil", "unmountDisk", disk.device], capture=False)
    if image.name.endswith(".raw.xz"):
        decompressor = subprocess.Popen(["xz", "-dc", str(image)], stdout=subprocess.PIPE)
        assert decompressor.stdout is not None
        writer = subprocess.run(
            ["sudo", "dd", f"of={disk.raw_device}", "bs=4m"],
            stdin=decompressor.stdout,
            check=False,
        )
        decompressor.stdout.close()
        decompressor_status = decompressor.wait()
        if decompressor_status != 0 or writer.returncode != 0:
            raise ProvisionError("image write failed; the card may be incomplete")
    else:
        with image.open("rb") as source:
            writer = subprocess.run(
                ["sudo", "dd", f"of={disk.raw_device}", "bs=4m"],
                stdin=source,
                check=False,
            )
        if writer.returncode != 0:
            raise ProvisionError("image write failed; the card may be incomplete")
    run(["sync"], capture=False)
    run(["diskutil", "mountDisk", disk.device], capture=False)


def xz_uncompressed_size(image: Path) -> int:
    result = run(["xz", "--robot", "--list", str(image)])
    totals = [line for line in result.stdout.splitlines() if line.startswith("totals\t")]
    if not totals:
        raise ProvisionError("could not determine the uncompressed image size")
    fields = totals[-1].split("\t")
    if len(fields) < 5:
        raise ProvisionError("unexpected xz size output")
    return int(fields[4])


def image_uncompressed_size(image: Path) -> int:
    return xz_uncompressed_size(image) if image.name.endswith(".raw.xz") else image.stat().st_size


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        validate_args(args)
        require_commands(["diskutil", "route", "ipconfig", "ifconfig", "scutil", "arp", "ping"])
        repository_root = Path(__file__).resolve().parent.parent
        disk = pick_disk(args)
        context = discover_macos_network(args.host_interface)
        static_address = pick_static_address(args, context)
        args.target_interface = prompt_value(args.target_interface, "Target wired interface")
        args.hostname = prompt_value(args.hostname, "Target hostname", args.worker_id)
        args.host_user = prompt_value(args.host_user, "Rootless Arcturus service account")
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]{0,31}", args.host_user):
            raise ProvisionError("invalid service account name")
        if not NAME_PATTERN.fullmatch(args.hostname):
            raise ProvisionError("hostname must be a lowercase DNS-style name")
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]+", args.target_interface):
            raise ProvisionError("invalid target interface name")
        public_key = pick_public_key(args)
        local_image = verify_local_image(args.image_file, args.image_sha256) if args.image_file else None
        image_choice = None if local_image else pick_image(args, repository_root)
        checksum = (
            args.image_sha256.lower()
            if local_image
            else resolve_checksum(image_choice.url, args.image_sha256)
        )
        service_tokens = parse_service_tokens(args.service_token)
        layout_args: list[str] = []
        for option in (
            "config_root",
            "data_root",
            "cache_root",
            "runtime_root",
            "bin_dir",
            "workload_root",
        ):
            if value := getattr(args, option):
                layout_args.extend([f"--{option.replace('_', '-')}", str(Path(value).expanduser())])
        firstboot = render_firstboot(
            host_user=args.host_user,
            worker_id=args.worker_id,
            control_plane_url=args.control_plane_url,
            bundle=args.arcturus_bundle,
            bundle_delivery=args.bundle_delivery,
            allowed_bind_roots=args.allowed_bind_root,
            service_tokens=[service for service, _ in service_tokens],
            registry_auth=args.target_registry_auth_file is not None,
            tailscale=args.tailscale_auth_key_file is not None,
            tailscale_ssh=args.tailscale_ssh,
            layout_args=layout_args,
            config_root=args.config_root,
        )
        plan = {
            "mode": "write" if args.write else "plan-only",
            "disk": asdict(disk),
            "network": asdict(context),
            "staticAddress": static_address,
            "targetInterface": args.target_interface,
            "hostname": args.hostname,
            "workerId": args.worker_id,
            "image": {
                "source": str(local_image) if local_image else image_choice.url,
                "sha256": checksum,
                "scheme": image_scheme(local_image) if local_image else image_choice.scheme,
            },
            "arcturusBundle": args.arcturus_bundle,
            "bundleDelivery": args.bundle_delivery,
            "bundleExtractionCli": select_container_cli(args.container_cli)
            if args.bundle_delivery == "staged"
            else None,
            "targetRegistryAuthStaged": args.target_registry_auth_file is not None,
            "tailscale": {
                "enrollOnFirstBoot": args.tailscale_auth_key_file is not None,
                "sshEnabled": args.tailscale_ssh,
            },
            "sshPublicKeyContentSha256": hashlib.sha256(public_key.encode()).hexdigest(),
        }
        print(json.dumps(plan, indent=2, sort_keys=True))
        if not args.write:
            print("Plan only: no disk was unmounted or written. Re-run with --write to continue.")
            return 0
        if args.non_interactive:
            confirmed = args.confirm_device == disk.device
        else:
            confirmed = input(
                f"Type ERASE {disk.device} to destroy all data on {disk.media_name}: "
            ).strip() == f"ERASE {disk.device}"
        if not confirmed:
            raise ProvisionError("destructive confirmation did not exactly match the selected device")
        required_write_commands = ["sudo", "dd", "sync"]
        if not local_image or local_image.name.endswith(".raw.xz"):
            required_write_commands.append("xz")
        require_commands(required_write_commands)
        image = local_image or download_verified(image_choice.url, checksum, args.cache_dir.expanduser())
        raw_size = image_uncompressed_size(image)
        if raw_size > disk.size_bytes:
            raise ProvisionError(
                f"uncompressed image ({human_size(raw_size)}) exceeds device capacity ({human_size(disk.size_bytes)})"
            )
        with tempfile.TemporaryDirectory(prefix="arcturus-rpi-") as temporary:
            payload_archive = None
            if args.bundle_delivery == "staged":
                container_cli = select_container_cli(args.container_cli)
                if not container_cli:
                    raise ProvisionError(
                        "staged bundle delivery requires Podman or Docker; select first-boot-pull explicitly to defer it"
                    )
                payload_archive = stage_arcturus_bundle(
                    args.arcturus_bundle,
                    args.bundle_registry_auth_file,
                    Path(temporary),
                    container_cli,
                )
            try:
                flash_image(image, disk)
                mount = locate_cidata(disk.device)
                write_seed(
                    mount,
                    repository_root=repository_root,
                    args=args,
                    context=context,
                    static_address=static_address,
                    public_key=public_key,
                    firstboot=firstboot,
                    payload_archive=payload_archive,
                )
                run(["sync"], capture=False)
            finally:
                run(["diskutil", "eject", disk.device], check=False, capture=False)
        host = str(ipaddress.ip_interface(static_address).ip)
        print(f"SD card is ready. Boot the Raspberry Pi, then connect with: ssh almalinux@{host}")
        print("The selected public key is pre-authorized; no password-based ssh-copy-id step is needed.")
        return 0
    except (OSError, ValueError, urllib.error.URLError, subprocess.CalledProcessError, ProvisionError) as exc:
        print(f"provision-rpi-sd: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
