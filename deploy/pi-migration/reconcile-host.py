#!/usr/bin/env python3
"""Reconcile only unfinished host unit states after a verified partial restore.

Existing transformed units, dependencies, cron and linger state must already
match the protected plan. This helper never deploys data or rebuilds a venv.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import types

sys.dont_write_bytecode = True


class ReconciliationError(RuntimeError):
    pass


SUPPORTED = {
    "homekit-wol.service", "thermal-governor.service", "gaming-sqm.service",
    "cloudflared.service", "cloudflared-update.service", "cloudflared-update.timer",
}
ACTIVITY = {"active", "inactive", "failed", "activating", "deactivating", "reloading"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_chain(path: Path, *, root: Path = Path("/")) -> None:
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise ReconciliationError("protected path escapes its root") from exc
    current = root
    for part in (None, *parts):
        if part is not None:
            current /= part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ReconciliationError("protected path has an unsafe directory chain")


def _regular_bytes(path: Path, *, private=False, limit=1024 * 1024) -> bytes:
    info = path.lstat()
    forbidden = 0o077 if private else 0o022
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_mode & forbidden or not 0 < info.st_size <= limit):
        raise ReconciliationError("protected input is not a bounded root-owned regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
            raise ReconciliationError("protected input changed during inspection")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(limit + 1)
        if len(data) != info.st_size:
            raise ReconciliationError("protected input changed during inspection")
        return data
    finally:
        os.close(fd)


def _pinned_input(path: Path, digest: str, *, private=False) -> bytes:
    if not path.is_absolute() or any(part in (".", "..") for part in path.parts):
        raise ReconciliationError("protected input path must be absolute and normalized")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ReconciliationError("protected input requires an exact SHA-256 pin")
    _safe_chain(path.parent)
    data = _regular_bytes(path, private=private)
    if _sha(data) != digest:
        raise ReconciliationError("protected input differs from its SHA-256 pin")
    return data


def _read_state(run, verb: str, name: str) -> str:
    result = run(["systemctl", verb, name], capture_output=True, text=True,
                 check=False, timeout=30)
    value = result.stdout.strip()
    allowed = ACTIVITY if verb == "is-active" else {"enabled", "disabled", "static"}
    if value not in allowed:
        raise ReconciliationError("unit state is unavailable or unsupported: " + name)
    return value


def _validate_plan(plan: dict) -> None:
    if (not isinstance(plan, dict) or plan.get("schemaVersion") != 1
            or plan.get("ready") is not True or not isinstance(plan.get("units"), list)
            or not isinstance(plan.get("cron"), list) or not isinstance(plan.get("linger", []), list)):
        raise ReconciliationError("unsupported or incomplete host plan")
    names = set()
    for item in plan["units"]:
        name = item.get("name")
        if (name not in SUPPORTED or name in names or type(item.get("active")) is not bool
                or item.get("enablement") not in {"enabled", "disabled", "static"}
                or not isinstance(item.get("content"), str) or not item["content"]):
            raise ReconciliationError("host plan has an unsupported unit")
        names.add(name)


def _exact_artifacts(plan: dict, root: Path, adapter, run) -> None:
    unit_dir = root / "etc/systemd/system"
    _safe_chain(unit_dir, root=root)
    for item in plan["units"]:
        path = unit_dir / item["name"]
        if _regular_bytes(path) != item["content"].encode("utf-8"):
            raise ReconciliationError("unit must already contain exact transformed bytes: " + item["name"])
        if item["name"] == "homekit-wol.service":
            venv = root / "opt/homekit-wol/venv-almalinux"
            adapter._no_symlink_ancestors(venv, root)
            if not venv.is_dir() or not (venv / "bin/python3").is_file():
                raise ReconciliationError("existing validated HomeKit venv is required")
    # The current adapter supplies all dependency checks, including the token.
    _paths, current_cron, _ = adapter._preflight(plan, root, run=run)
    expected_cron = [(entry["user"], entry["content"]) for entry in plan["cron"]]
    if current_cron != expected_cron:
        raise ReconciliationError("current cron differs from the source; reconciliation never writes cron")
    for item in plan.get("linger", []):
        user = item.get("user")
        if (not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]*[$]?", user)
                or type(item.get("enabled")) is not bool):
            raise ReconciliationError("host plan has an invalid linger entry")
        path = root / "var/lib/systemd/linger" / user
        adapter._no_symlink_ancestors(path, root)
        present = path.exists() or path.is_symlink()
        if present:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ReconciliationError("current linger marker is unsafe")
        if present != item["enabled"]:
            raise ReconciliationError("current linger differs from the source; reconciliation never edits linger")


def _reconcile_plan(plan: dict, root: Path, adapter, run=subprocess.run) -> dict:
    if os.geteuid() != 0:
        raise ReconciliationError("host reconciliation must run as root")
    original_run = run
    def run(command, **kwargs):
        # Even dependency inspection must not create Python cache files.
        kwargs["env"] = dict(kwargs.get("env") or os.environ,
                             PYTHONDONTWRITEBYTECODE="1")
        return original_run(command, **kwargs)
    _validate_plan(plan)
    os_release = (root / "etc/os-release").read_text(encoding="utf-8")
    if not re.search(r"^ID=[\"']?almalinux[\"']?$", os_release, re.M | re.I):
        raise ReconciliationError("target must report ID=almalinux")
    _exact_artifacts(plan, root, adapter, run)
    if plan["units"]:
        result = run(["systemd-analyze", "verify", "--root=" + str(root),
                      *(str(root / "etc/systemd/system" / unit["name"]) for unit in plan["units"])],
                     capture_output=True, text=True, check=False, timeout=60)
        if result.returncode:
            raise ReconciliationError("existing transformed units failed systemd verification")
    actions = []
    before = []
    for item in plan["units"]:
        name = item["name"]
        activity = _read_state(run, "is-active", name)
        enabled = _read_state(run, "is-enabled", name)
        before.append({"name": name, "activity": activity, "enablement": enabled})
        desired = item["enablement"]
        if desired == "static" and enabled != "static":
            raise ReconciliationError("static unit enablement requires separate reconciliation: " + name)
        if desired != enabled:
            actions.append({"unit": name, "action": "enable" if desired == "enabled" else "disable"})
        if item["active"] and activity != "active":
            actions.append({"unit": name, "action": "start"})
        elif not item["active"] and activity != "inactive":
            actions.append({"unit": name, "action": "stop"})
    # No command below runs until every artifact and every unit state passes.
    for action in actions:
        # Re-read to avoid starting/stopping a unit already reconciled externally.
        verb, name = action["action"], action["unit"]
        query = "is-enabled" if verb in {"enable", "disable"} else "is-active"
        state = _read_state(run, query, name)
        desired = {"enable": "enabled", "disable": "disabled", "start": "active", "stop": "inactive"}[verb]
        if state == desired:
            action["executed"] = False
            continue
        result = run(["systemctl", verb, name], capture_output=True, text=True, check=False, timeout=120)
        if result.returncode:
            raise ReconciliationError("host unit state reconciliation failed: " + name)
        action["executed"] = True
    _exact_artifacts(plan, root, adapter, run)
    services = []
    for item in plan["units"]:
        activity = _read_state(run, "is-active", item["name"])
        enabled = _read_state(run, "is-enabled", item["name"])
        if activity != ("active" if item["active"] else "inactive") or enabled != item["enablement"]:
            raise ReconciliationError("host unit did not reach its planned state: " + item["name"])
        services.append({"name": item["name"], "enablement": enabled, "active_state": activity})
    return {"status": "restored", "services": services,
            "cron_users": [item["user"] for item in plan["cron"] if item["content"] is not None],
            "lingering_users": [item["user"] for item in plan.get("linger", []) if item["enabled"]],
            "cron_backup": None, "excluded_units": plan.get("excluded_units", []),
            "limitations": plan.get("limitations", []),
            "reconciliation": {"mode": "unit-states-only", "before": before, "actions": actions,
                               "artifacts": "exact-transformed-units-and-existing-dependencies",
                               "cron": "already-exact", "linger": "already-exact"}}


def apply_plan(plan_path: Path, adapter_path: Path, *, plan_sha256: str,
               adapter_sha256: str, run=subprocess.run) -> dict:
    if os.geteuid() != 0:
        raise ReconciliationError("host reconciliation must run as root")
    plan_data = _pinned_input(plan_path, plan_sha256, private=True)
    adapter_data = _pinned_input(adapter_path, adapter_sha256)
    plan = json.loads(plan_data)
    adapter = types.ModuleType("arcturus_reconciliation_adapter")
    adapter.__file__ = str(adapter_path)
    exec(compile(adapter_data, str(adapter_path), "exec"), adapter.__dict__)
    result = _reconcile_plan(plan, Path("/"), adapter, run=run)
    result["reconciliation"].update(plan_sha256=plan_sha256, adapter_sha256=adapter_sha256)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["apply"])
    parser.add_argument("plan_path", type=Path)
    parser.add_argument("adapter_path", type=Path)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--adapter-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        result = apply_plan(args.plan_path, args.adapter_path, plan_sha256=args.plan_sha256,
                            adapter_sha256=args.adapter_sha256)
    except (ReconciliationError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        # Adapter dependency diagnostics must not expose the protected plan.
        print("current host adapter preflight refused reconciliation", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
