"""Local safety tests for deploy/pi-migration/deploy-files.py."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

DEPLOY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEPLOY_DIR))
import pi_migration_files as portable_files

MODULE_PATH = DEPLOY_DIR / "pi-migration" / "deploy-files.py"
SPEC = importlib.util.spec_from_file_location("pi_migration_deploy_files", MODULE_PATH)
deploy_files = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(deploy_files)


def make_bundle(directory: Path, *, uid: int | None = None, gid: int | None = None):
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    source = directory / "source.tar.gz"
    bundle = directory / "portable.tar.gz"
    with tarfile.open(source, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        home = tarfile.TarInfo("home/aki")
        home.type = tarfile.DIRTYPE
        home.mode = 0o750
        home.uid = uid
        home.gid = gid
        archive.addfile(home)
        item = tarfile.TarInfo("home/aki/config.txt")
        item.type = tarfile.REGTYPE
        item.mode = 0o640
        item.uid = uid
        item.gid = gid
        item.size = 4
        archive.addfile(item, io.BytesIO(b"data"))
    report = portable_files.build_bundle(source, bundle, ["/home/aki"])
    plan_path = directory / "plan.json"
    plan_path.write_text(json.dumps(report), encoding="utf-8")
    return plan_path, bundle, report


class DeployFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.target = self.root / "target"
        self.home = self.target / "home" / "aki"
        self.home.mkdir(parents=True)
        self.owner = SimpleNamespace(
            pw_name="aki", pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(self.home)
        )
        self.migration_root = self.root / "var" / "lib" / "arcturus-migration"
        self.migration_root.mkdir(parents=True, mode=0o700)
        self.public_key = "ssh-ed25519 " + base64.b64encode(b"fixture-key-bytes").decode() + " test key"
        self.plan, self.bundle, self.report = make_bundle(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def mocked_run(self, *, fail_extract=False):
        calls = []

        def run(argv, **kwargs):
            argv = list(argv)
            calls.append(argv)
            if argv[:2] == ["tar", "--version"]:
                return SimpleNamespace(returncode=0, stdout="GNU tar 1.35\n", stderr="")
            if argv[:2] == ["restorecon", "-n"]:
                return SimpleNamespace(returncode=0, stdout="restorecon 1\n", stderr="")
            if "-cpf" in argv:
                backup = Path(argv[argv.index("-cpf") + 1])
                backup.write_bytes(b"safe backup fixture")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if "-xpf" in argv and fail_extract:
                return SimpleNamespace(returncode=2, stdout="", stderr="fixture failure")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        return mock.Mock(side_effect=run), calls

    def _run_deploy(self, run_mock, **patches):
        defaults = {
            "_TARGET_ROOT": self.target,
            "_MIGRATION_ROOT": self.migration_root,
            "_ensure_migration_root": mock.Mock(),
        }
        defaults.update(patches)
        with mock.patch.object(deploy_files.os, "geteuid", return_value=0), \
             mock.patch.object(portable_files, "_os_id", return_value="almalinux"), \
             mock.patch.object(deploy_files.pwd, "getpwnam", return_value=self.owner), \
             mock.patch.object(deploy_files.subprocess, "run", run_mock), \
             mock.patch.multiple(deploy_files, **defaults):
            return deploy_files.deploy(self.plan, self.bundle, self.public_key, "aki")

    def _run_cli(self, key_file, run_mock):
        defaults = {
            "_TARGET_ROOT": self.target,
            "_MIGRATION_ROOT": self.migration_root,
            "_ensure_migration_root": mock.Mock(),
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(deploy_files.os, "geteuid", return_value=0), \
             mock.patch.object(portable_files, "_os_id", return_value="almalinux"), \
             mock.patch.object(deploy_files.pwd, "getpwnam", return_value=self.owner), \
             mock.patch.object(deploy_files.subprocess, "run", run_mock), \
             mock.patch.multiple(deploy_files, **defaults), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = deploy_files.main([str(self.plan), str(self.bundle), str(key_file), "aki"])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_deploy_backs_up_selected_existing_files_then_extracts_and_restores_key_mode(self):
        config = self.home / "config.txt"
        config.write_text("old", encoding="utf-8")
        ssh_dir = self.home / ".ssh"
        ssh_dir.mkdir(mode=0o755)
        authorized = ssh_dir / "authorized_keys"
        authorized.write_text("ssh-ed25519 AAAAold old-key\n", encoding="utf-8")
        os.chmod(authorized, 0o644)
        run_mock, calls = self.mocked_run()

        result = self._run_deploy(run_mock)

        self.assertEqual(result["status"], "deployed")
        self.assertEqual(result["selected_paths"], ["/home/aki"])
        self.assertEqual(result["bootstrap_key"], "preserved")
        backup = Path(result["backup"])
        self.assertTrue(backup.is_file())
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        backup_call = next(call for call in calls if "-cpf" in call)
        self.assertIn("--numeric-owner", backup_call)
        self.assertIn("--acls", backup_call)
        self.assertIn("--xattrs", backup_call)
        self.assertIn("home/aki", backup_call)
        extract_call = next(call for call in calls if "-xpf" in call)
        self.assertIn("--numeric-owner", extract_call)
        self.assertIn("--acls", extract_call)
        self.assertIn("--xattrs-exclude=security.selinux", extract_call)
        self.assertLess(calls.index(backup_call), calls.index(extract_call))
        restorecon_call = next(call for call in calls if call[:2] == ["restorecon", "-RF"])
        self.assertEqual(restorecon_call[-1], "/home/aki")
        self.assertIn(self.public_key, authorized.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(ssh_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(authorized.stat().st_mode), 0o600)
        self.assertEqual((authorized.stat().st_uid, authorized.stat().st_gid), (self.owner.pw_uid, self.owner.pw_gid))

    def test_existing_bootstrap_key_is_not_duplicated(self):
        ssh_dir = self.home / ".ssh"
        ssh_dir.mkdir()
        authorized = ssh_dir / "authorized_keys"
        authorized.write_text("comment before\n" + self.public_key + "\n", encoding="utf-8")
        run_mock, _ = self.mocked_run()
        self._run_deploy(run_mock)
        self.assertEqual(authorized.read_text(encoding="utf-8").count(self.public_key), 1)

    def test_unknown_ownership_type_conflict_and_owner_home_change_fail_before_writes(self):
        self.home.joinpath("config.txt").mkdir()
        run_mock, calls = self.mocked_run()
        with self.assertRaisesRegex(deploy_files.DeployFilesError, "incompatible type"):
            self._run_deploy(run_mock)
        self.assertEqual(calls, [])

        self.home.joinpath("config.txt").rmdir()
        other_owner = SimpleNamespace(**{**vars(self.owner), "pw_uid": self.owner.pw_uid + 1})
        with self.assertRaisesRegex(deploy_files.DeployFilesError, "existing owner home"):
            self._deploy_with_owner(run_mock, other_owner)
        self.assertEqual(calls, [])

    def _deploy_with_owner(self, run_mock, owner):
        with mock.patch.object(deploy_files.os, "geteuid", return_value=0), \
             mock.patch.object(portable_files, "_os_id", return_value="almalinux"), \
             mock.patch.object(deploy_files.pwd, "getpwnam", return_value=owner), \
             mock.patch.object(deploy_files.subprocess, "run", run_mock), \
             mock.patch.object(deploy_files, "_TARGET_ROOT", self.target), \
             mock.patch.object(deploy_files, "_MIGRATION_ROOT", self.migration_root), \
             mock.patch.object(deploy_files, "_ensure_migration_root"):
            return deploy_files.deploy(self.plan, self.bundle, self.public_key, "aki")

    def test_unknown_numeric_ids_are_rejected_before_tools_or_backup(self):
        unknown_directory = self.root / "unknown-owner"
        unknown_directory.mkdir()
        plan, bundle, report = make_bundle(unknown_directory, uid=987654321, gid=os.getgid())
        self.plan, self.bundle = plan, bundle
        run_mock, calls = self.mocked_run()
        with mock.patch.object(deploy_files.pwd, "getpwuid", side_effect=KeyError):
            with self.assertRaisesRegex(deploy_files.DeployFilesError, "ownership unknown"):
                self._run_deploy(run_mock)
        self.assertEqual(calls, [])

    def test_symlinked_live_ancestor_and_invalid_public_key_fail_before_writes(self):
        outside = self.root / "outside"
        outside.mkdir()
        shutil_link = self.target / "home"
        shutil.rmtree(shutil_link)
        shutil_link.symlink_to(outside, target_is_directory=True)
        run_mock, calls = self.mocked_run()
        with self.assertRaisesRegex(deploy_files.DeployFilesError, "ancestor"):
            self._run_deploy(run_mock)
        self.assertEqual(calls, [])
        shutil_link.unlink()
        shutil_link.mkdir()
        (shutil_link / "aki").mkdir()
        self.public_key = "ssh-ed25519 AAAAfirst\nssh-ed25519 AAAAsecond"
        with self.assertRaisesRegex(deploy_files.DeployFilesError, "one OpenSSH public-key line"):
            self._run_deploy(run_mock)
        self.assertEqual(calls, [])

    def test_key_parser_accepts_one_terminal_line_ending_and_rejects_extra_lines(self):
        self.assertEqual(deploy_files._key_line(self.public_key + "\n"), self.public_key)
        self.assertEqual(deploy_files._key_line(self.public_key + "\r\n"), self.public_key)
        for malformed in (self.public_key + "\n\n", self.public_key + "\r\n\n",
                          self.public_key + "\nssh-ed25519 AAAAsecond", self.public_key + "\r",
                          self.public_key + "\x00"):
            with self.subTest(value=repr(malformed)), self.assertRaises(deploy_files.DeployFilesError):
                deploy_files._key_line(malformed)

    def test_cli_reads_standard_lf_and_crlf_key_files(self):
        key_file = self.root / "bootstrap.pub"
        for ending in (b"\n", b"\r\n"):
            with self.subTest(ending=ending):
                ssh_dir = self.home / ".ssh"
                if ssh_dir.exists():
                    shutil.rmtree(ssh_dir)
                key_file.write_bytes(self.public_key.encode("utf-8") + ending)
                run_mock, _ = self.mocked_run()
                result, stdout, stderr = self._run_cli(key_file, run_mock)
                self.assertEqual(result, 0, stderr)
                self.assertEqual(json.loads(stdout)["bootstrap_key"], "preserved")
                authorized = ssh_dir / "authorized_keys"
                self.assertEqual(authorized.read_text(encoding="utf-8"), self.public_key + "\n")

    def test_cli_rejects_multiple_keys_before_any_writes(self):
        key_file = self.root / "bootstrap.pub"
        key_file.write_text(self.public_key + "\nssh-ed25519 "
                            + base64.b64encode(b"second-key").decode() + "\n", encoding="utf-8")
        run_mock, calls = self.mocked_run()
        result, _, stderr = self._run_cli(key_file, run_mock)
        self.assertEqual(result, 2)
        self.assertIn("one OpenSSH public-key line", stderr)
        self.assertEqual(calls, [])
        self.assertFalse((self.home / ".ssh").exists())
        self.assertEqual(list(self.migration_root.iterdir()), [])

    def test_plan_hash_mismatch_stops_before_backup_and_partial_extract_failure_keeps_backup(self):
        self.plan.write_text(json.dumps({**self.report, "bundle_sha256": "0" * 64}), encoding="utf-8")
        run_mock, calls = self.mocked_run()
        with self.assertRaisesRegex(deploy_files.DeployFilesError, "does not match"):
            self._run_deploy(run_mock)
        self.assertEqual(calls, [])

        self.plan.write_text(json.dumps(self.report), encoding="utf-8")
        self.home.joinpath("config.txt").write_text("old", encoding="utf-8")
        failing_run, calls = self.mocked_run(fail_extract=True)
        with self.assertRaisesRegex(deploy_files.DeployFilesError, "backup is retained"):
            self._run_deploy(failing_run)
        backup = next(self.migration_root.glob("pre-restore-*.tar"))
        self.assertTrue(backup.is_file())
        self.assertTrue(any("-xpf" in call for call in calls))

    def test_reserved_migration_backup_destination_is_rejected_before_bundle_validation(self):
        self.plan.write_text(json.dumps({
            **self.report, "selected_paths": ["/var/lib/arcturus-migration"]
        }), encoding="utf-8")
        run_mock, calls = self.mocked_run()
        with self.assertRaisesRegex(deploy_files.DeployFilesError, "overlaps the migration"):
            self._run_deploy(run_mock)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
