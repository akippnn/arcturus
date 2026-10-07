import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pi_migration_files as files


def make_tar(path, members):
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for member, payload in members:
            archive.addfile(member, io.BytesIO(payload) if payload is not None else None)


def regular(name, content=b"data", **kwargs):
    member = tarfile.TarInfo(name)
    member.type = tarfile.REGTYPE
    member.size = len(content)
    member.mode = kwargs.get("mode", 0o640)
    member.uid = kwargs.get("uid", 1234)
    member.gid = kwargs.get("gid", 2345)
    member.mtime = kwargs.get("mtime", 1700000000)
    member.pax_headers = kwargs.get("pax_headers", {})
    return member, content


def directory(name):
    item = tarfile.TarInfo(name)
    item.type = tarfile.DIRTYPE
    item.mode = 0o750
    return item, None


class PortableFilesBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "rootfs.tar.gz"
        self.bundle = self.root / "portable.tar.gz"

    def tearDown(self):
        self.temp.cleanup()

    def test_individual_timer_is_preserved_without_allowing_unit_directories(self):
        path = "etc/systemd/system/update.timer"
        content = b"[Timer]\nOnCalendar=daily\n"
        make_tar(self.source, [regular(path, content, uid=0, gid=0)])
        files.build_bundle(self.source, self.bundle, ["/" + path])
        with tarfile.open(self.bundle, "r:gz") as archive:
            self.assertEqual(archive.extractfile(path).read(), content)
        for selection in ("/etc/systemd/system", "/etc/systemd/system/a.service.d/override.conf", "/etc/systemd/system/other.socket"):
            with self.subTest(path=selection), self.assertRaises(files.FilesMigrationError):
                files._selected_paths([selection])

    def test_cloudflared_token_selection_includes_only_token_and_required_real_directories(self):
        token_path = "etc/cloudflared/token"
        make_tar(self.source, [directory("etc"), directory("etc/cloudflared"),
                              regular(token_path, b"protected credential", mode=0o600, uid=0, gid=0),
                              regular("etc/cloudflared/config.yml", b"unreviewed config", mode=0o600, uid=0, gid=0)])
        report = files.build_bundle(self.source, self.bundle, ["/" + token_path])
        self.assertEqual(report["selected_paths"], ["/" + token_path])
        with tarfile.open(self.bundle, "r:gz") as archive:
            self.assertEqual({item.name for item in archive}, {"etc", "etc/cloudflared", token_path})
            token = archive.getmember(token_path)
            self.assertEqual((token.uid, token.gid, token.mode), (0, 0, 0o600))
            self.assertEqual(archive.extractfile(token).read(), b"protected credential")

    def test_bundle_filters_runtime_storage_and_preserves_pax_metadata_and_links(self):
        pax = {"SCHILY.acl.access": "user::rw-,group::r--,other::---",
               "SCHILY.xattr.user.test": "pax-value"}
        members = [
            directory("home/aki"),
            regular("home/aki/config", b"important", mode=0o640, uid=1001, gid=1002, pax_headers=pax),
            (tarfile.TarInfo("home/aki/shared-link"), None),
            (tarfile.TarInfo("home/aki/absolute-link"), None),
            regular("opt/arcturus/bin/tool", b"tool-bytes", mode=0o751, uid=0, gid=0),
            (tarfile.TarInfo("home/aki/copied-tool"), None),
            regular("home/aki/.local/share/containers/storage/overlay/layer", b"runtime-secret"),
            regular("home/aki/.local/share/docker/overlay2/layer", b"docker-runtime"),
        ]
        members[2][0].type = tarfile.SYMTYPE
        members[2][0].linkname = "../../opt/arcturus"
        members[3][0].type = tarfile.SYMTYPE
        members[3][0].linkname = "/opt/arcturus"
        members[5][0].type = tarfile.LNKTYPE
        members[5][0].linkname = "opt/arcturus/bin/tool"
        make_tar(self.source, members)

        report = files.build_bundle(self.source, self.bundle, ["/home/aki"])
        self.assertEqual(report["excluded_runtime_storage_entry_count"], 2)
        self.assertEqual(report["deployment"], "staged_only")
        self.assertEqual(report["bundle_sha256"], hashlib.sha256(self.bundle.read_bytes()).hexdigest())
        with tarfile.open(self.bundle, "r:gz") as archive:
            names = {item.name for item in archive}
            self.assertEqual(names, {"home/aki", "home/aki/config", "home/aki/shared-link",
                                     "home/aki/absolute-link", "home/aki/copied-tool"})
            config = archive.getmember("home/aki/config")
            self.assertEqual((config.uid, config.gid, config.mode, config.mtime), (1001, 1002, 0o640, 1700000000))
            self.assertEqual(config.pax_headers.get("SCHILY.acl.access"), pax["SCHILY.acl.access"])
            self.assertEqual(config.pax_headers.get("SCHILY.xattr.user.test"), "pax-value")
            self.assertEqual(archive.extractfile(config).read(), b"important")
            self.assertTrue(archive.getmember("home/aki/shared-link").issym())
            self.assertEqual(archive.getmember("home/aki/absolute-link").linkname, "/opt/arcturus")
            copied = archive.getmember("home/aki/copied-tool")
            self.assertTrue(copied.isfile())
            self.assertEqual(archive.extractfile(copied).read(), b"tool-bytes")

    def test_selected_hardlink_target_is_preserved(self):
        hardlink = tarfile.TarInfo("home/aki/linked")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "home/aki/original"
        make_tar(self.source, [regular("home/aki/original", b"shared"), (hardlink, None)])
        files.build_bundle(self.source, self.bundle, ["/home/aki"])
        with tarfile.open(self.bundle, "r:gz") as archive:
            linked = archive.getmember("home/aki/linked")
            self.assertTrue(linked.islnk())
            self.assertEqual(linked.linkname, "home/aki/original")

    def test_pre_scan_rejects_traversal_duplicate_symlink_ancestor_and_missing_hardlink(self):
        unsafe = tarfile.TarInfo("home/aki/../../etc/passwd")
        unsafe.type = tarfile.REGTYPE
        unsafe.size = 1
        make_tar(self.source, [(unsafe, b"x")])
        with self.assertRaisesRegex(files.FilesMigrationError, "traverses"):
            files.build_bundle(self.source, self.bundle, ["/home/aki"])
        self.assertFalse(self.bundle.exists())

        link = tarfile.TarInfo("home/aki/.ssh")
        link.type = tarfile.SYMTYPE
        link.linkname = "/tmp/unsafe"
        make_tar(self.source, [(link, None), regular("home/aki/.ssh/key", b"key")])
        with self.assertRaisesRegex(files.FilesMigrationError, "symlink ancestor"):
            files.build_bundle(self.source, self.bundle, ["/home/aki"])
        self.assertFalse(self.bundle.exists())

        broken = tarfile.TarInfo("home/aki/broken")
        broken.type = tarfile.LNKTYPE
        broken.linkname = "home/aki/missing"
        make_tar(self.source, [(broken, None)])
        with self.assertRaisesRegex(files.FilesMigrationError, "target is missing"):
            files.build_bundle(self.source, self.bundle, ["/home/aki"])

        escaping = tarfile.TarInfo("home/aki/escape")
        escaping.type = tarfile.SYMTYPE
        escaping.linkname = "../../../../etc"
        make_tar(self.source, [(escaping, None)])
        with self.assertRaisesRegex(files.FilesMigrationError, "escapes the archive root"):
            files.build_bundle(self.source, self.bundle, ["/home/aki"])

    def test_duplicate_member_and_runtime_hardlink_target_are_rejected(self):
        make_tar(self.source, [regular("home/aki/a"), regular("./home/aki/a")])
        with self.assertRaisesRegex(files.FilesMigrationError, "duplicate member"):
            files.build_bundle(self.source, self.bundle, ["/home/aki"])

        link = tarfile.TarInfo("home/aki/runtime-copy")
        link.type = tarfile.LNKTYPE
        link.linkname = "home/aki/.local/share/docker/data"
        make_tar(self.source, [(link, None), regular("home/aki/.local/share/docker/data", b"store")])
        with self.assertRaisesRegex(files.FilesMigrationError, "runtime storage"):
            files.build_bundle(self.source, self.bundle, ["/home/aki"])

    def test_only_selected_entries_are_accepted_in_target_bundle(self):
        make_tar(self.source, [regular("home/aki/config"), regular("opt/unselected/file")])
        # The source archive may contain unrelated entries; the bundle may not.
        with self.assertRaisesRegex(files.FilesMigrationError, "outside the selected"):
            files.validate_bundle(self.source, ["/home/aki"])

    def test_restore_path_rules_keep_systemd_units_individual(self):
        self.assertEqual(files._selected_paths(["/etc/systemd/system/app.service"]), ["/etc/systemd/system/app.service"])
        with self.assertRaisesRegex(files.FilesMigrationError, "individually"):
            files._selected_paths(["/etc/systemd/system"])
        with self.assertRaisesRegex(files.FilesMigrationError, "service or timer files individually"):
            files._selected_paths(["/etc/systemd/system/multi-user.target.wants"])

    def test_remote_apply_verifies_plan_and_stages_without_deploying(self):
        make_tar(self.source, [directory("home/aki"), regular("home/aki/config", b"config")])
        report = files.build_bundle(self.source, self.bundle, ["/home/aki"])
        plan = self.root / "plan.json"
        plan.write_text(json.dumps(report), encoding="utf-8")
        fake_run = mock.Mock(side_effect=[
            subprocess.CompletedProcess(["tar", "--version"], 0, stdout="GNU tar 1.0\n", stderr=""),
            subprocess.CompletedProcess(["tar", "-xpf"], 0, stdout="", stderr=""),
        ])
        with tempfile.TemporaryDirectory(dir="/private/tmp") as stage_base:
            migration_root = Path(stage_base) / "migration-stage"
            with mock.patch.object(files, "_MIGRATION_ROOT", migration_root), \
                 mock.patch.object(files, "_validate_stage_ancestors"), \
                 mock.patch.object(files.os, "geteuid", return_value=0), \
                 mock.patch.object(files, "_os_id", return_value="almalinux"), \
                 mock.patch.object(files.subprocess, "run", fake_run):
                result = files.stage_bundle(plan, self.bundle)
            stage = Path(result["stage"])
            self.assertTrue(stage.is_dir())
            self.assertEqual(stage.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(stage.iterdir()), [])
            self.assertEqual(result["deployment"], "staged_only")
            extraction_args = fake_run.call_args_list[1].args[0]
            self.assertIn("--numeric-owner", extraction_args)
            self.assertIn("--acls", extraction_args)
            self.assertIn("--xattrs-exclude=security.selinux", extraction_args)
            self.assertEqual(extraction_args[extraction_args.index("-C") + 1], str(stage))

    def test_remote_apply_rejects_wrong_os_and_hash_before_staging(self):
        make_tar(self.source, [directory("home/aki"), regular("home/aki/config")])
        report = files.build_bundle(self.source, self.bundle, ["/home/aki"])
        plan = self.root / "plan.json"
        report["bundle_sha256"] = "0" * 64
        plan.write_text(json.dumps(report), encoding="utf-8")
        migration_root = self.root / "migration-stage"
        with mock.patch.object(files, "_MIGRATION_ROOT", migration_root), \
             mock.patch.object(files.os, "geteuid", return_value=0), \
             mock.patch.object(files, "_os_id", return_value="almalinux"):
            with self.assertRaisesRegex(files.FilesMigrationError, "does not match"):
                files.stage_bundle(plan, self.bundle)
        self.assertFalse(migration_root.exists())

        with mock.patch.object(files.os, "geteuid", return_value=0), \
             mock.patch.object(files, "_os_id", return_value="debian"):
            with self.assertRaisesRegex(files.FilesMigrationError, "ID=almalinux"):
                files.stage_bundle(plan, self.bundle)


if __name__ == "__main__":
    unittest.main()
