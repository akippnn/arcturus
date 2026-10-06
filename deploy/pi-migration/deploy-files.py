#!/usr/bin/env python3
"""Deploy a validated portable filesystem bundle on a new AlmaLinux host.

This helper is intentionally narrow: it deploys selected paths, keeps a
root-only GNU tar backup of existing destinations, restores SELinux labels,
and preserves the bootstrap SSH key for the selected owner. It does not
install packages or activate services.
"""
from __future__ import annotations

import argparse
import base64
import grp
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import stat
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pi_migration_files as portable_files


class DeployFilesError(RuntimeError):
    """A restore plan, target, or deployment step was unsafe or incomplete."""


_TARGET_ROOT = Path("/")
_MIGRATION_ROOT = Path("/var/lib/arcturus-migration")
_RESERVED = "/var/lib/arcturus-migration"
_KEY_TYPE = re.compile(
    r"^(?:ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-[A-Za-z0-9-]+|"
    r"sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _read_plan(plan_path: Path) -> dict:
    try:
        plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeployFilesError("restore plan is not readable UTF-8 JSON") from exc
    if not isinstance(plan, dict) or plan.get("schemaVersion") != 1:
        raise DeployFilesError("unsupported restore plan")
    try:
        selected = portable_files._selected_paths(plan.get("selected_paths"))
    except portable_files.FilesMigrationError as exc:
        raise DeployFilesError("restore plan contains invalid selected paths") from exc
    for path in selected:
        if path == _RESERVED or path.startswith(_RESERVED + "/") or _RESERVED.startswith(path.rstrip("/") + "/"):
            raise DeployFilesError("restore plan overlaps the migration backup directory")
    digest = plan.get("bundle_sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise DeployFilesError("restore plan has an invalid bundle SHA-256")
    return {**plan, "selected_paths": selected, "bundle_sha256": digest}


def _key_line(value: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise DeployFilesError("bootstrap public key must be one OpenSSH public-key line")
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if "\n" in value or "\r" in value:
        raise DeployFilesError("bootstrap public key must be one OpenSSH public-key line")
    fields = value.strip().split()
    if len(fields) < 2 or not _KEY_TYPE.fullmatch(fields[0]):
        raise DeployFilesError("bootstrap public key must be one OpenSSH public-key line")
    try:
        decoded = base64.b64decode(fields[1], validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise DeployFilesError("bootstrap public key has invalid base64 data") from exc
    if not decoded:
        raise DeployFilesError("bootstrap public key has empty key data")
    return " ".join(fields)


def _safe_name(value: str) -> bool:
    return bool(isinstance(value, str) and re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}\$?", value))


def _target_path(relative: str) -> Path:
    return _TARGET_ROOT / PurePosixPath(relative)


def _kind_from_stat(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "regular"
    return "other"


def _expected_kind(record: dict) -> str:
    kind = record["kind"]
    return "regular" if kind == "hardlink" else kind


def _check_existing_ancestors(relative: str) -> None:
    current = _TARGET_ROOT
    try:
        root_stat = current.lstat()
    except OSError as exc:
        raise DeployFilesError("target root is unavailable") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise DeployFilesError("target root must be a real directory")
    parts = PurePosixPath(relative).parts
    for part in parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DeployFilesError("cannot inspect a selected path ancestor") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise DeployFilesError("selected path has a symlink or non-directory live ancestor")


def _preflight_destinations(scan: dict, owner: pwd.struct_passwd) -> list[str]:
    records = scan["records"]
    selected_members = scan["selected_members"]
    # All ownership IDs must resolve on the destination before tar can alter it.
    for name in selected_members:
        record = records[name]
        try:
            pwd.getpwuid(record["uid"])
            grp.getgrgid(record["gid"])
        except KeyError as exc:
            raise DeployFilesError("bundle contains numeric ownership unknown to the target") from exc
        if record["kind"] == "other":
            raise DeployFilesError("bundle contains an unsupported special file")
        for ancestor in portable_files._ancestor_paths(name):
            ancestor_record = records.get(ancestor)
            if ancestor_record and ancestor_record["kind"] != "directory":
                raise DeployFilesError("bundle contains a non-directory selected-path ancestor")

    # Validate every live ancestor and exact destination type before any write.
    for name in selected_members:
        _check_existing_ancestors(name)
        destination = _target_path(name)
        try:
            existing = destination.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DeployFilesError("cannot inspect a selected live destination") from exc
        wanted = _expected_kind(records[name])
        actual = _kind_from_stat(existing.st_mode)
        if actual != wanted:
            raise DeployFilesError("selected live destination has an incompatible type")

    home = Path(owner.pw_dir)
    if not home.is_absolute() or ".." in home.parts or home == Path("/"):
        raise DeployFilesError("owner home directory is not a safe absolute path")
    home_relative = home.relative_to(_TARGET_ROOT).as_posix() if home.is_relative_to(_TARGET_ROOT) else None
    if home_relative is None:
        raise DeployFilesError("owner home directory is outside the deployment root")
    _check_existing_ancestors((home / ".ssh" / "authorized_keys").relative_to(_TARGET_ROOT).as_posix())
    try:
        home_info = home.lstat()
    except OSError as exc:
        raise DeployFilesError("owner home directory must already exist on the target") from exc
    if not stat.S_ISDIR(home_info.st_mode) or home_info.st_uid != owner.pw_uid:
        raise DeployFilesError("existing owner home has changed type or ownership")
    ssh_dir = home / ".ssh"
    try:
        ssh_info = ssh_dir.lstat()
    except FileNotFoundError:
        ssh_info = None
    except OSError as exc:
        raise DeployFilesError("cannot inspect owner SSH directory") from exc
    if ssh_info is not None and not stat.S_ISDIR(ssh_info.st_mode):
        raise DeployFilesError("owner SSH path is not a real directory")
    authorized = ssh_dir / "authorized_keys"
    try:
        key_info = authorized.lstat()
    except FileNotFoundError:
        key_info = None
    except OSError as exc:
        raise DeployFilesError("cannot inspect owner authorized_keys") from exc
    if key_info is not None and not stat.S_ISREG(key_info.st_mode):
        raise DeployFilesError("owner authorized_keys is not a regular file")

    # If the bundle explicitly supplies the owner's home directory, it must
    # retain the cloud-init account's numeric identity.
    home_record = records.get(home_relative)
    if home_record and home_record["kind"] == "directory" and (
        home_record["uid"] != owner.pw_uid or home_record["gid"] != owner.pw_gid
    ):
        raise DeployFilesError("bundle would change the existing owner home identity")
    ssh_relative = (home / ".ssh").relative_to(_TARGET_ROOT).as_posix()
    authorized_relative = (home / ".ssh" / "authorized_keys").relative_to(_TARGET_ROOT).as_posix()
    if ssh_relative in records and records[ssh_relative]["kind"] != "directory":
        raise DeployFilesError("bundle would replace the owner SSH directory with a non-directory")
    if authorized_relative in records and records[authorized_relative]["kind"] != "regular":
        raise DeployFilesError("bundle authorized_keys entry must be a regular file")

    existing: list[str] = []
    for path in scan["selected_paths"] if "selected_paths" in scan else []:
        candidate = _target_path(path.lstrip("/"))
        if candidate.exists() or candidate.is_symlink():
            existing.append(path.lstrip("/"))
    # Avoid adding the same live files to the safety archive more than once.
    existing.sort(key=lambda p: (p.count("/"), p))
    roots: list[str] = []
    for path in existing:
        if not any(path == parent or path.startswith(parent + "/") for parent in roots):
            roots.append(path)
    return roots


def _ensure_migration_root() -> None:
    try:
        relative = _MIGRATION_ROOT.relative_to(_TARGET_ROOT)
    except ValueError as exc:
        raise DeployFilesError("migration backup directory is outside the target root") from exc
    current = _TARGET_ROOT
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
        except OSError as exc:
            raise DeployFilesError("cannot inspect migration backup directory") from exc
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise DeployFilesError("migration backup ancestors must be root-owned and not group/world writable")
    os.chmod(_MIGRATION_ROOT, 0o700)


def _run_checked(argv: list[str], message: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise DeployFilesError(message) from exc
    if result.returncode:
        raise DeployFilesError(message)
    return result


def _ensure_bootstrap_key(owner: pwd.struct_passwd, public_key: str) -> None:
    home = Path(owner.pw_dir)
    ssh_dir = home / ".ssh"
    try:
        ssh_dir.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if ssh_dir.is_symlink() or not ssh_dir.is_dir():
        raise DeployFilesError("owner SSH directory changed during deployment")
    os.chown(ssh_dir, owner.pw_uid, owner.pw_gid)
    os.chmod(ssh_dir, 0o700)
    authorized = ssh_dir / "authorized_keys"
    if authorized.is_symlink():
        raise DeployFilesError("owner authorized_keys changed to a symlink during deployment")
    if authorized.exists():
        if not authorized.is_file():
            raise DeployFilesError("owner authorized_keys changed type during deployment")
        content = authorized.read_text(encoding="utf-8")
    else:
        content = ""
    key_pair = " ".join(public_key.split()[:2])
    present = any(" ".join(line.split()[:2]) == key_pair for line in content.splitlines() if line.strip())
    if not present:
        with authorized.open("a", encoding="utf-8") as stream:
            if content and not content.endswith("\n"):
                stream.write("\n")
            stream.write(public_key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    os.chown(authorized, owner.pw_uid, owner.pw_gid)
    os.chmod(authorized, 0o600)


def deploy(plan_path: Path, bundle_path: Path, authorized_public_key: str, owner_name: str) -> dict:
    """Validate, back up, and deploy selected paths on an AlmaLinux target."""
    if os.geteuid() != 0:
        raise DeployFilesError("deployment must run as root")
    if portable_files._os_id().lower() != "almalinux":
        raise DeployFilesError("deployment target must report ID=almalinux")
    if not _safe_name(owner_name) or owner_name == "root":
        raise DeployFilesError("owner must be a non-root local account name")
    try:
        owner = pwd.getpwnam(owner_name)
    except KeyError as exc:
        raise DeployFilesError("owner account does not exist on the target") from exc
    if owner.pw_uid == 0 or owner.pw_gid < 0:
        raise DeployFilesError("owner account has an invalid numeric identity")
    public_key = _key_line(authorized_public_key)
    plan = _read_plan(Path(plan_path))
    bundle_path = Path(bundle_path)
    if plan.get("bundle") is not None and plan["bundle"] != bundle_path.name:
        raise DeployFilesError("bundle filename does not match the restore plan")
    try:
        bundle_matches = (not bundle_path.is_symlink() and bundle_path.is_file()
                          and portable_files.sha256_file(bundle_path) == plan["bundle_sha256"])
    except (OSError, portable_files.FilesMigrationError, portable_files.MigrationArchiveError) as exc:
        raise DeployFilesError("bundle is missing or could not be verified") from exc
    if not bundle_matches:
        raise DeployFilesError("bundle is missing or does not match the plan SHA-256")
    try:
        scan = portable_files.validate_bundle(bundle_path, plan["selected_paths"])
    except portable_files.FilesMigrationError as exc:
        raise DeployFilesError("bundle failed selected-path validation") from exc
    scan["selected_paths"] = plan["selected_paths"]
    existing = _preflight_destinations(scan, owner)

    # Confirm both required system tools before creating any persistent data.
    tar_version = _run_checked(["tar", "--version"], "GNU tar is required for metadata-preserving deployment")
    if "GNU tar" not in tar_version.stdout:
        raise DeployFilesError("GNU tar is required for metadata-preserving deployment")
    _run_checked(["restorecon", "-n", "--", owner.pw_dir], "restorecon must be able to inspect target labels before deployment")

    _ensure_migration_root()
    backup_path = _MIGRATION_ROOT / ("pre-restore-" + uuid.uuid4().hex + ".tar")
    if existing:
        backup_partial = backup_path.with_suffix(".tar.partial")
        backup_partial_fd = os.open(backup_partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(backup_partial_fd)
        backup_command = ["tar", "--numeric-owner", "--acls", "--xattrs", "-cpf", str(backup_partial),
                          "-C", str(_TARGET_ROOT), "--", *existing]
        try:
            _run_checked(backup_command, "could not back up existing selected paths; deployment stopped")
            os.chmod(backup_partial, 0o600)
            os.replace(backup_partial, backup_path)
        except BaseException:
            backup_partial.unlink(missing_ok=True)
            raise

    extraction = ["tar", "--numeric-owner", "--acls", "--xattrs", "--xattrs-exclude=security.selinux",
                  "-xpf", str(bundle_path), "-C", str(_TARGET_ROOT)]
    _run_checked(extraction, "GNU tar deployment failed; pre-restore backup is retained")
    selected_absolute = ["/" + path.lstrip("/") for path in plan["selected_paths"]]
    _run_checked(["restorecon", "-RF", "--", *selected_absolute], "restorecon failed; pre-restore backup is retained")
    _ensure_bootstrap_key(owner, public_key)
    return {
        "status": "deployed",
        "backup": str(backup_path) if existing else None,
        "selected_paths": plan["selected_paths"],
        "bootstrap_key": "preserved",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("public_key_file", type=Path)
    parser.add_argument("owner")
    args = parser.parse_args(argv)
    try:
        public_key = args.public_key_file.read_text(encoding="utf-8")
        result = deploy(args.plan, args.bundle, public_key, args.owner)
    except (DeployFilesError, OSError, UnicodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
