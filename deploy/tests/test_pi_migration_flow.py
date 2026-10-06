"""Cross-step acceptance checks; all peers and reboot requests are mocked."""
import hashlib
import json
import os
import stat
import sys
import tempfile
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pi_migrate as migration


class FlowTests(unittest.TestCase):
    def test_generated_resume_inspection_rejects_missing_runtime_volume(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            stage = Path(temporary) / "session"
            stage.mkdir()
            runtime_path = stage / "runtime-plan.json"
            host_path = stage / "host-plan.json"
            runtime_path.write_text("runtime")
            host_path.write_text("host")
            runtime = {"containers": [], "networks": [{"name": "app-net"}],
                       "volumes": [{"name": "app-data"}], "image_exports": {"example/app:1": "image.tar"}}
            host = {"units": []}
            hashes = {"runtime-plan.json": hashlib.sha256(b"runtime").hexdigest(),
                      "host-plan.json": hashlib.sha256(b"host").hexdigest()}
            program = migration._runtime_resume_inspection_program(runtime, host, hashes, str(stage))
            original_stat = Path.stat
            original_lstat = Path.lstat
            def root_info(method):
                def wrapped(path, *args, **kwargs):
                    info = method(path, *args, **kwargs)
                    values = list(info)
                    values[4] = 0
                    values[0] &= ~0o022
                    return os.stat_result(values)
                return wrapped
            def run(args, **kwargs):
                if args[:3] == ["/usr/bin/podman", "image", "exists"]:
                    return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
                if args[:3] == ["/usr/bin/podman", "network", "exists"]:
                    return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
                return type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            with mock.patch.object(Path, "stat", root_info(original_stat)), \
                 mock.patch.object(Path, "lstat", root_info(original_lstat)), \
                 mock.patch.object(migration.subprocess, "run", side_effect=run), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "runtime state"):
                    exec(compile(program, "fixture-resume-runtime", "exec"), {})

    def test_generated_resume_inspection_ignores_host_units_not_restored_yet(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            stage = Path(temporary) / "session"
            stage.mkdir()
            (stage / "runtime-plan.json").write_text("runtime")
            (stage / "host-plan.json").write_text("host")
            runtime = {"containers": [], "networks": [{"name": "app-net"}],
                       "volumes": [{"name": "app-data"}], "image_exports": {"example/app:1": "image.tar"}}
            # These are the source states. Host restoration has not run yet, so
            # the target's inactive/disabled unit state must not block this gate.
            host = {"units": [{"name": "source.service", "active": True, "enablement": "enabled"}]}
            hashes = {"runtime-plan.json": hashlib.sha256(b"runtime").hexdigest(),
                      "host-plan.json": hashlib.sha256(b"host").hexdigest()}
            program = migration._runtime_resume_inspection_program(runtime, host, hashes, str(stage))
            original_stat = Path.stat
            original_lstat = Path.lstat
            def root_info(method):
                def wrapped(path, *args, **kwargs):
                    info = method(path, *args, **kwargs)
                    values = list(info)
                    values[4] = 0
                    values[0] &= ~0o022
                    return os.stat_result(values)
                return wrapped
            def run(args, **kwargs):
                if args[0] == "/usr/bin/systemctl":
                    raise AssertionError("prehost gate queried an unrestored host unit")
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            stdout = io.StringIO()
            with mock.patch.object(Path, "stat", root_info(original_stat)), \
                 mock.patch.object(Path, "lstat", root_info(original_lstat)), \
                 mock.patch.object(migration.subprocess, "run", side_effect=run), \
                 contextlib.redirect_stdout(stdout):
                exec(compile(program, "fixture-resume-unrestored-host", "exec"), {})
            self.assertEqual(json.loads(stdout.getvalue())["status"], "verified")

    def _resume_fixture(self, directory):
        files_plan = {"selected_paths": ["/home/aki"]}
        runtime_plan = {"containers": [], "networks": [], "volumes": [], "image_exports": {}}
        host_plan = {"units": []}
        for name, document in (("files-plan.json", files_plan), ("runtime-plan.json", runtime_plan),
                               ("host-plan.json", host_plan)):
            (directory / name).write_text(json.dumps(document))
        bundle = directory / "portable-files.tar.gz"
        bundle.write_bytes(b"frozen bundle")
        files_result = {"status": "deployed", "bootstrap_key": "preserved",
                        "selected_paths": ["/home/aki"]}
        (directory / "files-restoration-result.json").write_text(json.dumps(files_result))
        artifacts = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                     for name in ("files-plan.json", "runtime-plan.json", "host-plan.json", "portable-files.tar.gz")}
        state = {"phase": "environment-restoring", "restoration_acceptance": "verified",
                 "directory": str(directory), "nonce": "a" * 32, "restoration_artifacts": artifacts}
        return state, runtime_plan, host_plan

    def test_finish_restore_runtime_mismatch_blocks_host_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state, runtime, host = self._resume_fixture(directory)
            peer = mock.Mock()
            with mock.patch.object(migration, "alma_peer", return_value=peer), \
                 mock.patch.object(migration, "verified_cloud_init_status", return_value={}), \
                 mock.patch.object(migration.boot_acceptance, "verify_boot_payload"), \
                 mock.patch.object(migration, "verify_resume_runtime",
                                   side_effect=migration.MigrationError("runtime mismatch")), \
                 mock.patch.object(migration, "upload_resume_host_adapter") as upload_adapter, \
                 mock.patch.object(migration, "install_cloudflared_atomic") as install, \
                 mock.patch.object(migration, "upload_checked") as upload, \
                 mock.patch.object(migration, "state_save"):
                with self.assertRaisesRegex(migration.MigrationError, "runtime mismatch"):
                    migration.finish_restore(state)
            upload_adapter.assert_not_called()
            install.assert_not_called()
            upload.assert_not_called()
            self.assertEqual([call.args[0] for call in peer.run.call_args_list],
                             ["test -f /var/lib/arcturus-migration-firstboot && sudo -n true"])
            self.assertFalse((directory / "host-restoration-result.json").exists())

    def test_finish_restore_resumes_without_redeploy_or_runtime_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state, runtime, host = self._resume_fixture(directory)
            peer = mock.Mock()
            peer.run.side_effect = [b"", json.dumps({"status": "restored"}).encode()]
            with mock.patch.object(migration, "alma_peer", return_value=peer), \
                 mock.patch.object(migration, "verified_cloud_init_status", return_value={}), \
                 mock.patch.object(migration.boot_acceptance, "verify_boot_payload"), \
                 mock.patch.object(migration, "verify_resume_runtime", return_value={"status": "verified"}), \
                 mock.patch.object(migration, "upload_resume_host_adapter") as upload_adapter, \
                 mock.patch.object(migration, "upload_checked") as upload_checked, \
                 mock.patch.object(migration, "state_save"), \
                 mock.patch.object(migration, "configure_exposure") as exposure, \
                 mock.patch.object(migration, "verify_environment") as verify:
                migration.finish_restore(state)
            self.assertEqual(state["phase"], "environment-restored")
            self.assertEqual(json.loads((directory / "host-restoration-result.json").read_text()),
                             {"status": "restored"})
            upload_adapter.assert_called_once()
            upload_checked.assert_not_called()
            commands = [call.args[0] for call in peer.run.call_args_list]
            self.assertEqual(len(commands), 2)
            self.assertIn("restore-host.py", commands[1])
            self.assertNotIn("deploy-files.py", " ".join(commands))
            self.assertNotIn("pi_migration_runtime.py", " ".join(commands))
            exposure.assert_called_once_with(state, peer, runtime, host)
            verify.assert_called_once_with(state)

    def test_headless_seed_retry_rewrites_identical_bounded_payload(self):
        peer = mock.Mock()
        peer.run.side_effect = [migration.MigrationError("reply lost"), b""]
        seed = {name: "test data" for name in ("user-data", "meta-data", "network-config")}
        with mock.patch.object(migration.time, "sleep"):
            migration.upload_seed(peer, seed)
        first, second = peer.run.call_args_list
        self.assertEqual(first, second)
        with migration.tarfile.open(fileobj=migration.io.BytesIO(first.args[1])) as archive:
            self.assertEqual(archive.getnames(), list(seed))
            self.assertTrue(all(info.mode == 0o600 for info in archive))
        self.assertIn("tar -xf -", first.args[0])
        self.assertIn("test ! -L /run/alma-seed", first.args[0])

    def test_detached_flash_observation_reconnects_without_launching_again(self):
        state = {"phase": "flash-started", "nonce": "a" * 32,
                 "flash_job": "/run/alma-flash-job-" + "a" * 32}
        peer = mock.Mock()
        peer.run.side_effect = [migration.MigrationError("disconnected"), b"RUNNING", b"EXIT 0", b"", b"manifest", b"kernel"]
        with mock.patch.object(migration, "recovery_peer", return_value=peer), \
             mock.patch.object(migration, "check_destination"), mock.patch.object(migration, "guard"), \
             mock.patch.object(migration, "state_save"), mock.patch.object(migration.time, "sleep"), \
             mock.patch.object(migration.boot_acceptance, "record_boot_payload") as record:
            migration.finish_flash(state)
        self.assertEqual(state["phase"], "flash-verified")
        record.assert_called_once_with(state, b"manifest", b"kernel")
        self.assertFalse(any("bash -s" in call.args[0] for call in peer.run.call_args_list))

    def test_detached_flash_failure_never_qualifies_for_boot(self):
        state = {"phase": "flash-started", "nonce": "a" * 32,
                 "flash_job": "/run/alma-flash-job-" + "a" * 32}
        peer = mock.Mock()
        peer.run.return_value = b"EXIT 1"
        with mock.patch.object(migration, "recovery_peer", return_value=peer), \
             mock.patch.object(migration, "check_destination"), \
             mock.patch.object(migration.boot_acceptance, "record_boot_payload") as record:
            with self.assertRaisesRegex(migration.MigrationError, "keep RAM powered"):
                migration.finish_flash(state)
        record.assert_not_called()
        self.assertEqual(state["phase"], "flash-started")
        with self.assertRaisesRegex(migration.MigrationError, "refusing to launch"):
            migration.finish_flash({"phase": "backup-verified", "nonce": "a" * 32})

    def test_image_upload_resumes_a_matching_prefix_and_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "image.xz"
            target = directory / "ram-image.xz"
            payload = b"signed-image-test-data" * 100000
            source.write_bytes(payload)
            target.write_bytes(payload[:400000])

            class LocalPeer(migration.Peer):
                def __init__(self):
                    super().__init__({}, recovery=True)

                def run(self, command, **kwargs):
                    data = target.read_bytes()
                    return (str(len(data)) + "\n" + hashlib.sha256(data).hexdigest() + "  /run/alma.raw.xz\n").encode()

                def command(self, command):
                    mode = "ab" if ">>" in command else "wb"
                    return [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).open(sys.argv[2]).write(sys.stdin.buffer.read())", str(target), mode]

            peer = LocalPeer()
            with mock.patch.object(migration, "check_destination"):
                peer.upload(source, "/run/alma.raw.xz", resume=True)
            self.assertEqual(target.read_bytes(), payload)
            target.write_bytes(b"corrupt prefix")
            with mock.patch.object(migration.subprocess, "Popen") as process:
                with self.assertRaisesRegex(migration.MigrationError, "prefix differs"):
                    peer.upload(source, "/run/alma.raw.xz", resume=True)
                process.assert_not_called()
            self.assertEqual(target.read_bytes(), b"corrupt prefix")
            with self.assertRaisesRegex(migration.MigrationError, "limited to the RAM OS image"):
                peer.upload(source, "/dev/mmcblk0", resume=True)

    def test_existing_firewall_allows_only_restored_ports_and_homekit_mdns(self):
        peer = mock.Mock()
        peer.run.side_effect = [b"[51826]", b"active", b"public"] + [b"success"] * 6
        state = {"network": {"interface": "eth0"}}
        runtime = {"containers": [{"desired_running": True, "command": ["podman", "create", "--publish", "0.0.0.0:3000:3000/tcp"]}]}
        host = {"units": [{"name": "homekit-wol.service", "active": True}]}
        with mock.patch.object(migration, "state_save"):
            migration.configure_exposure(state, peer, runtime, host)
        commands = [call.args[0] for call in peer.run.call_args_list]
        self.assertIn("sudo -n firewall-cmd --zone=public --permanent --add-port=3000/tcp", commands)
        self.assertIn("sudo -n firewall-cmd --zone=public --add-port=51826/tcp", commands)
        self.assertIn("sudo -n firewall-cmd --zone=public --add-service=mdns", commands)
        self.assertEqual(state["host_tcp_ports"], [3000, 51826])
        self.assertTrue(state["firewall_configured"])

    def test_inactive_firewall_is_not_started(self):
        peer = mock.Mock()
        peer.run.return_value = b"inactive"
        state = {}
        with mock.patch.object(migration, "state_save"):
            migration.configure_exposure(state, peer, {"containers": []}, {"units": []})
        peer.run.assert_called_once_with("sudo -n systemctl is-active firewalld || true")
        self.assertFalse(state["firewall_configured"])

    def test_compressed_backup_input_change_cannot_reuse_prior_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            artifact = directory / "rootfs.tar.gz"
            artifact.write_bytes(b"original")
            target = {"cid": "c" * 32}
            manifest = {"offline": True, "target": target, "files": {artifact.name:
                        {"bytes": 8, "sha256": hashlib.sha256(b"original").hexdigest()}}}
            (directory / "manifest.json").write_text(json.dumps(manifest))
            state = {"directory": temporary, "target": target}
            with mock.patch.object(migration, "check_destination"):
                migration.check_backup_inputs(state)
                artifact.write_bytes(b"replaced")
                with self.assertRaisesRegex(migration.MigrationError, "input changed"):
                    migration.check_backup_inputs(state)

    def test_minimal_image_can_defer_tar_install_but_requires_bootstrap_tools(self):
        metadata = {"compressed_sha256": "a" * 64, "raw_sha256": "b" * 64, "raw_bytes": 4096}
        target = {"device": "/dev/mmcblk0", "cid": "c" * 32, "size_bytes": 8192}
        report = {"nonce": "d" * 32, "target": target, "image": metadata, "is_almalinux": True,
                  "gnu_tar": False, "tools": {name: "/usr/bin/" + name for name in
                  ("python3", "rpm", "cloud-init", "systemctl", "sshd", "useradd", "usermod", "groupmod")}}
        with tempfile.TemporaryDirectory() as temporary:
            state = {"directory": temporary, "phase": "backup-verified", "image": dict(metadata, filename="image.xz"),
                     "nonce": report["nonce"], "target": target}
            peer = mock.Mock()
            peer.run.return_value = json.dumps(report).encode()
            with mock.patch.object(migration, "image_metadata", return_value=metadata), \
                 mock.patch.object(migration, "recovery_peer", return_value=peer), \
                 mock.patch.object(migration, "guard"), mock.patch.object(migration, "state_save"):
                migration.inspect_image(state)
                self.assertTrue((Path(temporary) / "image-inspection.json").is_file())
                del report["tools"]["sshd"]
                peer.run.return_value = json.dumps(report).encode()
                with self.assertRaisesRegex(migration.MigrationError, "bootstrap dependency: sshd"):
                    migration.inspect_image(state)

    def test_owner_gid_is_reserved_before_cloud_init_creates_supplementary_groups(self):
        inventory = {"hostname": "pi", "users": [{"name": "aki", "uid": 1000, "gid": 1000, "home": "/home/aki"}],
                     "groups": [{"name": "gpio", "members": ["aki"]}], "subuid": "", "subgid": ""}
        state = {"user": "aki", "nonce": "a" * 32, "network": {"mac": "aa:bb:cc:dd:ee:ff", "interface": "eth0"}}
        seed = migration.cloud_seed(state, inventory, "ssh-ed25519 AAAA")
        self.assertIn("bootcmd:", seed["user-data"])
        self.assertIn("groupadd --gid 1000 aki", seed["user-data"])
        self.assertIn('groups: ["gpio", "wheel"]', seed["user-data"])

    def test_reachable_cloud_init_verification_failure_is_reported_without_repeating_discovery(self):
        peer = mock.Mock()
        with mock.patch.object(migration, "alma_peer", return_value=peer) as discovery, \
             mock.patch.object(migration, "verified_cloud_init_status",
                               side_effect=migration.MigrationError("cloud-init failed")) as verify_status, \
             mock.patch.object(migration.time, "sleep") as sleep:
            with self.assertRaisesRegex(migration.MigrationError, "cloud-init failed"):
                migration.wait_alma({})
        discovery.assert_called_once()
        verify_status.assert_called_once_with(peer, {})
        sleep.assert_not_called()

    def test_final_reboot_requires_both_promotion_and_updated_packages(self):
        for state in ({}, {"boot_promoted": True}, {"packages_upgraded": True}):
            with self.subTest(state=state), mock.patch.object(migration, "alma_peer") as peer:
                with self.assertRaises(migration.MigrationError):
                    migration.reboot_final(state)
                peer.assert_not_called()

    def test_final_reboot_checks_boot_payload_and_saves_revoked_health_before_reboot(self):
        state = {"boot_promoted": True, "packages_upgraded": True, "environment_verified": True}
        peer = mock.Mock()
        events = []
        peer.run.side_effect = lambda *a, **k: events.append(("reboot", dict(state)))
        with mock.patch.object(migration, "alma_peer", return_value=peer), \
             mock.patch.object(migration.boot_acceptance, "verify_boot_payload") as verify, \
             mock.patch.object(migration, "state_save", side_effect=lambda s: events.append(("save", dict(s)))):
            migration.reboot_final(state)
        verify.assert_called_once_with(peer, state, require_rescue_default=False)
        self.assertEqual([kind for kind, unused in events], ["save", "reboot"])
        self.assertFalse(events[0][1]["environment_verified"])
        self.assertEqual(events[0][1]["phase"], "final-boot-requested")

    def test_only_active_disabled_source_timers_are_resumed(self):
        units = [
            {"name": "active-disabled.timer", "active": True, "enabled": False},
            {"name": "inactive.timer", "active": False, "enabled": False},
            {"name": "active-enabled.timer", "active": True, "enabled": True},
            {"name": "active-disabled.service", "active": True, "enabled": False},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "host-plan.json").write_text(json.dumps({"units": units}))
            (directory / "host-restoration-result.json").write_text("{}")
            peer = mock.Mock()
            with mock.patch.object(migration, "alma_peer", return_value=peer):
                migration.resume_timers({"directory": temporary})
            peer.run.assert_called_once_with("sudo -n systemctl start active-disabled.timer")

    def test_health_observation_rejects_restarting_container_and_revokes_cached_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "runtime-plan.json").write_text(json.dumps({"containers": [
                {"name": "app", "unit": "app.service", "desired_running": True, "command": []}]}))
            (directory / "host-plan.json").write_text(json.dumps({"units": []}))
            peer = mock.Mock()
            peer.run.side_effect = [b"true", b"active", b"0", b"true", b"active", b"1"]
            state = {"directory": temporary, "environment_verified": True}
            with mock.patch.object(migration, "alma_peer", return_value=peer), \
                 mock.patch.object(migration, "state_save"), mock.patch.object(migration.time, "sleep"), \
                 mock.patch.object(migration.boot_acceptance, "verify_boot_payload") as boot:
                with self.assertRaisesRegex(migration.MigrationError, "restarted"):
                    migration.verify_environment(state)
            self.assertFalse(state["environment_verified"])
            boot.assert_not_called()

    def test_health_observation_rejects_incorrect_disabled_enablement(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "runtime-plan.json").write_text(json.dumps({"containers": []}))
            (directory / "host-plan.json").write_text(json.dumps({"units": [
                {"name": "update.timer", "active": True, "enabled": False, "enablement": "disabled"}]}))
            peer = mock.Mock()
            peer.run.side_effect = [b"active", b"enabled"]
            state = {"directory": temporary, "environment_verified": True}
            with mock.patch.object(migration, "alma_peer", return_value=peer), \
                 mock.patch.object(migration, "state_save"):
                with self.assertRaisesRegex(migration.MigrationError, "enablement"):
                    migration.verify_environment(state)
            self.assertFalse(state["environment_verified"])


if __name__ == "__main__":
    unittest.main()
