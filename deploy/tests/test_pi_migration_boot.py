"""Boot acceptance tests use local fixtures; no SSH or block writes occur."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pi_migration_boot as boot


class FixturePeer:
    """Run the actual remote Python against fixture mount/device metadata."""
    def __init__(self, state, mount):
        self.state, self.mount = state, mount
        self.calls = []
        self.cid = state["target"]["cid"]
        self.size = state["target"]["size_bytes"]
        self.partition = "/dev/mmcblk0p1"
        self.source = self.partition
        self.parent = "/dev/mmcblk0"
        self.label = "CIDATA"
        self.extra_mount = False
        self.extra_label = False
        self.fsroot = "/"

    def run(self, command, data=None, timeout=180):
        self.calls.append(command)
        if command == "uname -r; sha256sum /recovery/boot/kernel.img /recovery/boot/recovery.dtb":
            hashes = self.state["boot_payload"]["sha256"]
            return (self.state["boot_payload"]["recovery_kernel"] + "\n"
                    + hashes["arcturus-recovery-kernel.img"] + "  /recovery/boot/kernel.img\n"
                    + hashes["arcturus-recovery.dtb"] + "  /recovery/boot/recovery.dtb\n").encode()
        if command.startswith("set -eu\ncid="):
            self.retry_command = command
            self.retry_manifest = data
            if self.cid != self.state["target"]["cid"] or self.size != self.state["target"]["size_bytes"]:
                raise boot.BootAcceptanceError("SD card identity differs from frozen flash evidence")
            for name in boot.PAYLOAD_FILES + (boot.CHECKSUM_FILE,):
                path = self.mount / name
                if path.is_symlink() or not path.is_file():
                    raise boot.BootAcceptanceError("boot payload is not a regular file")
                if hashlib.sha256(path.read_bytes()).hexdigest() != self.state["boot_payload"]["sha256"][name]:
                    raise boot.BootAcceptanceError("boot payload checksum differs from frozen flash evidence: " + name)
            expected_manifest = "".join(self.state["boot_payload"]["sha256"][name] + "  " + name + "\n"
                                         for name in boot.PAYLOAD_FILES + (boot.CHECKSUM_FILE,)).encode()
            if data != expected_manifest:
                raise boot.BootAcceptanceError("retry checksum manifest differs from frozen flash evidence")
            return b""
        if command.startswith("set -eu\nprintf 'kernel=%s"):
            return ("kernel=6.18.50+rpt-rpi-2712\n"
                    "boot_id=12345678-1234-1234-1234-123456789abc\n"
                    "uptime=123.45 67.89\n"
                    "observed_at_utc=2026-10-04T12:34:56Z\n"
                    "chosen_boot-count_hex=01\nchosen_tryboot_hex=01\n"
                    "chosen_arg1_hex=00000000\nchosen_rsts_hex=00000000\n"
                    "chosen_partition_hex=00000001\n").encode()
        if command != "sudo -n /usr/bin/python3 -":
            return b""
        original_stat = os.stat
        original_read = Path.read_text
        device_number = self.mount.stat().st_dev

        def fake_stat(path, *args, **kwargs):
            if str(path) == "/dev/mmcblk0p1":
                return SimpleNamespace(st_mode=stat.S_IFBLK | 0o600, st_rdev=device_number)
            return original_stat(path, *args, **kwargs)

        def fake_read(path, *args, **kwargs):
            if str(path) == "/sys/block/mmcblk0/device/cid":
                return self.cid
            if str(path) == "/sys/block/mmcblk0/size":
                return str(self.size // 512)
            return original_read(path, *args, **kwargs)

        def command_output(args):
            if args[0] == "lsblk":
                nodes = [{"path": self.partition, "type": "part", "pkname": self.parent,
                          "label": self.label, "fstype": "vfat"}]
                if self.extra_label:
                    nodes.append(dict(nodes[0], path="/dev/sda1", pkname="/dev/sda"))
                return json.dumps({"blockdevices": nodes}).encode()
            if args[0] == "findmnt":
                nodes = [{"source": self.source, "target": str(self.mount), "fstype": "vfat",
                          "options": "rw", "maj:min": f"{os.major(device_number)}:{os.minor(device_number)}",
                          "fsroot": self.fsroot}]
                if self.extra_mount:
                    nodes.append(dict(nodes[0], target="/other-mount"))
                return json.dumps({"filesystems": nodes}).encode()
            raise AssertionError("unexpected subprocess command")

        stdout, stderr = io.StringIO(), io.StringIO()
        with (mock.patch("os.stat", side_effect=fake_stat),
              mock.patch.object(Path, "read_text", fake_read),
              mock.patch("subprocess.check_output", side_effect=command_output),
              mock.patch("os.sync"), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr)):
            try:
                exec(compile(data, "fixture-remote", "exec"), {})
            except SystemExit as error:
                if error.code:
                    raise boot.BootAcceptanceError(stderr.getvalue().strip()) from None
        return stdout.getvalue().encode()


class BootAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.mount = Path(self.temporary.name)
        self.state = {"target": {"device": "/dev/mmcblk0", "cid": "a" * 32,
                                 "size_bytes": 16 * 1024**3}, "nonce": "b" * 32,
                      "phase": "flash-verified"}
        self.alma_config = b"[all]\narm_64bit=1\nkernel=kernel_2712.img\n"
        for name in boot.PAYLOAD_FILES:
            content = (boot.RESCUE_CONFIG if name == "config.txt" else self.alma_config
                       if name in ("alma-config.txt", "tryboot.txt") else (name + " payload\n").encode())
            (self.mount / name).write_bytes(content)
        self.manifest = "".join(hashlib.sha256((self.mount / name).read_bytes()).hexdigest()
                                + "  " + name + "\n" for name in boot.PAYLOAD_FILES).encode()
        (self.mount / boot.CHECKSUM_FILE).write_bytes(self.manifest)
        boot.record_boot_payload(self.state, self.manifest, b"6.18.50+rpt-rpi-2712\n")
        self.peer = FixturePeer(self.state, self.mount)

    def test_writer_manifest_is_complete_and_frozen_to_session_card(self):
        self.assertEqual(self.state["boot_payload"]["target"], self.state["target"])
        self.assertEqual(self.state["boot_payload"]["sha256"][boot.CHECKSUM_FILE],
                         hashlib.sha256(self.manifest).hexdigest())
        for damaged in (self.manifest.splitlines()[0] + b"\n", self.manifest + self.manifest.splitlines()[0] + b"\n",
                        self.manifest.replace(b"network-config", b"../network-config")):
            with self.subTest(manifest=damaged[:20]), self.assertRaises(boot.BootAcceptanceError):
                boot.record_boot_payload(copy.deepcopy(self.state), damaged, "6.18.50+rpt-rpi-2712")
        self.state["target"]["cid"] = "c" * 32
        with self.assertRaises(boot.BootAcceptanceError):
            boot.verify_boot_payload(self.peer, self.state)
        self.assertEqual(self.peer.calls, [])

    def test_new_flash_evidence_revokes_previous_acceptance_flags(self):
        self.state.update(fallback_verified=True, environment_verified=True, boot_promoted=True)
        boot.record_boot_payload(self.state, self.manifest, "6.18.50+rpt-rpi-2712")
        self.assertTrue(all(name not in self.state for name in
                            ("fallback_verified", "environment_verified", "boot_promoted")))

    def test_dynamic_mount_is_accepted_without_returning_seed_contents(self):
        report = boot.verify_boot_payload(self.peer, self.state)
        self.assertEqual(report["mountpoint"], str(self.mount))
        self.assertNotIn("user-data payload", json.dumps(report))
        self.assertEqual(report["sha256"], self.state["boot_payload"]["sha256"])

    def test_reject_wrong_card_partition_or_nonunique_mount(self):
        for attribute, value in (("cid", "c" * 32), ("size", 32 * 1024**3),
                                 ("partition", "/dev/sda1"), ("parent", "/dev/sda"),
                                 ("label", "OTHER"), ("source", "/dev/mmcblk0p2"),
                                 ("source", "/dev/mmcblk0p1[/subdirectory]"),
                                 ("fsroot", "/subdirectory"), ("extra_mount", True), ("extra_label", True)):
            peer = FixturePeer(self.state, self.mount)
            setattr(peer, attribute, value)
            with self.subTest(attribute=attribute, value=value), self.assertRaises(boot.BootAcceptanceError):
                boot.verify_boot_payload(peer, self.state)

    def test_modified_rescue_seed_or_default_configuration_is_rejected(self):
        for name in ("arcturus-recovery.img", "arcturus-recovery-kernel.img", "arcturus-recovery.dtb",
                     "network-config", "user-data", "config.txt", "alma-config.txt", boot.CHECKSUM_FILE):
            file = self.mount / name
            before = file.read_bytes()
            file.write_bytes(before + b"changed\n")
            with self.subTest(file=name), self.assertRaises(boot.BootAcceptanceError):
                boot.verify_boot_payload(self.peer, self.state)
            file.write_bytes(before)
        (self.mount / "config.txt").write_bytes(self.alma_config)
        with self.assertRaises(boot.BootAcceptanceError):
            boot.verify_boot_payload(self.peer, self.state)

    def test_failed_fallback_never_authorizes_promotion(self):
        self.state["fallback_verified"] = True
        self.state["environment_verified"] = True
        rebooted = []
        def failing_guard(peer):
            raise boot.BootAcceptanceError("a namespace retains the SD")
        with self.assertRaises(boot.BootAcceptanceError):
            boot.test_fallback(self.peer, self.state, normal_reboot=lambda peer: rebooted.append(peer),
                               wait_recovery=lambda state: self.peer, guard=failing_guard)
        self.assertEqual(rebooted, [self.peer])
        self.assertIs(self.state["fallback_verified"], False)
        self.assertEqual(self.state["phase"], "fallback-boot-requested")
        calls = len(self.peer.calls)
        with self.assertRaises(boot.BootAcceptanceError):
            boot.promote_boot(self.peer, self.state)
        self.assertEqual(len(self.peer.calls), calls)
        self.assertEqual((self.mount / "config.txt").read_bytes(), boot.RESCUE_CONFIG)

    def test_changed_recovery_kernel_or_dtb_fails_fallback(self):
        recovery = mock.Mock()
        recovery.run.return_value = b"wrong-kernel\n"
        with self.assertRaises(boot.BootAcceptanceError):
            boot.test_fallback(self.peer, self.state, normal_reboot=lambda peer: None,
                               wait_recovery=lambda state: recovery, guard=lambda peer: None)
        self.assertIs(self.state["fallback_verified"], False)

    def test_successful_fallback_and_environment_enable_atomic_promotion(self):
        guarded = []
        returned = boot.test_fallback(self.peer, self.state, normal_reboot=lambda peer: None,
                                     wait_recovery=lambda state: self.peer, guard=guarded.append)
        self.assertIs(returned, self.peer)
        self.assertEqual(guarded, [self.peer])
        self.assertEqual(self.state["phase"], "fallback-verified")
        self.state["environment_verified"] = True
        original_replace = os.replace
        with mock.patch("os.replace", wraps=original_replace) as replace:
            report = boot.promote_boot(self.peer, self.state)
        self.assertEqual(replace.call_count, 1)
        self.assertEqual((self.mount / "config.txt").read_bytes(), self.alma_config)
        self.assertEqual(report["sha256"]["config.txt"], hashlib.sha256(self.alma_config).hexdigest())
        self.assertIs(self.state["boot_promoted"], True)
        self.assertEqual(self.state["phase"], "alma-promoted")
        self.assertNotIn("/bin/reboot-tryboot", self.peer.calls)
        # A lost first reply must be retryable without another replacement.
        with mock.patch("os.replace", wraps=original_replace) as replace:
            boot.promote_boot(self.peer, self.state)
        replace.assert_not_called()

    def test_changed_payload_blocks_promotion_before_any_replacement(self):
        self.state.update(fallback_verified=True, environment_verified=True)
        (self.mount / "arcturus-recovery.dtb").write_bytes(b"wrong device tree")
        with mock.patch("os.replace") as replace, self.assertRaises(boot.BootAcceptanceError):
            boot.promote_boot(self.peer, self.state)
        replace.assert_not_called()
        self.assertNotIn("boot_promoted", self.state)
        self.assertEqual((self.mount / "config.txt").read_bytes(), boot.RESCUE_CONFIG)

    def test_payload_change_between_client_preflight_and_remote_commit_is_rejected(self):
        self.state.update(fallback_verified=True, environment_verified=True)
        original_run = self.peer.run
        inspections = 0
        def mutate_before_second_inspection(command, data=None, timeout=180):
            nonlocal inspections
            inspections += 1
            if inspections == 2:
                (self.mount / "network-config").write_bytes(b"changed seed during promotion")
            return original_run(command, data, timeout)
        with (mock.patch.object(self.peer, "run", side_effect=mutate_before_second_inspection),
              mock.patch("os.replace") as replace, self.assertRaises(boot.BootAcceptanceError)):
            boot.promote_boot(self.peer, self.state)
        replace.assert_not_called()
        self.assertEqual((self.mount / "config.txt").read_bytes(), boot.RESCUE_CONFIG)
        self.assertNotIn("boot_promoted", self.state)

    def test_precommit_fsync_failure_keeps_rescue_default_and_removes_owned_partial(self):
        self.state.update(fallback_verified=True, environment_verified=True)
        with (mock.patch("os.fsync", side_effect=OSError("fixture fsync failure")),
              mock.patch("os.replace") as replace, self.assertRaises(boot.BootAcceptanceError)):
            boot.promote_boot(self.peer, self.state)
        replace.assert_not_called()
        self.assertEqual((self.mount / "config.txt").read_bytes(), boot.RESCUE_CONFIG)
        self.assertEqual(list(self.mount.glob(".arcturus-config-*.new")), [])
        self.assertNotIn("boot_promoted", self.state)

    def test_preconditions_require_boolean_acceptance_and_no_stale_partial_trust(self):
        for fallback, environment in ((False, True), (True, False), ("verified", True), (True, "verified")):
            self.state.update(fallback_verified=fallback, environment_verified=environment)
            with self.subTest(fallback=fallback, environment=environment), self.assertRaises(boot.BootAcceptanceError):
                boot.promote_boot(self.peer, self.state)
        self.assertEqual(self.peer.calls, [])
        self.state.update(fallback_verified=True, environment_verified=True)
        pending = self.mount / (".arcturus-config-" + self.state["nonce"] + ".new")
        pending.write_bytes(b"untrusted stale partial")
        with self.assertRaises(boot.BootAcceptanceError):
            boot.promote_boot(self.peer, self.state)
        self.assertEqual(pending.read_bytes(), b"untrusted stale partial")
        self.assertEqual((self.mount / "config.txt").read_bytes(), boot.RESCUE_CONFIG)

    def test_one_shot_reboot_requires_guard_and_preserved_recovery(self):
        guarded, requested = [], []
        boot.reboot_alma(self.peer, self.state, guard=guarded.append, request_reboot=requested.append)
        self.assertEqual(guarded, [self.peer])
        self.assertEqual(requested, [self.peer])
        self.assertEqual(self.state["phase"], "alma-boot-requested")
        self.assertIn("test -f /run/alma-flash-verified && sync", self.peer.calls)

    def test_retry_requires_requested_unpromoted_state_and_frozen_payload_before_peer_use(self):
        for update in ({"phase": "flash-verified"}, {"phase": "alma-promoted"},
                       {"phase": "alma-boot-requested", "boot_promoted": True}):
            state = copy.deepcopy(self.state)
            state.update(update)
            with self.subTest(update=update), self.assertRaises(boot.BootAcceptanceError):
                boot.retry_alma(self.peer, state, guard=lambda peer: self.fail("guard must not run"),
                                request_reboot=lambda peer: self.fail("reboot must not run"))
        state = copy.deepcopy(self.state)
        state["phase"] = "alma-boot-requested"
        state.pop("boot_payload")
        with self.assertRaises(boot.BootAcceptanceError):
            boot.retry_alma(self.peer, state, guard=lambda peer: self.fail("guard must not run"),
                            request_reboot=lambda peer: self.fail("reboot must not run"))
        self.assertEqual(self.peer.calls, [])

    def test_retry_hash_mismatch_blocks_reboot_without_manufacturing_acceptance(self):
        self.state["phase"] = "alma-boot-requested"
        self.state.pop("/run/alma-flash-verified", None)
        self.state.pop("fallback_verified", None)
        self.state.pop("environment_verified", None)
        self.state.pop("boot_promoted", None)
        (self.mount / "user-data").write_bytes(b"changed cloud-init seed")
        rebooted = []
        with self.assertRaises(boot.BootAcceptanceError):
            boot.retry_alma(self.peer, self.state, guard=lambda peer: None,
                            request_reboot=rebooted.append)
        self.assertEqual(rebooted, [])
        self.assertNotIn("boot_retry_events", self.state)
        self.assertEqual(self.state["phase"], "alma-boot-requested")
        self.assertNotIn("/run/alma-flash-verified", self.peer.calls)
        self.assertFalse(any(self.state.get(flag) is True for flag in
                             ("fallback_verified", "environment_verified", "boot_promoted")))

    def test_retry_card_identity_mismatch_blocks_reboot(self):
        self.state["phase"] = "alma-boot-requested"
        self.peer.cid = "c" * 32
        rebooted = []
        with self.assertRaises(boot.BootAcceptanceError):
            boot.retry_alma(self.peer, self.state, guard=lambda peer: None,
                            request_reboot=rebooted.append)
        self.assertEqual(rebooted, [])
        self.assertNotIn("boot_retry_events", self.state)
        self.assertEqual(self.state["phase"], "alma-boot-requested")

    def test_retry_script_preflights_mount_tools_and_does_not_depend_on_mountpoint_binary(self):
        command = boot._retry_boot_inspection_command(self.state)
        preflight = command.index("for utility in ")
        mkdir = command.index('mkdir -m 700 "$mountpoint"')
        mount = command.index('mount -t vfat -o ro,nosuid,nodev,noexec')
        self.assertLess(preflight, mkdir)
        self.assertLess(preflight, mount)
        for tool in boot._RETRY_REQUIRED_TOOLS:
            self.assertIn(tool, command[preflight:mkdir])
        self.assertIn('findmnt -rn --mountpoint "$mountpoint"', command)
        self.assertNotIn("mountpoint -q", command)

    def test_generated_cleanup_preserves_exit_status_and_fails_on_unmount_error(self):
        command = boot._retry_boot_inspection_command(self.state)
        start = command.index("mount_state() {")
        mount_state_end = command.index("\nif mount_state; then", start)
        cleanup_start = command.index("cleanup() {", mount_state_end)
        cleanup_end = command.index("trap cleanup EXIT", cleanup_start)
        functions = command[start:mount_state_end] + "\n" + command[cleanup_start:cleanup_end]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim_dir = root / "bin"
            shim_dir.mkdir()
            state_file = root / "mount-state"
            shims = {
                "findmnt": """#!/bin/sh
if [ "$2" = "--mountpoint" ]; then
    [ "$(cat "$MOUNT_STATE_FILE")" = mounted ]
    exit $?
fi
printf '%s\\n' '/dev/mmcblk0p1 vfat ro,nosuid,nodev,noexec'
""",
                "umount": """#!/bin/sh
if [ "${FAIL_UMOUNT:-0}" = 1 ]; then exit 1; fi
printf '%s\\n' unmounted > "$MOUNT_STATE_FILE"
""",
                "rmdir": "#!/bin/sh\nexit 0\n",
            }
            for name, content in shims.items():
                path = shim_dir / name
                path.write_text(content)
                path.chmod(0o755)

            def run_cleanup(initial_status, *, fail_unmount=False):
                state_file.write_text("mounted\n")
                shell = ("mountpoint=/run/backup-boot\n" + functions
                         + "trap cleanup EXIT\nexit " + str(initial_status) + "\n")
                environment = dict(os.environ, PATH=str(shim_dir) + ":/usr/bin:/bin",
                                   MOUNT_STATE_FILE=str(state_file),
                                   FAIL_UMOUNT="1" if fail_unmount else "0")
                return subprocess.run(["/bin/bash", "-c", shell], env=environment,
                                      capture_output=True, text=True)

            successful_cleanup = run_cleanup(0)
            self.assertEqual(successful_cleanup.returncode, 0, successful_cleanup.stderr)
            prior_failure_cleanup = run_cleanup(23)
            self.assertEqual(prior_failure_cleanup.returncode, 23, prior_failure_cleanup.stderr)
            failed_unmount_cleanup = run_cleanup(0, fail_unmount=True)
            self.assertNotEqual(failed_unmount_cleanup.returncode, 0, failed_unmount_cleanup.stderr)

    def test_retry_unmount_guard_and_event_persist_precede_one_shot_reboot(self):
        self.state["phase"] = "alma-boot-requested"
        guards = []
        callbacks = []

        def guard(peer):
            guards.append(len(peer.calls))
            if len(guards) == 2:
                self.assertTrue(any(call.startswith("set -eu\ncid=") for call in peer.calls))
                self.assertIn("umount \"$mountpoint\"", peer.retry_command)
                self.assertIn("sha256sum -c -", peer.retry_command)
                self.assertLess(peer.retry_command.index('cd "$mountpoint"\nsha256sum -c -'),
                                peer.retry_command.index("sha256sum -c -"))
                self.assertLess(peer.retry_command.index("if ! cd /; then"),
                                peer.retry_command.index("if mount_state; then",
                                                         peer.retry_command.index("cleanup()")))

        def request_reboot(peer):
            callbacks.append((peer, copy.deepcopy(self.state.get("boot_retry_events"))))
            self.assertEqual(self.state["phase"], "alma-boot-requested")
            self.assertEqual(len(self.state["boot_retry_events"]), 1)
            self.assertTrue(any(call.startswith("set -eu\nprintf 'kernel=%s") for call in peer.calls))
            peer.calls.append("/bin/reboot-tryboot")
            return "requested"

        result = boot.retry_alma(self.peer, self.state, guard=guard, request_reboot=request_reboot)
        self.assertEqual(result, "requested")
        self.assertEqual(len(guards), 2)
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(self.state["phase"], "alma-boot-requested")
        self.assertEqual(set(self.state["boot_retry_events"][0]), {
            "kernel", "boot_id", "uptime", "observed_at_utc", "chosen_boot-count_hex", "chosen_tryboot_hex",
            "chosen_arg1_hex", "chosen_rsts_hex", "chosen_partition_hex"})
        self.assertEqual(self.peer.calls[-1], "/bin/reboot-tryboot")
        self.assertEqual(self.state.get("fallback_verified"), None)
        self.assertEqual(self.state.get("environment_verified"), None)
        self.assertEqual(self.state.get("boot_promoted"), None)


if __name__ == "__main__":
    unittest.main()
