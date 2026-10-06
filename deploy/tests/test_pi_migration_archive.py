import gzip
import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pi_migration_archive as archive


def _tar_gz(path: Path, entries: list[tuple[str, bytes | None, str | None]], *, uid: int = 1001, gid: int = 1002) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                for name, data, link in entries:
                    member = tarfile.TarInfo(name)
                    member.uid = uid
                    member.gid = gid
                    if link is not None:
                        member.type = tarfile.SYMTYPE
                        member.linkname = link
                        tar.addfile(member)
                    elif data is None:
                        member.type = tarfile.DIRTYPE
                        tar.addfile(member)
                    else:
                        member.size = len(data)
                        tar.addfile(member, io.BytesIO(data))


def _write_backup(directory: Path) -> dict:
    raw = b"whole card contents"
    (directory / "whole-card.raw.gz").write_bytes(gzip.compress(raw, mtime=0))
    _tar_gz(directory / "rootfs.tar.gz", [("./home/alice/app/data.db", b"database", None), ("./etc/hostname", b"pi", None)])
    _tar_gz(directory / "bootfs.tar.gz", [("./config.txt", b"dtoverlay=test", None)])
    (directory / "inventory.json").write_text(json.dumps({"packages": []}), encoding="utf-8")
    files = {}
    for name in archive.REQUIRED_FILES:
        path = directory / name
        entry = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
        if name == "whole-card.raw.gz":
            entry.update({"uncompressed_sha256": hashlib.sha256(raw).hexdigest(), "uncompressed_bytes": len(raw)})
        files[name] = entry
    manifest = {
        "schemaVersion": 1,
        "target": {"cid": "test-cid", "size_bytes": len(raw), "device": "/dev/mmcblk0"},
        "offline": True,
        "files": files,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


class PiMigrationArchiveTests(unittest.TestCase):
    def test_verifies_offline_bundle_hashes_gzip_and_tar_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_backup(directory)
            _tar_gz(
                directory / "rootfs.tar.gz",
                [
                    ("./home/alice/app/data.db", b"database", None),
                    ("./etc/mtab", None, "/proc/mounts"),
                ],
            )
            manifest_path = directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            rootfs_path = directory / "rootfs.tar.gz"
            manifest["files"]["rootfs.tar.gz"] = {
                "bytes": rootfs_path.stat().st_size,
                "sha256": hashlib.sha256(rootfs_path.read_bytes()).hexdigest(),
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = archive.verify_backup(directory)
            self.assertEqual(result["verification"]["status"], "verified")
            rootfs = result["verification"]["files"]["rootfs.tar.gz"]
            self.assertEqual(rootfs["type_counts"]["regular"], 1)
            self.assertEqual(rootfs["type_counts"]["absolute_symlink"], 1)
            self.assertEqual(rootfs["numeric_ids"], {"uids": [1001], "gids": [1002]})

    def test_detects_compressed_artifact_tampering_and_manifest_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_backup(directory)
            with (directory / "inventory.json").open("ab") as stream:
                stream.write(b" ")
            with self.assertRaisesRegex(archive.MigrationArchiveError, "mismatch"):
                archive.verify_backup(directory)

    def test_detects_truncated_raw_gzip_even_if_manifest_matches(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_backup(directory)
            raw_path = directory / "whole-card.raw.gz"
            raw_path.write_bytes(raw_path.read_bytes()[:-5])
            manifest_path = directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["files"][raw_path.name]["bytes"] = raw_path.stat().st_size
            manifest["files"][raw_path.name]["sha256"] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(archive.MigrationArchiveError, "gzip"):
                archive.verify_backup(directory)

    def test_rejects_tar_traversal_and_unsafe_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.tar.gz"
            _tar_gz(path, [("./home/../etc/passwd", b"no", None)])
            with self.assertRaisesRegex(archive.MigrationArchiveError, "traverses"):
                archive._verify_tar(path)
            _tar_gz(path, [("home/alice/app/link", None, "../../../../etc/shadow")])
            with self.assertRaisesRegex(archive.MigrationArchiveError, "escapes"):
                archive._verify_tar(path)

    def test_restore_allowlist_and_runtime_stores(self):
        allowed = ["/home/alice/app", "/srv/apps", "/etc/arcturus/config", "/etc/subuid",
                   "/etc/cloudflared/token"]
        self.assertEqual(archive.validate_restore_paths(allowed), allowed)
        for path in (
            "/etc/passwd",
            "/etc/cloudflared",
            "/etc/cloudflared/other",
            "/etc/cloudflared/token/child",
            "/usr/lib/systemd/system/example.service",
            "/var/lib/docker",
            "/var/lib/containers/storage",
            "/home/alice/.local/share/containers/storage",
            "/srv/../etc",
        ):
            with self.subTest(path=path), self.assertRaises(archive.MigrationArchiveError):
                archive.validate_restore_paths([path])

    def test_selection_rejects_link_leaving_selected_subtree_and_reports_owners(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rootfs.tar.gz"
            _tar_gz(path, [("home/alice/app/data", b"contents", None), ("home/alice/app/link", None, "../../other" )])
            with self.assertRaisesRegex(archive.MigrationArchiveError, "selected restore subtree"):
                archive.inspect_restore_selection(path, ["/home/alice/app"])
            _tar_gz(path, [("home/alice/app/data", b"contents", None)], uid=1234, gid=2345)
            report = archive.inspect_restore_selection(path, ["/home/alice/app"])
            self.assertEqual(report["numeric_ids"], {"uids": [1234], "gids": [2345]})
            self.assertTrue(report["warnings"])

    def test_home_selection_excludes_rootless_runtime_storage_and_restore_scan_rejects_absolute_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rootfs.tar.gz"
            _tar_gz(
                path,
                [
                    ("home/alice/app/data", b"contents", None),
                    ("home/alice/.local/share/containers/storage/overlay/layer", b"runtime", None),
                    ("home/alice/app/absolute-link", None, "/proc/mounts"),
                ],
            )
            full_scan = archive._verify_tar(path)
            self.assertEqual(full_scan["type_counts"]["absolute_symlink"], 1)
            with self.assertRaisesRegex(archive.MigrationArchiveError, "absolute"):
                archive.inspect_restore_selection(path, ["/home/alice/app"])

            _tar_gz(
                path,
                [
                    ("home/alice/app/data", b"contents", None),
                    ("home/alice/.local/share/containers/storage/overlay/layer", b"runtime", None),
                ],
            )
            report = archive.inspect_restore_selection(path, ["/home/alice"])
            self.assertEqual(report["selected_entry_count"], 1)
            self.assertEqual(report["excluded_entry_count"], 1)
            self.assertEqual(
                report["excluded_runtime_storage"],
                [{"path": "/home/alice/.local/share/containers/storage", "entry_count": 1}],
            )


if __name__ == "__main__":
    unittest.main()
