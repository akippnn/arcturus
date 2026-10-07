"""Offline verification and safe restore planning for Pi migration archives.

This module deliberately does not extract files or modify a host.  It validates
backup integrity and helps choose portable data for a cross-distribution move.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import posixpath
import re
import stat
import tarfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any


class MigrationArchiveError(RuntimeError):
    """Raised when an archive, manifest, or restore selection is unsafe."""


REQUIRED_FILES = ("whole-card.raw.gz", "rootfs.tar.gz", "bootfs.tar.gz", "inventory.json")
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_PREFIXES = (
    "/home",
    "/root",
    "/opt",
    "/srv",
    "/usr/local",
    "/var/lib",
    "/etc/systemd/system",
    "/etc/containers",
    "/etc/arcturus",
)
_EXACT_ALLOWED = {"/etc/subuid", "/etc/subgid", "/etc/cloudflared/token"}


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a regular file, read in bounded chunks."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise MigrationArchiveError(f"not a regular file: {path.name}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise MigrationArchiveError(f"cannot read {path.name}: {exc}") from exc
    return digest.hexdigest()


def _regular_file(directory: Path, name: str) -> Path:
    path = directory / name
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise MigrationArchiveError(f"required file is missing: {name}") from exc
    if not stat.S_ISREG(mode):
        raise MigrationArchiveError(f"expected a regular local file: {name}")
    return path


def _decompressed_metadata(path: Path) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    try:
        with gzip.open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except (OSError, EOFError, zlib.error) as exc:
        raise MigrationArchiveError(f"invalid or truncated gzip file: {path.name}") from exc
    return size, digest.hexdigest()


def _archive_name(name: str, *, allow_root: bool = False) -> str:
    """Canonicalize a tar member name after rejecting traversal components."""
    if not isinstance(name, str) or not name or name.startswith("/") or "\x00" in name:
        raise MigrationArchiveError("tar contains an absolute or empty member name")
    # A leading sequence of ./ is common in tarballs; internal dot components
    # and all parent components are ambiguous and therefore rejected.
    pieces = name.split("/")
    while pieces and pieces[0] == ".":
        pieces.pop(0)
    if any(piece in (".", "..") for piece in pieces):
        raise MigrationArchiveError("tar member path traverses outside the archive root")
    result = "/".join(piece for piece in pieces if piece)
    if not result and not allow_root:
        raise MigrationArchiveError("tar contains an empty member path")
    return result


def _resolve_link(base: str, target: str, *, hardlink: bool) -> str:
    if not isinstance(target, str) or not target or target.startswith("/") or "\x00" in target:
        raise MigrationArchiveError("tar contains an absolute or empty link target")
    # Hardlink names are archive-root relative; symlink names are relative to
    # their containing directory. Permit `..` only while it remains in-root.
    initial = [] if hardlink else [part for part in posixpath.dirname(base).split("/") if part]
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not initial:
                raise MigrationArchiveError("tar link escapes the archive root")
            initial.pop()
        else:
            initial.append(part)
    return "/".join(initial)


def _verify_tar(path: Path) -> dict[str, Any]:
    # Consume the gzip stream independently: tar readers may stop at the tar
    # end marker without reading a gzip trailer and checking its CRC.
    _decompressed_metadata(path)
    counts = {
        "regular": 0,
        "directory": 0,
        "symlink": 0,
        "hardlink": 0,
        "absolute_symlink": 0,
        "other": 0,
    }
    uids: set[int] = set()
    gids: set[int] = set()
    members: list[tuple[str, tarfile.TarInfo]] = []
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive:
                name = _archive_name(member.name, allow_root=member.isdir())
                if member.issym():
                    if member.linkname.startswith("/"):
                        if "\x00" in member.linkname:
                            raise MigrationArchiveError("tar contains an invalid absolute symlink target")
                        counts["absolute_symlink"] += 1
                    else:
                        _resolve_link(name, member.linkname, hardlink=False)
                    kind = "symlink"
                elif member.islnk():
                    _resolve_link(name, member.linkname, hardlink=True)
                    kind = "hardlink"
                elif member.isfile():
                    kind = "regular"
                elif member.isdir():
                    kind = "directory"
                else:
                    kind = "other"
                counts[kind] += 1
                uids.add(int(member.uid))
                gids.add(int(member.gid))
                members.append((name, member))
    except MigrationArchiveError:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise MigrationArchiveError(f"invalid or truncated tar archive: {path.name}") from exc
    return {
        "type_counts": counts,
        "numeric_ids": {"uids": sorted(uids), "gids": sorted(gids)},
        "_members": members,
    }


def verify_backup(directory: Path) -> dict[str, Any]:
    """Verify a complete offline backup and return its manifest plus findings."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise MigrationArchiveError("backup directory must be a real local directory")
    manifest_path = _regular_file(directory, "manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationArchiveError("manifest.json is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
        raise MigrationArchiveError("unsupported or missing manifest schemaVersion")
    if manifest.get("offline") is not True:
        raise MigrationArchiveError("backup manifest must declare offline=true")
    target = manifest.get("target")
    if not isinstance(target, dict) or not all(key in target for key in ("cid", "size_bytes", "device")):
        raise MigrationArchiveError("manifest target must include cid, size_bytes, and device")
    if isinstance(target["size_bytes"], bool) or not isinstance(target["size_bytes"], int) or target["size_bytes"] <= 0:
        raise MigrationArchiveError("manifest target size_bytes must be a positive integer")

    files = manifest.get("files")
    if not isinstance(files, dict):
        raise MigrationArchiveError("manifest files must be an object")
    missing = sorted(set(REQUIRED_FILES) - set(files))
    if missing:
        raise MigrationArchiveError(f"manifest is missing required artifacts: {', '.join(missing)}")
    verified: dict[str, dict[str, Any]] = {}
    for name, expected in files.items():
        if not isinstance(name, str) or name in (".", "..") or "/" in name or "\\" in name:
            raise MigrationArchiveError("manifest artifact names must be local filenames")
        if not isinstance(expected, dict):
            raise MigrationArchiveError(f"invalid manifest entry for {name}")
        path = _regular_file(directory, name)
        digest = expected.get("sha256")
        size = expected.get("bytes")
        if not isinstance(digest, str) or not _HEX_SHA256.fullmatch(digest):
            raise MigrationArchiveError(f"invalid SHA-256 in manifest for {name}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise MigrationArchiveError(f"invalid byte count in manifest for {name}")
        actual_size = path.stat().st_size
        actual_digest = sha256_file(path)
        if actual_size != size or actual_digest != digest:
            raise MigrationArchiveError(f"size or SHA-256 mismatch for {name}")
        verified[name] = {"bytes": actual_size, "sha256": actual_digest}
        for key in ("uncompressed_bytes",):
            if key in expected and (isinstance(expected[key], bool) or not isinstance(expected[key], int) or expected[key] < 0):
                raise MigrationArchiveError(f"invalid {key} in manifest for {name}")
        if "uncompressed_sha256" in expected and (
            not isinstance(expected["uncompressed_sha256"], str)
            or not _HEX_SHA256.fullmatch(expected["uncompressed_sha256"])
        ):
            raise MigrationArchiveError(f"invalid uncompressed_sha256 in manifest for {name}")

    raw_expected = files["whole-card.raw.gz"]
    if not all(key in raw_expected for key in ("uncompressed_sha256", "uncompressed_bytes")):
        raise MigrationArchiveError("whole-card.raw.gz requires uncompressed digest and byte count")
    raw_size, raw_digest = _decompressed_metadata(directory / "whole-card.raw.gz")
    if raw_size != raw_expected["uncompressed_bytes"] or raw_digest != raw_expected["uncompressed_sha256"]:
        raise MigrationArchiveError("uncompressed raw image size or SHA-256 mismatch")
    if raw_size != target["size_bytes"]:
        raise MigrationArchiveError("uncompressed raw image size does not match target size_bytes")
    verified["whole-card.raw.gz"].update({"uncompressed_bytes": raw_size, "uncompressed_sha256": raw_digest})

    try:
        json.loads((directory / "inventory.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationArchiveError("inventory.json is not valid UTF-8 JSON") from exc
    tar_reports = {name: _verify_tar(directory / name) for name in ("rootfs.tar.gz", "bootfs.tar.gz")}
    for name, report in tar_reports.items():
        report.pop("_members", None)
        verified[name].update(report)
    result = dict(manifest)
    result["verification"] = {"status": "verified", "files": verified}
    return result


def validate_restore_paths(paths: list[str]) -> list[str]:
    """Validate absolute, portable restore selections and reject OS/runtime roots."""
    if not isinstance(paths, list) or not paths:
        raise MigrationArchiveError("select at least one absolute restore path")
    accepted: list[str] = []
    for original in paths:
        if not isinstance(original, str) or not original.startswith("/") or "\x00" in original:
            raise MigrationArchiveError("restore paths must be absolute POSIX paths")
        components = original.split("/")[1:]
        if not components or any(component in ("", ".", "..") for component in components):
            raise MigrationArchiveError("restore paths must be normalized and cannot traverse")
        path = "/" + "/".join(components)
        if path in _EXACT_ALLOWED:
            accepted.append(path)
            continue
        if not any(path == prefix or path.startswith(prefix + "/") for prefix in _ALLOWED_PREFIXES):
            raise MigrationArchiveError(f"restore path is outside the portable allowlist: {path}")
        if path in ("/var/lib", "/etc/systemd/system", "/etc/containers", "/etc/arcturus"):
            raise MigrationArchiveError(f"restore path is too broad: {path}")
        if path == "/usr/local" or path in ("/home", "/root", "/opt", "/srv"):
            pass  # These directory roots are intentionally valid selections.
        elif path.startswith("/usr/local/") or path.startswith("/home/") or path.startswith("/root/") or path.startswith("/opt/") or path.startswith("/srv/"):
            pass
        elif path.startswith("/var/lib/") or path.startswith("/etc/systemd/system/") or path.startswith("/etc/containers/") or path.startswith("/etc/arcturus/"):
            pass
        else:
            raise MigrationArchiveError(f"restore path is outside the portable allowlist: {path}")

        if _covers_runtime_storage(path):
            raise MigrationArchiveError(f"container runtime storage cannot be restored directly: {path}")
        accepted.append(path)
    return accepted


def _covers_runtime_storage(path: str) -> bool:
    roots = (
        "/var/lib/docker",
        "/var/lib/containers/storage",
        "/root/.local/share/containers/storage",
        "/root/.local/share/docker",
    )
    for root in roots:
        if path == root or path.startswith(root + "/"):
            return True
    if path.startswith("/home/"):
        pieces = path.split("/")
        if len(pieces) >= 3:
            user_root = "/home/" + pieces[2]
            user_stores = (
                user_root + "/.local/share/containers/storage",
                user_root + "/.local/share/docker",
            )
            return any(path == root or path.startswith(root + "/") for root in user_stores)
    return False


def _runtime_storage_root(member_name: str) -> str | None:
    path = "/" + member_name
    roots = [
        "/var/lib/docker",
        "/var/lib/containers/storage",
        "/root/.local/share/containers/storage",
        "/root/.local/share/docker",
    ]
    if path.startswith("/home/"):
        components = path.split("/")
        if len(components) >= 3:
            user_root = "/home/" + components[2]
            roots.extend((
                user_root + "/.local/share/containers/storage",
                user_root + "/.local/share/docker",
            ))
    for root in roots:
        if path == root or path.startswith(root + "/"):
            return root
    return None


def inspect_restore_selection(rootfs_tar: Path, paths: list[str]) -> dict[str, Any]:
    """Inspect selected rootfs entries and return a cross-distro restore plan."""
    selected = validate_restore_paths(paths)
    archive_report = _verify_tar(Path(rootfs_tar))
    members: list[tuple[str, tarfile.TarInfo]] = archive_report.pop("_members")
    matched: set[str] = set()
    counts = {"regular": 0, "directory": 0, "symlink": 0, "hardlink": 0, "other": 0}
    selected_uids: set[int] = set()
    selected_gids: set[int] = set()
    found_selections: set[str] = set()
    excluded_storage: dict[str, set[str]] = {}
    for name, member in members:
        containing = [root for root in selected if name == root.lstrip("/") or name.startswith(root.lstrip("/") + "/")]
        if containing:
            found_selections.update(containing)
            storage_root = _runtime_storage_root(name)
            if storage_root is not None:
                excluded_storage.setdefault(storage_root, set()).add(name)
                continue
            matched.add(name)
            selected_uids.add(int(member.uid))
            selected_gids.add(int(member.gid))
            if member.issym():
                kind = "symlink"
                target = _resolve_link(name, member.linkname, hardlink=False)
            elif member.islnk():
                kind = "hardlink"
                target = _resolve_link(name, member.linkname, hardlink=True)
            elif member.isfile():
                kind = "regular"
                target = None
            elif member.isdir():
                kind = "directory"
                target = None
            else:
                kind = "other"
                target = None
            counts[kind] += 1
            if target is not None and not any(target == root.lstrip("/") or target.startswith(root.lstrip("/") + "/") for root in selected):
                raise MigrationArchiveError("selected tar link escapes its selected restore subtree")
    missing = [root for root in selected if root not in found_selections and not any(
        root.lstrip("/").startswith(name + "/") for name, _member in members
    )]
    if missing:
        raise MigrationArchiveError("selected restore path is absent from rootfs archive")
    warnings = [
        "Numeric ownership may refer to Debian users and groups; create matching AlmaLinux identities before restoring ownership-dependent data.",
        "Recreate and validate required packages, systemd units, and application dependencies on AlmaLinux.",
        "Recreate container runtimes, images, and workloads; runtime storage archives are refused for direct restore.",
    ]
    return {
        "selected_paths": selected,
        "selected_entry_count": len(matched),
        "type_counts": counts,
        "numeric_ids": {"uids": sorted(selected_uids), "gids": sorted(selected_gids)},
        "excluded_runtime_storage": [
            {"path": root, "entry_count": len(entries)}
            for root, entries in sorted(excluded_storage.items())
        ],
        "excluded_entry_count": sum(len(entries) for entries in excluded_storage.values()),
        "warnings": warnings,
    }
