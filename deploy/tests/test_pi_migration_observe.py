"""Read-only and output-sanitization tests for the Pi observer."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pi_migrate
import pi_migration_observe as observe


NONCE = "0123456789abcdef0123456789abcdef"
CID = "a" * 32
MAC = "dc:a6:32:01:02:03"


def state_data(directory: Path) -> dict:
    return {
        "directory": str(directory), "key": str(directory / "id_ed25519"),
        "known_hosts": str(directory / "known_hosts"),
        "host_key_alias": "arcturus-pi-wired", "host": "192.0.2.10", "user": "aki",
        "nonce": NONCE,
        "target": {"device": "/dev/mmcblk0", "cid": CID, "size_bytes": 64 * 1024**3},
        "network": {"interface": "eth0", "mac": MAC, "address": "192.0.2.10", "prefix": 24},
        "phase": "alma-boot-requested",
    }


def state_fixture(directory: Path) -> dict:
    key = directory / "id_ed25519"
    key.write_text("private test key\n")
    key.chmod(0o600)
    known_hosts = directory / "known_hosts"
    known_hosts.write_text("arcturus-pi-wired ssh-ed25519 AAAATEST\n")
    known_hosts.chmod(0o600)
    session_file = directory / "session.json"
    session_file.write_text("{}\n")
    session_file.chmod(0o600)
    return state_data(directory.resolve())


def recovery_report(**changes) -> bytes:
    values = {
        "nonce": NONCE, "cid": CID, "size": str(64 * 1024**3),
        "root": "rootfs /", "os": "", "kernel": "6.12.0-test",
        "mounts": "proc /proc proc\nsysfs /sys sysfs\n",
        "mac": MAC,
    }
    values.update(changes)
    return (
        f"nonce\n{values['nonce']}\ncid\n{values['cid']}\nsize\n{values['size']}\n"
        f"root\n{values['root']}\nos_begin\n{values['os']}\nos_end\n"
        f"kernel\n{values['kernel']}\nmounts_begin\n{values['mounts']}mounts_end\n"
        f"mac\n{values['mac']}\n"
    ).encode()


class FakePeer:
    def __init__(self, state, recovery=False, port=None, *, calls, responses):
        self.state, self.recovery, self.port = state, recovery, port
        self.calls, self.responses = calls, responses

    def run(self, command, data=None, timeout=180):
        self.calls.append((self.recovery, self.port, command, data, timeout, self.state))
        response = self.responses.get((self.recovery, self.port))
        if isinstance(response, BaseException):
            raise response
        return response


class ObserverTests(unittest.TestCase):
    def factory(self, calls, responses):
        def create(state, recovery=False, port=None):
            return FakePeer(state, recovery, port, calls=calls, responses=responses)
        return create

    def test_busybox_recovery_is_accepted_only_when_pinned_identity_matches(self):
        calls = []
        result = observe._observe_state(
            state_data(Path("/unused")),
            self.factory(calls, {(True, 2222): recovery_report(), (False, 22): OSError("not reachable")}),
        )
        self.assertEqual(result["state"], "recovery")
        self.assertTrue(result["verified_identity"])
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual([(c[0], c[1]) for c in calls], [(True, 2222), (False, 22)])

    def test_recovery_identity_mismatches_fail_closed_for_all_pins(self):
        mismatches = {
            "nonce": ("nonce", "f" * 32),
            "MAC": ("mac", "00:00:00:00:00:00"),
            "CID": ("cid", "b" * 32),
            "size": ("size", "1234"),
        }
        for label, (field, wrong) in mismatches.items():
            with self.subTest(field=label):
                calls = []
                report = observe._parse_recovery(recovery_report(**{field: wrong}))
                assessed = observe._assess(report, state_data(Path("/unused")), "recovery")
                self.assertFalse(assessed["verified_identity"])
                check = {"nonce": "nonce", "MAC": "mac", "CID": "cid", "size": "size"}[label]
                self.assertFalse(assessed["checks"][check])
                result = observe._observe_state(
                    state_data(Path("/unused")),
                    self.factory(calls, {(True, 2222): recovery_report(**{field: wrong}), (False, 22): OSError("offline")}),
                )
                self.assertEqual(result["state"], "identity_mismatch")
                self.assertFalse(result["verified_identity"])

    def test_recovery_rejects_mounted_sd_and_non_busybox_os(self):
        for label, changes in (
            ("sd mounted", {"mounts": "/dev/mmcblk0p2 / ext4 rw\n"}),
            ("other os", {"os": "ID=debian\n"}),
        ):
            with self.subTest(case=label):
                report = observe._parse_recovery(recovery_report(**changes))
                assessed = observe._assess(report, state_data(Path("/unused")), "recovery")
                self.assertFalse(assessed["verified_identity"])

    def test_alma_requires_exact_root_and_all_identity_pins(self):
        state = state_data(Path("/unused"))
        base = {"os": "ID=almalinux\n", "cid": CID, "size_bytes": 64 * 1024**3,
                "instance_id": NONCE, "root": "xfs /dev/mmcblk0p2",
                "mounts": "/dev/mmcblk0p2 / xfs rw\n/dev/mmcblk0p1 /boot/firmware vfat rw\n",
                "macs": {"eth0": MAC}, "kernel": "6.12.0-test"}
        accepted = observe._assess(observe._normalize_alma(base), state, "alma")
        self.assertTrue(accepted["verified_identity"])
        self.assertFalse(accepted["checks"]["sd_unmounted"])
        for label, value, check in (
            ("root", "xfs /dev/mmcblk0p20", "root"),
            ("nonce", "f" * 32, "nonce"),
            ("MAC", "00:00:00:00:00:00", "mac"),
            ("CID", "b" * 32, "cid"),
            ("size", 12, "size"),
            ("OS", "ID=debian\n", "os"),
        ):
            with self.subTest(field=label):
                changed = dict(base)
                if check == "root":
                    changed["root"] = value
                elif check == "nonce":
                    changed["instance_id"] = value
                elif check == "mac":
                    changed["macs"] = {"eth0": value}
                elif check == "cid":
                    changed["cid"] = value
                elif check == "size":
                    changed["size_bytes"] = value
                else:
                    changed["os"] = value
                assessed = observe._assess(observe._normalize_alma(changed), state, "alma")
                self.assertFalse(assessed["verified_identity"])
                self.assertFalse(assessed["checks"][check])

    def test_unreachable_and_peer_errors_publish_class_only(self):
        calls = []
        secret = "192.0.2.10 /secret/session private stderr"
        result = observe._observe_state(
            state_data(Path("/unused")),
            self.factory(calls, {(True, 2222): pi_migrate.MigrationError(secret), (False, 22): TimeoutError(secret)}),
        )
        encoded = json.dumps(result)
        self.assertEqual(result["state"], "unreachable")
        self.assertEqual(result["errors"], {"recovery": "MigrationError", "alma": "TimeoutError"})
        for item in ("192.0.2.10", "/secret/session", "private stderr", NONCE, CID):
            self.assertNotIn(item, encoded)

    def test_private_session_errors_are_sanitized_and_cli_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            session_file = session / "session.json"
            session_file.write_text("{}\n")
            session_file.chmod(0o600)
            with mock.patch.object(pi_migrate, "state_load", side_effect=pi_migrate.MigrationError("secret host 192.0.2.99 /private/key")):
                result = observe.observe(session)
            self.assertEqual(result, {"state": "invalid_session", "verified_identity": False,
                                      "phase": "unknown", "error_class": "MigrationError"})
            self.assertNotIn("192.0.2.99", json.dumps(result))
            out = io.StringIO()
            with mock.patch.object(observe, "observe", return_value=result), contextlib.redirect_stdout(out):
                self.assertEqual(observe.main(["--session", str(session)]), 1)
            self.assertNotIn("/private/key", out.getvalue())

    def test_state_is_unchanged_and_no_migration_or_discovery_api_is_called(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            state = state_fixture(session)
            state_path = session / "session.json"
            before = hashlib.sha256(state_path.read_bytes()).hexdigest()
            calls = []
            responses = {(True, 2222): recovery_report(), (False, 22): OSError("offline")}
            peer_factory = self.factory(calls, responses)
            with (
                mock.patch.object(pi_migrate, "state_load", return_value=state) as state_load,
                mock.patch.object(pi_migrate, "state_save", side_effect=AssertionError("must not save")) as save,
                mock.patch.object(pi_migrate, "discover", side_effect=AssertionError("must not discover")) as discover,
                mock.patch.object(pi_migrate, "recovery_peer", side_effect=AssertionError("must not rediscover")) as recovery_peer,
                mock.patch.object(pi_migrate, "alma_peer", side_effect=AssertionError("must not rediscover")) as alma_peer,
                mock.patch.object(pi_migrate, "Peer", side_effect=peer_factory),
                mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"alias key\n", b"")) as run,
            ):
                result = observe.observe(session)
            self.assertEqual(result["state"], "recovery")
            self.assertTrue(result["verified_identity"])
            self.assertEqual(before, hashlib.sha256(state_path.read_bytes()).hexdigest())
            save.assert_not_called()
            discover.assert_not_called()
            recovery_peer.assert_not_called()
            alma_peer.assert_not_called()
            state_load.assert_called_once_with(session.resolve())
            self.assertEqual(len(calls), 2)
            run.assert_called_once_with(
                ["ssh-keygen", "-F", "arcturus-pi-wired", "-f", str(session.resolve() / "known_hosts")],
                capture_output=True, timeout=5, check=False,
            )

    def test_missing_host_key_alias_blocks_observation(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            state = state_fixture(session)
            with (
                mock.patch.object(pi_migrate, "state_load", return_value=state),
                mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"secret diagnostic")),
                mock.patch.object(observe.pi_migrate, "Peer") as peer,
            ):
                result = observe.observe(session)
            self.assertEqual(result["state"], "invalid_session")
            self.assertEqual(result["error_class"], "ObservationError")
            peer.assert_not_called()
            self.assertNotIn("secret diagnostic", json.dumps(result))

    def test_observation_uses_copy_of_state_and_fixed_read_commands(self):
        calls = []
        original = state_data(Path("/unused"))
        expected = json.loads(json.dumps(original))
        result = observe._observe_state(
            original,
            self.factory(calls, {(True, 2222): recovery_report(), (False, 22): OSError("offline")}),
        )
        self.assertEqual(result["state"], "recovery")
        self.assertEqual(original, expected)
        self.assertEqual(calls[0][2], observe.RECOVERY_READ.format(interface="eth0"))
        self.assertEqual(calls[1][2], "sudo -n /usr/bin/python3 -")
        self.assertEqual(calls[0][4], 20)
        self.assertEqual(calls[1][4], 20)
        self.assertEqual(calls[0][5], original)
        self.assertIsNot(calls[0][5], original)

    def test_cli_help(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                observe.main(["--help"])
        self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
