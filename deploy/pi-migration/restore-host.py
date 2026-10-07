#!/usr/bin/env python3
"""Plan or apply supported host-service and cron restoration on AlmaLinux.

Only known Arcturus host services are adapted. Unknown custom services or
unverifiable cron commands block apply; this tool never activates Docker or
Podman workload units, installs Tailscale, or enables arbitrary source units.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import uuid


class HostRestoreError(RuntimeError):
    """Unsafe, unsupported, or incomplete host restoration input."""


_SUPPORTED = {
    "homekit-wol.service", "cloudflared.service", "cloudflared-update.service",
    "cloudflared-update.timer", "thermal-governor.service", "gaming-sqm.service",
}
_UNIT_NAME = re.compile(r"^[A-Za-z0-9_.@:-]+\.(?:service|timer)$")
_CRON_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_REQ_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9_.+!-]*)(?:\s+#.*)?$")
_SYSTEM_PATH = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")
_CLOUDFLARED_TOKEN_FILE = "/etc/cloudflared/token"


def _status_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip().splitlines()[0].strip() if value.strip() else ""
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, str):
            lines = error.strip().splitlines()
            return lines[0].strip() if lines else ""
    return ""


def _recorded_unit_states(inventory: dict, name: str, record: dict) -> tuple[str | None, str | None]:
    active = _status_value(record.get("active"))
    if active not in ("active", "inactive", "failed", "activating", "deactivating"):
        state = record.get("active")
        if isinstance(state, dict) and state.get("status") == 3:
            active = "inactive"
        else:
            listing = inventory.get("active_units")
            if isinstance(listing, str):
                for line in listing.splitlines():
                    fields = line.split()
                    if len(fields) >= 4 and fields[0] == name and fields[1] in ("loaded", "not-found"):
                        active = fields[2]
                        break
    if active not in ("active", "inactive", "failed", "activating", "deactivating"):
        active = None

    enabled = _status_value(record.get("enabled"))
    if enabled not in ("enabled", "disabled", "static", "indirect", "masked", "generated", "alias", "linked", "linked-runtime", "enabled-runtime"):
        listing = inventory.get("system_units")
        if isinstance(listing, str):
            for line in listing.splitlines():
                fields = line.split()
                if len(fields) >= 2 and fields[0] == name:
                    enabled = fields[1]
                    break
    if enabled not in ("enabled", "disabled", "static", "indirect", "masked", "generated", "alias", "linked", "linked-runtime", "enabled-runtime"):
        enabled = None
    return active, enabled


def _unit_lines(content: str) -> list[str]:
    if not isinstance(content, str) or "\x00" in content:
        raise HostRestoreError("custom unit content is missing or invalid")
    return content.splitlines()


def _directive_locations(lines: list[str], section: str, directive: str) -> list[int]:
    current = ""
    found = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
        elif current == section and re.match(r"^\s*" + re.escape(directive) + r"\s*=", line):
            found.append(index)
    return found


def _directive_value(line: str) -> str:
    return line.split("=", 1)[1].strip()


def _replace_exec_start(lines: list[str], new_command: list[str]) -> None:
    locations = _directive_locations(lines, "Service", "ExecStart")
    if len(locations) != 1:
        raise HostRestoreError("supported service must have exactly one ExecStart")
    lines[locations[0]] = "ExecStart=" + shlex.join(new_command)


def _cloudflared_token_file(command: list[str]) -> str | None:
    values: list[str] = []
    index = 0
    while index < len(command):
        argument = command[index]
        if argument == "--token-file":
            if index + 1 >= len(command):
                raise HostRestoreError("cloudflared --token-file is missing its path")
            values.append(command[index + 1])
            index += 2
            continue
        if argument.startswith("--token-file="):
            values.append(argument.split("=", 1)[1])
        index += 1
    if not values:
        return None
    if len(values) != 1 or values[0] != _CLOUDFLARED_TOKEN_FILE:
        raise HostRestoreError("cloudflared token file must be the reviewed /etc/cloudflared/token path")
    return _CLOUDFLARED_TOKEN_FILE


def _validate_cloudflared_token(path: Path, root: Path) -> None:
    _no_symlink_ancestors(path, root)
    try:
        parent = path.parent.lstat()
        info = path.lstat()
    except OSError as exc:
        raise HostRestoreError("cloudflared token file is missing") from exc
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022):
        raise HostRestoreError("cloudflared token directory must be a protected root-owned real directory")
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or info.st_size <= 0 or stat.S_IMODE(info.st_mode) & 0o077):
        raise HostRestoreError("cloudflared token must be nonempty, root-owned, and protected")


def _transform_unit(name: str, content: str) -> tuple[str, dict]:
    lines = _unit_lines(content)
    facts: dict = {}
    if name == "homekit-wol.service":
        locations = _directive_locations(lines, "Service", "ExecStart")
        if len(locations) != 1:
            raise HostRestoreError("homekit-wol must have exactly one ExecStart")
        command = shlex.split(_directive_value(lines[locations[0]]))
        if not command or Path(command[0]).name not in ("python", "python3"):
            raise HostRestoreError("homekit-wol ExecStart must be a Python command")
        if "-c" in command or "-m" in command:
            raise HostRestoreError("homekit-wol inline or module ExecStart needs manual review")
        script = next((arg for arg in command[1:] if not arg.startswith("-")), "")
        if not script.startswith("/opt/homekit-wol/"):
            raise HostRestoreError("homekit-wol ExecStart script must be inside /opt/homekit-wol")
        command[0] = "/opt/homekit-wol/venv-almalinux/bin/python3"
        _replace_exec_start(lines, command)
        facts["requirements"] = "/opt/homekit-wol/requirements.txt"
    elif name == "cloudflared-update.service":
        locations = _directive_locations(lines, "Service", "ExecStart")
        if len(locations) != 1:
            raise HostRestoreError("cloudflared updater must have exactly one ExecStart")
        command = shlex.split(_directive_value(lines[locations[0]]))
        if len(command) != 3 or Path(command[0].lstrip("-+!@")) != Path("/bin/bash") or command[1] != "-c":
            raise HostRestoreError("cloudflared updater ExecStart differs from the reviewed command")
        script = " ".join(command[2].split())
        expected = ("/usr/bin/cloudflared update; code=$?; if [ $code -eq 11 ]; then "
                    "systemctl restart cloudflared; exit 0; fi; exit $code")
        if script != expected:
            raise HostRestoreError("cloudflared updater shell logic differs from the reviewed command")
        command[2] = script.replace("/usr/bin/cloudflared", "/usr/local/bin/cloudflared", 1)
        _replace_exec_start(lines, command)
        facts["binary"] = "/usr/local/bin/cloudflared"
        facts["update_signal_restart_code"] = 11
    elif name == "cloudflared-update.timer":
        on_calendar = _directive_locations(lines, "Timer", "OnCalendar")
        if len(on_calendar) != 1 or _directive_value(lines[on_calendar[0]]) != "daily":
            raise HostRestoreError("cloudflared updater timer must have exactly one OnCalendar=daily")
        install = _directive_locations(lines, "Install", "WantedBy")
        if len(install) != 1 or _directive_value(lines[install[0]]) != "timers.target":
            raise HostRestoreError("cloudflared updater timer must be enabled by timers.target")
        timer_directives = []
        section = ""
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1]
            elif section == "Timer" and stripped and not stripped.startswith(("#", ";")):
                timer_directives.append(stripped.split("=", 1)[0])
        if set(timer_directives) - {"OnCalendar", "Persistent"} or timer_directives.count("OnCalendar") != 1:
            raise HostRestoreError("cloudflared updater timer has unsupported scheduling directives")
        persistent = _directive_locations(lines, "Timer", "Persistent")
        if persistent and (len(persistent) != 1 or _directive_value(lines[persistent[0]]).lower() not in ("yes", "no", "true", "false")):
            raise HostRestoreError("cloudflared updater timer has an invalid Persistent value")
        facts["schedule"] = "daily"
    elif name == "cloudflared.service":
        locations = _directive_locations(lines, "Service", "ExecStart")
        if len(locations) != 1:
            raise HostRestoreError("cloudflared must have exactly one ExecStart")
        command = shlex.split(_directive_value(lines[locations[0]]))
        if not command or Path(command[0].lstrip("-+!@")).name != "cloudflared":
            raise HostRestoreError("cloudflared ExecStart must invoke cloudflared directly")
        token_file = _cloudflared_token_file(command)
        if token_file:
            facts["token_file"] = token_file
        command[0] = "/usr/local/bin/cloudflared"
        _replace_exec_start(lines, command)
        facts["binary"] = "/usr/local/bin/cloudflared"
    elif name == "thermal-governor.service":
        adjusted = []
        for line in lines:
            if (not line.lstrip().startswith(("#", ";"))
                    and re.match(r"^\s*After\s*=", line)
                    and "multi-user.target" in _directive_value(line).split()):
                remaining = [item for item in _directive_value(line).split() if item != "multi-user.target"]
                if remaining:
                    line = "After=" + " ".join(remaining)
                else:
                    continue
            adjusted.append(line)
        lines = adjusted
        facts["ordering_fix"] = "removed After=multi-user.target to avoid the target ordering cycle"
    elif name == "gaming-sqm.service":
        locations = _directive_locations(lines, "Unit", "ConditionPathExists")
        if any(_directive_value(lines[index]) != "/sys/class/net/wlan0" for index in locations):
            raise HostRestoreError("gaming-sqm has an incompatible ConditionPathExists directive")
        if locations:
            remove = set(locations)
            lines = [line for index, line in enumerate(lines) if index not in remove]

        interfaces = ("wlan0", "eth0")
        start_locations = _directive_locations(lines, "Service", "ExecStart")
        if len(start_locations) != len(interfaces):
            raise HostRestoreError("gaming-sqm must have exactly two reviewed ExecStart commands")
        seen_starts = set()
        for index in start_locations:
            command = shlex.split(_directive_value(lines[index]))
            if len(command) != 10 or command[0] != "/sbin/tc":
                raise HostRestoreError("gaming-sqm ExecStart differs from the reviewed tc command")
            interface = command[4]
            expected = ["qdisc", "replace", "dev", interface, "root", "cake", "bandwidth", "190mbit", "diffserv4"]
            if interface not in interfaces or command[1:] != expected or interface in seen_starts:
                raise HostRestoreError("gaming-sqm ExecStart differs from the reviewed tc settings")
            script = (f"if test -e /sys/class/net/{interface}; then "
                      f"exec /usr/sbin/tc qdisc replace dev {interface} root cake bandwidth 190mbit diffserv4; fi")
            lines[index] = "ExecStart=" + shlex.join(["/bin/sh", "-c", script])
            seen_starts.add(interface)
        if seen_starts != set(interfaces):
            raise HostRestoreError("gaming-sqm must configure wlan0 and eth0 exactly once")

        stop_locations = _directive_locations(lines, "Service", "ExecStop")
        if len(stop_locations) != len(interfaces):
            raise HostRestoreError("gaming-sqm must have exactly two reviewed ExecStop commands")
        seen_stops = set()
        for index in stop_locations:
            command = shlex.split(_directive_value(lines[index]))
            if len(command) == 3 and command[:2] == ["/bin/sh", "-c"]:
                shell_command = shlex.split(command[2])
            elif command and command[0] == "/sbin/tc":
                shell_command = command
            else:
                raise HostRestoreError("gaming-sqm ExecStop differs from the reviewed tc command")
            interface = shell_command[4] if len(shell_command) > 4 else ""
            expected = ["/sbin/tc", "qdisc", "del", "dev", interface, "root", "2>/dev/null", "||", "true"]
            if shell_command != expected:
                raise HostRestoreError("gaming-sqm ExecStop differs from the reviewed tc command")
            if interface not in interfaces or interface in seen_stops:
                raise HostRestoreError("gaming-sqm ExecStop must target wlan0 and eth0 exactly once")
            script = (f"if test -e /sys/class/net/{interface}; then "
                      f"/usr/sbin/tc qdisc del dev {interface} root 2>/dev/null || true; fi")
            lines[index] = "ExecStop=" + shlex.join(["/bin/sh", "-c", script])
            seen_stops.add(interface)
        if seen_stops != set(interfaces):
            raise HostRestoreError("gaming-sqm must stop wlan0 and eth0 exactly once")
        facts["interfaces"] = list(interfaces)
    return "\n".join(lines).rstrip() + "\n", facts


def _excluded_unit(name: str, content: str) -> str | None:
    lower = name.lower()
    if lower.startswith(("tailscale", "tailscaled")):
        return "Tailscale service"
    if lower.startswith(("docker", "podman", "container-", "arcturus-migrated-")):
        return "container lifecycle managed by the migrated runtime"
    if re.search(r"(?im)^\s*Exec(?:Start|Stop|Reload)=.*\b(?:docker|podman)\s+(?:run|start|stop|compose|container)\b", content):
        return "container lifecycle managed by the migrated runtime"
    return None


def _rooted(root: Path, absolute: str) -> Path:
    candidate = PurePosixPath(absolute)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise HostRestoreError("inventory path is not a normalized absolute path")
    return root / candidate.relative_to("/")


def _real_regular(path: Path) -> bool:
    try:
        info = path.lstat()
        return stat.S_ISREG(info.st_mode)
    except OSError:
        return False


def _is_executable(path: Path) -> bool:
    try:
        info = path.stat()
        return stat.S_ISREG(info.st_mode) and bool(info.st_mode & 0o111)
    except OSError:
        return False


def _is_real_executable(path: Path) -> bool:
    try:
        info = path.lstat()
        return stat.S_ISREG(info.st_mode) and bool(info.st_mode & 0o111)
    except OSError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cloudflared_descriptor(dependencies: object) -> tuple[str | None, str | None]:
    if not isinstance(dependencies, dict):
        return None, None
    record = dependencies.get("cloudflared")
    if not isinstance(record, dict):
        return None, None
    descriptor = record.get("file")
    if isinstance(descriptor, dict):
        source = descriptor.get("path") or descriptor.get("name")
        digest = record.get("sha256") or descriptor.get("sha256")
    else:
        source = descriptor
        digest = record.get("sha256")
    if not isinstance(source, str) or not source or "/" in source and Path(source).name != "cloudflared":
        return None, None
    if Path(source).name != "cloudflared" or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None, None
    return source, digest


def _which(root: Path, name: str, path_value: str | None = None, *, verify_target: bool = True) -> str | None:
    if "/" in name:
        if not name.startswith("/"):
            return None
        return name if not verify_target or _is_executable(_rooted(root, name)) else None
    search = path_value.split(":") if path_value else list(_SYSTEM_PATH)
    for directory in search:
        if not directory.startswith("/") or ".." in PurePosixPath(directory).parts:
            continue
        candidate = str(PurePosixPath(directory) / name)
        if not verify_target or _is_executable(_rooted(root, candidate)):
            return candidate
    return None


def _cron_entries(raw: object, username: str, root: Path, users: set[str], *, verify_target: bool = True) -> tuple[str | None, list[str]]:
    if isinstance(raw, dict) and raw.get("status") and isinstance(raw.get("error"), str):
        if "no crontab for" in raw["error"].lower():
            return None, []
        return None, [f"cron for {username} reported a source-side error"]
    if not isinstance(raw, str):
        return None, [f"cron for {username} is not captured as text"]
    blockers: list[str] = []
    current_path: str | None = None
    for number, original in enumerate(raw.splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if _CRON_ENV.fullmatch(line):
            key, value = line.split("=", 1)
            if key == "PATH":
                current_path = value
            continue
        fields = line.split(None, 5)
        if fields and fields[0].startswith("@"):
            if len(fields) < 2:
                blockers.append(f"cron for {username} line {number} is incomplete")
                continue
            command_text = " ".join(fields[1:])
        elif len(fields) == 6:
            command_text = fields[5]
        else:
            blockers.append(f"cron for {username} line {number} has invalid schedule syntax")
            continue
        if any(char in command_text for char in ";&|<>`$()%\n\r"):
            blockers.append(f"cron for {username} line {number} uses shell syntax requiring manual review")
            continue
        try:
            command = shlex.split(command_text)
        except ValueError:
            blockers.append(f"cron for {username} line {number} has invalid command quoting")
            continue
        if not command or command[0].startswith(("NAME=", "VAR=")):
            blockers.append(f"cron for {username} line {number} does not have a directly checkable executable")
            continue
        executable = _which(root, command[0], current_path, verify_target=verify_target)
        if executable is None:
            blockers.append(f"cron for {username} line {number} executable is unavailable on the target")
            continue
        basename = Path(executable).name
        if basename == "env":
            blockers.append(f"cron for {username} line {number} wraps its command with env and needs manual dependency review")
            continue
        if basename in ("python", "python2", "python3", "perl", "ruby", "node"):
            if any(flag in command[1:] for flag in ("-c", "-e", "-m")):
                blockers.append(f"cron for {username} line {number} uses interpreter inline/module execution")
                continue
            operands = [arg for arg in command[1:] if not arg.startswith("-")]
            if operands and not operands[0].startswith("/"):
                blockers.append(f"cron for {username} line {number} has a relative script dependency")
                continue
            if verify_target and operands and operands[0].startswith("/") and not _real_regular(_rooted(root, operands[0])):
                blockers.append(f"cron for {username} line {number} script dependency is unavailable")
                continue
            if not operands:
                blockers.append(f"cron for {username} line {number} interpreter dependency is not explicit")
                continue
        if command[0] in users or Path(command[0]).name in users:
            blockers.append(f"cron for {username} line {number} uses a user command that cannot be validated")
    return raw if not blockers else None, blockers


def _requirements(dependencies: object) -> tuple[list[str], list[str]]:
    if not isinstance(dependencies, dict) or not isinstance(dependencies.get("homekit_requirements"), list):
        return [], ["protected dependencies.json must contain homekit_requirements"]
    packages: list[str] = []
    blockers: list[str] = []
    for number, original in enumerate(dependencies["homekit_requirements"], 1):
        if not isinstance(original, str):
            blockers.append(f"homekit dependency entry {number} is not a pinned requirement")
            continue
        line = original.strip()
        match = _REQ_PIN.fullmatch(line)
        if not match:
            blockers.append(f"homekit-wol requirement line {number} is not an exact package pin")
        else:
            packages.append(line)
    if not packages and not blockers:
        blockers.append("dependencies.json has no pinned homekit-wol packages")
    return packages, blockers


def build_plan(inventory: dict, dependencies: dict | None = None, target_root: Path = Path("/"),
               *, verify_target: bool = True) -> dict:
    """Build an offline plan; no target state is changed and no commands run."""
    if not isinstance(inventory, dict) or inventory.get("schemaVersion") != 1:
        raise HostRestoreError("unsupported source inventory")
    if dependencies is None:
        dependencies = inventory.get("dependencies")
    target_root = Path(target_root)
    if not isinstance(inventory.get("custom_units"), dict) or not isinstance(inventory.get("cron"), dict):
        raise HostRestoreError("inventory is missing custom_units or cron data")
    blockers: list[str] = []
    limitations: list[str] = []
    target_dependencies_pending: list[str] = []
    units: list[dict] = []
    exclusions: list[dict] = []
    for name, record in inventory["custom_units"].items():
        if not isinstance(name, str) or not _UNIT_NAME.fullmatch(name) or "/" in name:
            blockers.append("inventory contains an invalid custom systemd unit name")
            continue
        if not isinstance(record, dict) or not isinstance(record.get("content"), str):
            blockers.append(f"{name} has no captured unit content")
            continue
        content = record["content"]
        excluded = _excluded_unit(name, content)
        if excluded:
            exclusions.append({"unit": name, "reason": excluded})
            continue
        if name not in _SUPPORTED:
            blockers.append(f"unsupported custom unit: {name}")
            continue
        try:
            adapted, facts = _transform_unit(name, content)
        except (HostRestoreError, ValueError) as exc:
            blockers.append(f"{name}: {exc}")
            continue
        active_state, enabled_state = _recorded_unit_states(inventory, name, record)
        if active_state is None:
            blockers.append(f"{name} source activity state was not captured")
            continue
        if enabled_state not in ("enabled", "disabled", "static"):
            blockers.append(f"{name} source enablement state {enabled_state or 'unknown'} cannot be reproduced safely")
            continue
        units.append({"name": name, "content": adapted, "source_content": content,
                      "enabled": enabled_state == "enabled", "enablement": enabled_state,
                      "active": active_state in ("active", "activating"), "active_state": active_state, "facts": facts})

    if any(unit["name"] == "homekit-wol.service" for unit in units):
        packages, req_blockers = _requirements(dependencies)
        blockers.extend(req_blockers)
        for unit in units:
            if unit["name"] == "homekit-wol.service":
                unit["requirements_packages"] = packages
        if verify_target and not (target_root / "opt/homekit-wol").is_dir():
            blockers.append("homekit-wol application directory is absent from the target filesystem")
        elif not verify_target:
            target_dependencies_pending.append("restored /opt/homekit-wol application directory")
    for unit in units:
        if unit["name"] in ("cloudflared.service", "cloudflared-update.service"):
            source_name, expected_digest = _cloudflared_descriptor(dependencies)
            if source_name is None or expected_digest is None:
                blockers.append("dependencies.json must identify the provided cloudflared binary and SHA-256")
                continue
            binary = _rooted(target_root, "/usr/local/bin/cloudflared")
            if verify_target:
                if not _is_real_executable(binary):
                    blockers.append("provided /usr/local/bin/cloudflared binary is missing or not executable")
                elif _sha256(binary) != expected_digest:
                    blockers.append("provided cloudflared binary does not match dependencies.json SHA-256")
            else:
                target_dependencies_pending.append("provided /usr/local/bin/cloudflared binary and SHA-256")
            unit["cloudflared_sha256"] = expected_digest

    users = {str(user.get("name")) for user in inventory.get("users", []) if isinstance(user, dict) and user.get("name")}
    cron_plan: list[dict] = []
    for username, raw in inventory["cron"].items():
        if not isinstance(username, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}\$?", username):
            blockers.append("inventory contains an invalid cron account name")
            continue
        if username not in users:
            blockers.append(f"cron account {username} is absent from the source user inventory")
            continue
        content, cron_blockers = _cron_entries(raw, username, target_root, users, verify_target=verify_target)
        blockers.extend(cron_blockers)
        cron_plan.append({"user": username, "content": content})
        if not verify_target and content is not None:
            target_dependencies_pending.append(f"cron executables and script files for {username}")

    user_units = inventory.get("user_units")
    linger: list[dict] = []
    if not isinstance(user_units, dict):
        blockers.append("user systemd inventory is missing; refusing to infer enablement")
    else:
        custom_user_units = inventory.get("custom_user_units", {})
        if isinstance(custom_user_units, dict):
            for username, captured in custom_user_units.items():
                if (not isinstance(username, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}\$?", username)
                        or not isinstance(captured, (dict, list))):
                    blockers.append("custom_user_units inventory contains a malformed account or unit listing")
                elif captured:
                    blockers.append("custom user systemd units are not supported by this host restore helper")
        elif isinstance(custom_user_units, list):
            if custom_user_units:
                blockers.append("custom user systemd units are not supported by this host restore helper")
        else:
            blockers.append("custom_user_units inventory must be a mapping or list")
        for username, captured in user_units.items():
            if isinstance(captured, dict) and "error" in captured:
                limitations.append(f"user systemd manager for {username} was unavailable; restored home enablement links are preserved, but active state is unknown")
                continue
            if isinstance(captured, str) and captured.strip():
                limitations.append(f"user systemd unit listing for {username} may include distribution units; custom_user_units is the restoration gate and home enablement links are preserved")
            elif not isinstance(captured, str):
                blockers.append(f"user systemd inventory for {username} has an unsupported format")
        if "user_linger" not in inventory:
            limitations.append("source inventory has no loginctl linger state; user lingering cannot be reproduced")
        elif not isinstance(inventory["user_linger"], dict):
            blockers.append("user_linger inventory is not a user-to-boolean mapping")
    if "user_linger" in inventory and isinstance(inventory["user_linger"], dict):
        for username, enabled in inventory["user_linger"].items():
            if username not in users or not isinstance(enabled, bool):
                blockers.append("user_linger contains an unknown account or invalid state")
            else:
                linger.append({"user": username, "enabled": enabled})

    return {
        "schemaVersion": 1,
        "ready": not blockers,
        "units": units,
        "cron": cron_plan,
        "source_users": sorted(users),
        "target_dependencies_pending": sorted(set(target_dependencies_pending)),
        "linger": linger,
        "excluded_units": exclusions,
        "blockers": blockers,
        "limitations": limitations,
    }


def _unit_dir(root: Path) -> Path:
    return root / "etc/systemd/system"


def _no_symlink_ancestors(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise HostRestoreError("host restore path escapes the target root") from exc
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        if current.is_symlink():
            raise HostRestoreError("host restore path cannot traverse a symlink")
        if index < len(relative.parts) - 1 and current.exists() and not current.is_dir():
            raise HostRestoreError("host restore path has a non-directory ancestor")


def _preflight(plan: dict, root: Path, run=subprocess.run) -> tuple[list[Path], list[tuple[str, str | None]], list[str]]:
    if not plan.get("ready"):
        raise HostRestoreError("host restoration plan has unsupported items; inspect the plan report")
    if not isinstance(plan.get("units"), list) or not isinstance(plan.get("cron"), list):
        raise HostRestoreError("restore plan is missing unit or cron actions")
    unit_dir = _unit_dir(root)
    _no_symlink_ancestors(unit_dir, root)
    if not unit_dir.is_dir():
        raise HostRestoreError("target systemd unit directory must be a real existing directory")
    unit_paths: list[Path] = []
    for unit in plan["units"]:
        path = unit_dir / unit["name"]
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise HostRestoreError("target custom unit path has an unsafe type")
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if existing not in (unit["content"], unit.get("source_content")):
                raise HostRestoreError("target custom unit changed after filesystem restore")
        unit_paths.append(path)
    for unit in plan["units"]:
        if unit["name"] == "homekit-wol.service":
            app = root / "opt/homekit-wol"
            venv = app / "venv-almalinux"
            _no_symlink_ancestors(app, root)
            if not app.is_dir() or venv.is_symlink():
                raise HostRestoreError("homekit-wol application or venv path is unsafe")
            if venv.exists() and (not venv.is_dir() or not (venv / "bin/python3").is_file()
                                  or not _real_regular(venv / "pyvenv.cfg")):
                raise HostRestoreError("existing AlmaLinux homekit-wol venv is incomplete")
            if venv.exists():
                result = run([str(venv / "bin/python3"), "-m", "pip", "freeze", "--all"],
                             capture_output=True, text=True, check=False)
                if result.returncode:
                    raise HostRestoreError("cannot verify existing AlmaLinux homekit-wol venv")
                def canonical(spec: str) -> str:
                    name, marker, version = spec.partition("==")
                    return re.sub(r"[-_.]+", "-", name.lower()) + marker + version
                actual = {canonical(line.strip()) for line in result.stdout.splitlines()
                          if "==" in line and not line.lower().startswith(("pip==", "setuptools=="))}
                wanted = {canonical(item) for item in unit.get("requirements_packages", [])}
                if actual != wanted:
                    raise HostRestoreError("existing AlmaLinux homekit-wol venv differs from the pinned package inventory")
            _packages, req_blockers = _requirements({"homekit_requirements": unit.get("requirements_packages", [])})
            if req_blockers:
                raise HostRestoreError("restore plan has invalid pinned homekit-wol dependencies")
        if unit["name"] == "cloudflared.service":
            binary = root / "usr/local/bin/cloudflared"
            _no_symlink_ancestors(binary, root)
            if not _is_real_executable(binary):
                raise HostRestoreError("provided cloudflared binary is no longer executable")
            if not re.fullmatch(r"[0-9a-f]{64}", str(unit.get("cloudflared_sha256", ""))) or _sha256(binary) != unit["cloudflared_sha256"]:
                raise HostRestoreError("provided cloudflared binary no longer matches its recorded SHA-256")
            source_content = unit.get("source_content", unit["content"])
            source_lines = _unit_lines(source_content)
            locations = _directive_locations(source_lines, "Service", "ExecStart")
            if len(locations) != 1:
                raise HostRestoreError("cloudflared must have exactly one ExecStart")
            command = shlex.split(_directive_value(source_lines[locations[0]]))
            token_file = _cloudflared_token_file(command)
            recorded_token = unit.get("facts", {}).get("token_file")
            if recorded_token is not None and token_file != recorded_token:
                raise HostRestoreError("cloudflared token-file plan does not match its reviewed unit")
            if token_file:
                _validate_cloudflared_token(_rooted(root, token_file), root)
    cron_backups: list[tuple[str, str | None]] = []
    for entry in plan["cron"]:
        user = entry["user"]
        if entry["content"] is not None:
            _validated, cron_blockers = _cron_entries(entry["content"], user, root,
                                                       set(plan.get("source_users", [])), verify_target=True)
            if cron_blockers:
                raise HostRestoreError("a cron executable or dependency is unavailable on the target")
        try:
            pwd.getpwnam(user)
        except KeyError as exc:
            raise HostRestoreError("cron target account is missing") from exc
        try:
            result = run(["crontab", "-u", user, "-l"], capture_output=True, text=True, check=False)
        except OSError as exc:
            raise HostRestoreError("crontab is required to preserve target state") from exc
        if result.returncode == 0:
            cron_backups.append((user, result.stdout))
        elif "no crontab for" in (result.stderr or "").lower():
            cron_backups.append((user, None))
        else:
            raise HostRestoreError("cannot read an existing target crontab before replacement")
    if plan["units"]:
        try:
            result = run(["systemd-analyze", "--version"], capture_output=True, text=True, check=False)
        except OSError as exc:
            raise HostRestoreError("systemd-analyze is required before unit activation") from exc
        if result.returncode:
            raise HostRestoreError("systemd-analyze is required before unit activation")
    return unit_paths, cron_backups, []


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name("." + path.name + ".restore-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _backup_crontabs(cron_backups: list[tuple[str, str | None]], root: Path) -> Path | None:
    if not cron_backups:
        return None
    migration_root = root / "var/lib/arcturus-migration"
    backup_root = root / "var/lib/arcturus-migration/host-restore"
    _no_symlink_ancestors(migration_root, root)
    migration_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if migration_root.is_symlink() or not migration_root.is_dir():
        raise HostRestoreError("migration backup root must be a real directory")
    os.chmod(migration_root, 0o700)
    _no_symlink_ancestors(backup_root, root)
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if backup_root.is_symlink() or not backup_root.is_dir():
        raise HostRestoreError("host restore backup directory must be a real directory")
    os.chmod(backup_root, 0o700)
    directory = backup_root / ("crontabs-" + uuid.uuid4().hex)
    directory.mkdir(mode=0o700)
    for user, content in cron_backups:
        data = json.dumps({"user": user, "had_crontab": content is not None, "content": content}, indent=2) + "\n"
        path = directory / (user + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
    return directory


def _apply_plan(plan: dict, target_root: Path = Path("/"), run=subprocess.run) -> dict:
    """Preflight fully, then adapt units and restore supported cron entries."""
    if os.geteuid() != 0:
        raise HostRestoreError("host restoration must run as root")
    target_root = Path(target_root)
    try:
        os_release = (target_root / "etc/os-release").read_text(encoding="utf-8")
    except OSError as exc:
        raise HostRestoreError("target OS identity is unavailable") from exc
    if not re.search(r"^ID=[\"']?almalinux[\"']?$", os_release, re.M | re.I):
        raise HostRestoreError("target must report ID=almalinux")
    unit_paths, cron_backups, _ = _preflight(plan, target_root, run=run)
    backup = _backup_crontabs(cron_backups, target_root)
    for unit in plan["units"]:
        if unit["name"] == "homekit-wol.service":
            app = target_root / "opt/homekit-wol"
            venv = app / "venv-almalinux"
            if not venv.exists():
                try:
                    _run(run, ["/usr/bin/python3", "-m", "venv", str(venv)],
                         "could not create the AlmaLinux homekit-wol virtual environment")
                    _run(run, [str(venv / "bin/python3"), "-m", "pip", "install", "--no-deps",
                               "--disable-pip-version-check", "--no-input", *unit.get("requirements_packages", [])],
                         "could not install pinned homekit-wol requirements")
                except BaseException:
                    # This path was proven absent during complete preflight, so
                    # only a venv created by this apply attempt can be removed.
                    shutil.rmtree(venv, ignore_errors=True)
                    raise
    for unit, path in zip(plan["units"], unit_paths):
        _atomic_write(path, unit["content"])
    if unit_paths:
        command = ["systemd-analyze", "verify", "--root=" + str(target_root), *map(str, unit_paths)]
        _run(run, command, "systemd-analyze rejected a restored service definition; services remain untouched")
    _run(run, ["systemctl", "daemon-reload"], "systemd daemon-reload failed")
    for unit in plan["units"]:
        name = unit["name"]
        if unit["enablement"] == "enabled":
            _run(run, ["systemctl", "enable", name], "could not restore service enablement")
        elif unit["enablement"] == "disabled":
            _run(run, ["systemctl", "disable", name], "could not restore disabled service state")
        _run(run, ["systemctl", "start" if unit["active"] else "stop", name], "could not restore source service activity")
    for item in plan.get("linger", []):
        _run(run, ["loginctl", "enable-linger" if item["enabled"] else "disable-linger", item["user"]],
             "could not restore source user lingering state")
    restored_cron = []
    backed_up_cron = dict(cron_backups)
    for entry in plan["cron"]:
        if entry["content"] is None:
            if backed_up_cron.get(entry["user"]) is not None:
                result = run(["crontab", "-r", "-u", entry["user"]], capture_output=True, text=True, check=False)
                if result.returncode:
                    raise HostRestoreError("could not remove a target crontab absent from the source; host backup is retained")
            continue
        result = run(["crontab", "-u", entry["user"], "-"], input=entry["content"],
                     capture_output=True, text=True, check=False)
        if result.returncode:
            raise HostRestoreError("could not restore a validated crontab; host backup is retained")
        restored_cron.append(entry["user"])
    return {
        "status": "restored",
        "services": [{"name": unit["name"], "enablement": unit["enablement"], "active_state": unit["active_state"]}
                     for unit in plan["units"]],
        "cron_users": restored_cron,
        "lingering_users": [item["user"] for item in plan.get("linger", []) if item["enabled"]],
        "cron_backup": str(backup) if backup else None,
        "excluded_units": plan["excluded_units"],
        "limitations": plan["limitations"],
    }


def apply_plan(plan_path: Path, run=subprocess.run) -> dict:
    """Read a protected plan and apply it only on the root AlmaLinux host."""
    if os.geteuid() != 0:
        raise HostRestoreError("host restoration must run as root")
    try:
        info = Path(plan_path).lstat()
        if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise HostRestoreError("restore plan must be a protected root-owned regular file")
        plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HostRestoreError("restore plan is not readable protected JSON") from exc
    if not isinstance(plan, dict) or plan.get("schemaVersion") != 1:
        raise HostRestoreError("unsupported host restore plan")
    return _apply_plan(plan, Path("/"), run=run)


def _write_plan(path: Path, plan: dict) -> None:
    path = Path(path)
    if path.exists() or path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir():
        raise HostRestoreError("plan output must be a new file in an existing real directory")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise HostRestoreError("could not create protected restore plan") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(plan, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _run(run, argv: list[str], message: str) -> None:
    try:
        result = run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise HostRestoreError(message) from exc
    if result.returncode:
        raise HostRestoreError(message)


def _summary(plan: dict) -> dict:
    return {
        "ready": plan["ready"],
        "services": [{key: unit[key] for key in ("name", "enablement", "active_state", "facts")} for unit in plan["units"]],
        "cron_users": [entry["user"] for entry in plan["cron"] if entry["content"] is not None],
        "excluded_units": plan["excluded_units"],
        "blockers": plan["blockers"],
        "limitations": plan["limitations"],
        "target_dependencies_pending": plan.get("target_dependencies_pending", []),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    planning = commands.add_parser("plan", help="validate inventory and write a protected plan")
    planning.add_argument("inventory", type=Path)
    planning.add_argument("dependencies", type=Path, help="protected dependencies.json containing pinned dependencies")
    planning.add_argument("plan_path", type=Path)
    planning.add_argument("--defer-target-checks", action="store_true",
                          help="create a portable plan before the AlmaLinux target files are deployed")
    applying = commands.add_parser("apply", help="apply a protected plan on AlmaLinux")
    applying.add_argument("plan_path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            protected = {}
            for key, path in (("inventory", args.inventory), ("dependencies", args.dependencies)):
                info = path.lstat()
                if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                    raise HostRestoreError(f"{key} must be a protected regular file")
                protected[key] = json.loads(path.read_text(encoding="utf-8"))
            plan = build_plan(protected["inventory"], protected["dependencies"],
                              verify_target=not args.defer_target_checks)
            _write_plan(args.plan_path, plan)
            print(json.dumps(_summary(plan), indent=2))
            return 0 if plan["ready"] else 2
        result = apply_plan(args.plan_path)
    except (HostRestoreError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
