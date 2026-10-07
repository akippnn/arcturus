#!/usr/bin/env python3
"""Build and safely stage a selected portable filesystem restore bundle.

Bundles contain selected entries from a full Debian rootfs archive, retain
PAX metadata, and exclude container runtime stores. ``apply`` validates and
extracts to a new root-only staging directory on AlmaLinux; it does not deploy
files or activate units.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
import zlib
from pathlib import Path
from typing import Any

from pi_migration_archive import MigrationArchiveError, validate_restore_paths


class FilesMigrationError(RuntimeError):
    """An unsafe, invalid, or incomplete filesystem restore bundle."""


_MIGRATION_ROOT = Path("/var/lib/arcturus-migration")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUNTIME_SUFFIXES = (
    ".local/share/containers/storage",
    ".local/share/docker",
)
_CLOUDFLARED_TOKEN = "/etc/cloudflared/token"


def _selected_paths(paths: list[str]) -> list[str]:
    if isinstance(paths, list) and any(path in ("/etc/systemd/system", "/etc/containers") for path in paths if isinstance(path, str)):
        raise FilesMigrationError("select configuration files or snippets individually")
    try:
        selected = validate_restore_paths(paths)
    except MigrationArchiveError as exc:
        raise FilesMigrationError(str(exc)) from exc
    if len(set(selected)) != len(selected):
        raise FilesMigrationError("restore paths must not contain duplicates")
    for path in selected:
        if path.startswith("/etc/systemd/system/"):
            tail = path.removeprefix("/etc/systemd/system/")
            if "/" in tail or not re.fullmatch(r"[A-Za-z0-9_.@-]+\.(service|timer)", tail):
                raise FilesMigrationError("select systemd service or timer files individually")
        if path in ("/etc/containers", "/etc/systemd/system"):
            raise FilesMigrationError("select configuration files or snippets individually")
    return selected


def _archive_path(name: str, *, allow_root: bool = False) -> str:
    if not isinstance(name, str) or not name or name.startswith("/") or "\x00" in name:
        raise FilesMigrationError("archive contains an absolute, empty, or invalid member name")
    parts = name.split("/")
    while parts and parts[0] == ".":
        parts.pop(0)
    # Tar directory headers commonly have one final slash.
    while parts and parts[-1] == "":
        parts.pop()
    if any(part in ("", ".", "..") for part in parts):
        raise FilesMigrationError("archive member path traverses outside its root")
    result = "/".join(parts)
    if not result and not allow_root:
        raise FilesMigrationError("archive contains an empty member path")
    try:
        result.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FilesMigrationError("archive member path is not valid UTF-8") from exc
    return result


def _link_target(name: str, target: str, *, hardlink: bool) -> str:
    if not isinstance(target, str) or not target or "\x00" in target:
        raise FilesMigrationError("archive contains an empty or invalid link target")
    if hardlink:
        return _archive_path(target)
    if target.startswith("/"):
        return target
    stack = [] if hardlink else [part for part in name.rsplit("/", 1)[0].split("/") if part]
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                raise FilesMigrationError("relative symlink escapes the archive root")
            stack.pop()
        else:
            stack.append(part)
    return "/".join(stack)


def _under_selection(name: str, selected: list[str]) -> bool:
    return any(name == path.lstrip("/") or name.startswith(path.lstrip("/") + "/") for path in selected)


def _runtime_storage(name: str) -> bool:
    if name in ("var/lib/docker", "var/lib/containers/storage", "root/.local/share/containers/storage", "root/.local/share/docker"):
        return True
    if name.startswith("var/lib/docker/") or name.startswith("var/lib/containers/storage/"):
        return True
    if name.startswith("root/"):
        relative = name[5:]
        return any(relative == suffix or relative.startswith(suffix + "/") for suffix in _RUNTIME_SUFFIXES)
    if name.startswith("home/"):
        pieces = name.split("/")
        if len(pieces) >= 3:
            relative = "/".join(pieces[2:])
            return any(relative == suffix or relative.startswith(suffix + "/") for suffix in _RUNTIME_SUFFIXES)
    return False


def _ancestor_paths(name: str) -> list[str]:
    parts = name.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _scan_archive(path: Path, selected: list[str]) -> dict[str, Any]:
    records: dict[str, dict[str, Any]] = {}
    found: set[str] = set()
    excluded = 0
    counts = {"regular": 0, "directory": 0, "symlink": 0, "hardlink": 0, "other": 0}
    uid_set: set[int] = set()
    gid_set: set[int] = set()
    try:
        with tarfile.open(path, mode="r|gz") as archive:
            for member in archive:
                name = _archive_path(member.name, allow_root=member.isdir())
                if not name:
                    continue
                if name in records:
                    raise FilesMigrationError("archive contains a duplicate member path")
                kind = ("symlink" if member.issym() else "hardlink" if member.islnk() else
                        "regular" if member.isfile() else "directory" if member.isdir() else "other")
                link_target = None
                if member.issym():
                    link_target = _link_target(name, member.linkname, hardlink=False)
                elif member.islnk():
                    link_target = _link_target(name, member.linkname, hardlink=True)
                records[name] = {"kind": kind, "target": link_target, "size": member.size,
                                 "uid": int(member.uid), "gid": int(member.gid), "mode": member.mode}
                if not _under_selection(name, selected):
                    continue
                found.update(path for path in selected if name == path.lstrip("/") or name.startswith(path.lstrip("/") + "/"))
                if _runtime_storage(name):
                    excluded += 1
                    continue
                if kind == "other":
                    raise FilesMigrationError("selected paths contain an unsupported special file")
                counts[kind] += 1
                uid_set.add(int(member.uid))
                gid_set.add(int(member.gid))
    except FilesMigrationError:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise FilesMigrationError("rootfs archive is invalid or truncated") from exc

    missing = [path for path in selected if path not in found]
    if missing:
        raise FilesMigrationError("selected path is absent from rootfs archive: " + missing[0])

    symlinks = {name for name, record in records.items() if record["kind"] == "symlink"}
    selected_members = {name for name in records if _under_selection(name, selected) and not _runtime_storage(name)}
    # Include only real ancestor directory headers needed for this exact file;
    # selecting /etc/cloudflared itself would copy unrelated credentials.
    if _CLOUDFLARED_TOKEN in selected:
        token_name = _CLOUDFLARED_TOKEN.lstrip("/")
        for ancestor in _ancestor_paths(token_name):
            record = records.get(ancestor)
            if ancestor in ("etc", "etc/cloudflared") and (record is None or record["kind"] != "directory"):
                raise FilesMigrationError("cloudflared token parent must be an archived real directory")
            if record is not None and record["kind"] == "directory" and ancestor not in selected_members:
                selected_members.add(ancestor)
                counts["directory"] += 1
                uid_set.add(record["uid"])
                gid_set.add(record["gid"])
    # GNU tar must never follow an archived symlink while extracting a selected
    # descendant, including a symlink outside the selected subtree.
    for path in selected:
        if any(ancestor in symlinks for ancestor in _ancestor_paths(path.lstrip("/"))):
            raise FilesMigrationError("selected path traverses an archived symlink ancestor")
    for name in selected_members:
        if any(ancestor in symlinks for ancestor in _ancestor_paths(name)):
            raise FilesMigrationError("selected archive member traverses an archived symlink ancestor")

    def regular_hardlink_target(name: str, active: set[str] | None = None) -> str:
        active = set() if active is None else active
        if name in active:
            raise FilesMigrationError("archive contains a hardlink cycle")
        record = records.get(name)
        if record is None:
            raise FilesMigrationError("archive hardlink target is missing")
        if record["kind"] == "regular":
            return name
        if record["kind"] != "hardlink":
            raise FilesMigrationError("archive hardlink target is not a regular file")
        if any(ancestor in symlinks for ancestor in _ancestor_paths(name)):
            raise FilesMigrationError("archive hardlink target traverses a symlink")
        active.add(name)
        return regular_hardlink_target(record["target"], active)

    hardlink_payloads: dict[str, str] = {}
    for name in selected_members:
        record = records[name]
        if record["kind"] != "hardlink":
            continue
        target = record["target"]
        if target not in records:
            raise FilesMigrationError("archive hardlink target is missing")
        if any(ancestor in symlinks for ancestor in _ancestor_paths(target)):
            raise FilesMigrationError("archive hardlink target traverses a symlink")
        regular_target = regular_hardlink_target(target)
        if _runtime_storage(regular_target):
            raise FilesMigrationError("selected hardlink targets excluded container runtime storage")
        if target not in selected_members:
            hardlink_payloads[name] = regular_target
    return {"records": records, "selected_members": selected_members, "hardlink_payloads": hardlink_payloads,
            "excluded": excluded, "type_counts": counts, "numeric_ids": {"uids": sorted(uid_set), "gids": sorted(gid_set)}}


def _copy_stream(source, destination) -> None:
    shutil.copyfileobj(source, destination, 1024 * 1024)


def _clone_info(member: tarfile.TarInfo, name: str) -> tarfile.TarInfo:
    info = copy.copy(member)
    info.name = name
    info.pax_headers = dict(member.pax_headers)
    info.pax_headers.pop("path", None)
    info.pax_headers.pop("linkpath", None)
    if len(name.encode("utf-8")) > 100 or any(ord(ch) > 127 for ch in name):
        info.pax_headers["path"] = name
    if info.islnk():
        target = _link_target(name, member.linkname, hardlink=True)
        info.linkname = target
        if len(target.encode("utf-8")) > 100 or any(ord(ch) > 127 for ch in target):
            info.pax_headers["linkpath"] = target
    elif info.issym():
        info.linkname = member.linkname
        if len(info.linkname.encode("utf-8")) > 100 or any(ord(ch) > 127 for ch in info.linkname):
            info.pax_headers["linkpath"] = info.linkname
    return info


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_gzip(path: Path) -> None:
    try:
        with gzip.open(path, "rb") as stream:
            for _chunk in iter(lambda: stream.read(1024 * 1024), b""):
                pass
    except (OSError, EOFError, zlib.error) as exc:
        raise FilesMigrationError("archive is invalid or has a damaged gzip trailer") from exc


def build_bundle(rootfs_tar: Path, destination: Path, paths: list[str]) -> dict[str, Any]:
    """Create a selected-entry gzip tar bundle from a full rootfs backup."""
    rootfs_tar, destination = Path(rootfs_tar), Path(destination)
    selected = _selected_paths(paths)
    if rootfs_tar.is_symlink() or not rootfs_tar.is_file():
        raise FilesMigrationError("rootfs backup must be a regular local file")
    _verify_gzip(rootfs_tar)
    if destination.exists() or destination.is_symlink():
        raise FilesMigrationError("bundle destination already exists")
    if destination.parent.is_symlink() or not destination.parent.is_dir():
        raise FilesMigrationError("bundle parent must be an existing real directory")
    rootfs_digest = sha256_file(rootfs_tar)
    scan = _scan_archive(rootfs_tar, selected)
    selected_members: set[str] = scan["selected_members"]
    temp_root = tempfile.TemporaryDirectory(prefix="pi-files-bundle-")
    temp_path = Path(temp_root.name)
    payload_files: dict[str, Path] = {}
    try:
        # Hard links whose targets are outside the selected output need their
        # target content copied as a regular file. Spool only those payloads.
        wanted_targets = set(scan["hardlink_payloads"].values())
        if wanted_targets:
            with tarfile.open(rootfs_tar, mode="r|gz") as archive:
                for member in archive:
                    name = _archive_path(member.name, allow_root=member.isdir())
                    if name not in wanted_targets:
                        continue
                    if not member.isfile():
                        raise FilesMigrationError("hardlink payload is not a regular archive member")
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise FilesMigrationError("cannot read hardlink payload")
                    spool = temp_path / hashlib.sha256(name.encode("utf-8")).hexdigest()
                    with spool.open("wb") as output:
                        _copy_stream(extracted, output)
                    payload_files[name] = spool
        if set(payload_files) != wanted_targets:
            raise FilesMigrationError("archive hardlink payload was not found")

        partial_path = destination.parent / ("." + destination.name + ".partial-" + uuid.uuid4().hex)
        try:
            with partial_path.open("xb") as raw_output:
                os.chmod(partial_path, 0o600)
                with tarfile.open(fileobj=raw_output, mode="w|gz", format=tarfile.PAX_FORMAT) as output_archive:
                    with tarfile.open(rootfs_tar, mode="r|gz") as input_archive:
                        for member in input_archive:
                            name = _archive_path(member.name, allow_root=member.isdir())
                            if name not in selected_members:
                                continue
                            info = _clone_info(member, name)
                            if member.islnk() and name in scan["hardlink_payloads"]:
                                source_name = scan["hardlink_payloads"][name]
                                source_record = scan["records"][source_name]
                                info.type = tarfile.REGTYPE
                                info.linkname = ""
                                info.size = source_record["size"]
                                info.pax_headers.pop("linkpath", None)
                                with payload_files[source_name].open("rb") as content:
                                    output_archive.addfile(info, content)
                            elif member.isfile():
                                content = input_archive.extractfile(member)
                                if content is None:
                                    raise FilesMigrationError("cannot read selected archive member")
                                output_archive.addfile(info, content)
                            else:
                                output_archive.addfile(info)
                raw_output.flush()
                os.fsync(raw_output.fileno())
            if sha256_file(rootfs_tar) != rootfs_digest:
                raise FilesMigrationError("rootfs archive changed during bundle creation")
            validate_bundle(partial_path, selected)
            try:
                os.link(partial_path, destination, follow_symlinks=False)
            except FileExistsError as exc:
                raise FilesMigrationError("bundle destination appeared during creation") from exc
            partial_path.unlink()
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            partial_path.unlink(missing_ok=True)
            raise
    except FilesMigrationError:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise FilesMigrationError("could not create the selected restore bundle") from exc
    finally:
        temp_root.cleanup()
    return {
        "schemaVersion": 1,
        "bundle": destination.name,
        "bundle_sha256": sha256_file(destination),
        "selected_paths": selected,
        "included_entry_count": len(selected_members),
        "excluded_runtime_storage_entry_count": scan["excluded"],
        "type_counts": scan["type_counts"],
        "numeric_ids": scan["numeric_ids"],
        "deployment": "staged_only",
    }


def validate_bundle(bundle: Path, paths: list[str]) -> dict[str, Any]:
    """Recheck a portable bundle before staging it on the target host."""
    selected = _selected_paths(paths)
    bundle = Path(bundle)
    if bundle.is_symlink() or not bundle.is_file():
        raise FilesMigrationError("bundle must be a regular local file")
    _verify_gzip(bundle)
    scan = _scan_archive(bundle, selected)
    token_ancestors = set(_ancestor_paths(_CLOUDFLARED_TOKEN.lstrip("/"))) if _CLOUDFLARED_TOKEN in selected else set()
    for name in scan["records"]:
        is_token_parent = (name in token_ancestors
                           and scan["records"][name]["kind"] == "directory")
        if (not _under_selection(name, selected) and not is_token_parent) or _runtime_storage(name):
            raise FilesMigrationError("bundle contains an entry outside the selected portable paths")
    return scan


def _os_id() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("ID="):
                return line[3:].strip().strip('"\'')
    except OSError:
        return ""
    return ""


def _real_root_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) & 0o077 == 0


def _validate_stage_ancestors(path: Path) -> None:
    current = Path("/")
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise FilesMigrationError("migration staging path cannot traverse symlinks")
        if current.exists():
            info = current.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
                raise FilesMigrationError("migration staging ancestors must be root-owned and not group/world writable")


def stage_bundle(plan_path: Path, bundle: Path) -> dict[str, Any]:
    """Validate and extract a bundle into a fresh root-only staging directory."""
    if os.geteuid() != 0:
        raise FilesMigrationError("apply must run as root")
    if _os_id().lower() != "almalinux":
        raise FilesMigrationError("apply target must report ID=almalinux")
    try:
        plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FilesMigrationError("restore plan is not readable UTF-8 JSON") from exc
    if not isinstance(plan, dict) or plan.get("schemaVersion") != 1:
        raise FilesMigrationError("unsupported restore plan")
    selected = _selected_paths(plan.get("selected_paths"))
    expected_hash = plan.get("bundle_sha256")
    if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
        raise FilesMigrationError("restore plan has an invalid bundle SHA-256")
    bundle = Path(bundle)
    if plan.get("bundle") is not None and plan["bundle"] != bundle.name:
        raise FilesMigrationError("bundle filename does not match the restore plan")
    if bundle.is_symlink() or not bundle.is_file() or sha256_file(bundle) != expected_hash:
        raise FilesMigrationError("bundle is missing or does not match the plan SHA-256")
    validate_bundle(bundle, selected)
    try:
        version = subprocess.run(["tar", "--version"], capture_output=True, text=True, check=False)
    except OSError as exc:
        raise FilesMigrationError("GNU tar is required to stage filesystem metadata") from exc
    if version.returncode or "GNU tar" not in version.stdout:
        raise FilesMigrationError("GNU tar is required to stage filesystem metadata")
    # Reject symlinked or untrusted ancestors before creating the stage dir.
    _validate_stage_ancestors(_MIGRATION_ROOT)
    if _MIGRATION_ROOT.is_symlink():
        raise FilesMigrationError("migration staging root cannot be a symlink")
    if _MIGRATION_ROOT.exists():
        if not _real_root_directory(_MIGRATION_ROOT):
            raise FilesMigrationError("migration staging root must be owned by root and mode 0700")
        os.chmod(_MIGRATION_ROOT, 0o700)
    else:
        _MIGRATION_ROOT.mkdir(mode=0o700, parents=True)
        os.chmod(_MIGRATION_ROOT, 0o700)
    stage = _MIGRATION_ROOT / ("stage-" + uuid.uuid4().hex)
    staged_bundle = _MIGRATION_ROOT / (".bundle-" + uuid.uuid4().hex + ".tar.gz")
    stage.mkdir(mode=0o700)
    try:
        source_fd = os.open(bundle, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                raise FilesMigrationError("bundle must be a regular file")
            with os.fdopen(source_fd, "rb", closefd=False) as source, staged_bundle.open("xb") as target:
                os.chmod(staged_bundle, 0o600)
                shutil.copyfileobj(source, target, 1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
        finally:
            os.close(source_fd)
        if sha256_file(staged_bundle) != expected_hash:
            raise FilesMigrationError("bundle changed after validation")
        validate_bundle(staged_bundle, selected)
        if any(stage.iterdir()):
            raise FilesMigrationError("new staging directory is not empty")
        command = ["tar", "--numeric-owner", "--acls", "--xattrs", "--xattrs-exclude=security.selinux",
                   "-xpf", str(staged_bundle), "-C", str(stage)]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise FilesMigrationError("GNU tar failed while staging the filesystem bundle")
        staged_bundle.unlink()
        return {"stage": str(stage), "selected_paths": selected, "deployment": "staged_only"}
    except FilesMigrationError:
        shutil.rmtree(stage)
        staged_bundle.unlink(missing_ok=True)
        raise
    except (OSError, tarfile.TarError, EOFError) as exc:
        shutil.rmtree(stage)
        staged_bundle.unlink(missing_ok=True)
        raise FilesMigrationError("could not safely stage the filesystem bundle") from exc
    except Exception:
        shutil.rmtree(stage)
        staged_bundle.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("apply",))
    parser.add_argument("plan_json")
    parser.add_argument("bundle")
    args = parser.parse_args(argv)
    try:
        result = stage_bundle(Path(args.plan_json), Path(args.bundle))
    except FilesMigrationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError:
        print("could not safely access or create the filesystem staging area", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
