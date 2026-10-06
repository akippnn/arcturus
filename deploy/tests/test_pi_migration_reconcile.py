"""Real artifact/preflight fixtures for the narrowly scoped host reconciler."""
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock


BASE = Path(__file__).resolve().parents[1] / "pi-migration"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reconcile = load("migration_reconcile", BASE / "reconcile-host.py")
adapter = load("migration_host_for_reconcile", BASE / "restore-host.py")


class Runner:
    def __init__(self, names):
        self.states = {name: ["active", "enabled"] for name in names}
        self.calls = []
        self.actions = []
        self.cron = {"root": None, "aki": None}
        self.packages = "HAP-python==5.0.0\n"

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        assert kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
        rc, stdout, stderr = 0, "", ""
        if command[-4:] == ["-m", "pip", "freeze", "--all"]:
            stdout = self.packages
        elif command[0] == "crontab":
            user = command[2]
            content = self.cron[user]
            if content is None:
                rc, stderr = 1, "no crontab for " + user
            else:
                stdout = content
        elif command[0] == "systemctl":
            verb, name = command[1:]
            if verb == "is-active":
                stdout = self.states[name][0]
                rc = 0 if stdout == "active" else 3
            elif verb == "is-enabled":
                stdout = self.states[name][1]
                rc = 0 if stdout in {"enabled", "static"} else 1
            else:
                assert verb in {"enable", "disable", "start", "stop"}, command
                self.actions.append((verb, name))
                index = 1 if verb in {"enable", "disable"} else 0
                self.states[name][index] = {"enable": "enabled", "disable": "disabled",
                                           "start": "active", "stop": "inactive"}[verb]
        elif command[0] != "systemd-analyze":
            raise AssertionError("unexpected command: " + repr(command))
        return subprocess.CompletedProcess(command, rc, stdout, stderr)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="arcturus-reconcile-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ("etc/systemd/system", "etc/cloudflared", "usr/local/bin",
                          "opt/homekit-wol/venv-almalinux/bin", "var/lib/systemd/linger"):
            (self.root / directory).mkdir(parents=True, exist_ok=True, mode=0o755)
        self.write("etc/os-release", 'ID="almalinux"\n')
        self.write("opt/homekit-wol/venv-almalinux/bin/python3", "fixture", 0o755)
        self.write("opt/homekit-wol/venv-almalinux/pyvenv.cfg", "home = /usr/bin\n")
        self.write("usr/local/bin/cloudflared", "fixture cloud binary", 0o755)
        self.write("etc/cloudflared/token", "fixture secret", 0o600)
        units = []
        for name in ("homekit-wol.service", "thermal-governor.service", "cloudflared-update.timer",
                     "cloudflared.service", "cloudflared-update.service", "gaming-sqm.service"):
            source = "[Service]\nExecStart=/usr/bin/true\n"
            content = source
            if name == "cloudflared.service":
                source = "[Service]\nExecStart=/usr/bin/cloudflared tunnel --token-file /etc/cloudflared/token run\n"
                content = source.replace("/usr/bin/cloudflared", "/usr/local/bin/cloudflared")
            unit = dict(name=name, content=content, source_content=source, active=True, enablement="enabled")
            if name == "homekit-wol.service":
                unit["requirements_packages"] = ["HAP-python==5.0.0"]
            if name == "cloudflared.service":
                unit["cloudflared_sha256"] = hashlib.sha256(b"fixture cloud binary").hexdigest()
            units.append(unit)
            self.write("etc/systemd/system/" + name, content)
        self.plan = dict(schemaVersion=1, ready=True, units=units,
                         cron=[dict(user="root", content=None), dict(user="aki", content=None)],
                         source_users=["root", "aki"], linger=[dict(user="aki", enabled=False)],
                         excluded_units=["excluded.service"], limitations=["fixture limitation"])
        self.run = Runner([unit["name"] for unit in units])
        self.run.states["gaming-sqm.service"] = ["inactive", "disabled"]
        # Fixtures live under the invoking developer account. Only ownership is
        # simulated; all filesystem type/mode/content checks use real fixtures.
        original_lstat = Path.lstat
        def root_lstat(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            fields = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
            fields.update(st_uid=0, st_gid=0)
            return types.SimpleNamespace(**fields)
        patch = mock.patch.object(Path, "lstat", root_lstat)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(reconcile.os, "geteuid", return_value=0)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(adapter.pwd, "getpwnam", return_value=object())
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, path, text, mode=0o644):
        target = self.root / path
        target.write_text(text, encoding="utf-8")
        target.chmod(mode)

    def snapshot(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mode)
                for path in self.root.rglob("*") if path.is_file()}

    def apply(self):
        return reconcile._reconcile_plan(self.plan, self.root, adapter, run=self.run)

    def refuses(self, message=None):
        with self.assertRaises((reconcile.ReconciliationError, adapter.HostRestoreError)) as caught:
            self.apply()
        if message:
            self.assertIn(message, str(caught.exception))
        self.assertEqual(self.run.actions, [])

    def test_only_missing_gaming_states_and_no_artifact_writes(self):
        before = self.snapshot()
        result = self.apply()
        self.assertEqual(self.run.actions, [("enable", "gaming-sqm.service"), ("start", "gaming-sqm.service")])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(result["status"], "restored")
        self.assertEqual(len(result["services"]), 6)
        self.assertEqual(result["cron_users"], [])
        self.assertEqual(result["lingering_users"], [])
        self.assertEqual(result["excluded_units"], ["excluded.service"])
        self.assertEqual(result["limitations"], ["fixture limitation"])
        self.assertTrue(all(action["executed"] for action in result["reconciliation"]["actions"]))

    def test_already_complete_never_restarts_active_units(self):
        self.run.states["gaming-sqm.service"] = ["active", "enabled"]
        self.assertEqual(self.apply()["reconciliation"]["actions"], [])
        self.assertEqual(self.run.actions, [])

    def test_source_unit_bytes_are_not_accepted(self):
        cloud = self.plan["units"][3]
        self.write("etc/systemd/system/cloudflared.service", cloud["source_content"])
        self.refuses("exact transformed bytes")

    def test_symlink_unit_collision_blocks_every_action(self):
        path = self.root / "etc/systemd/system/gaming-sqm.service"
        path.unlink()
        path.symlink_to("thermal-governor.service")
        self.refuses("regular file")

    def test_missing_existing_venv_is_not_rebuilt(self):
        (self.root / "opt/homekit-wol/venv-almalinux/bin/python3").unlink()
        self.refuses("existing validated HomeKit venv")

    def test_venv_packages_must_match(self):
        self.run.packages = "HAP-python==99.0.0\n"
        self.refuses("pinned package inventory")

    def test_binary_pin_mismatch_blocks_every_action(self):
        self.write("usr/local/bin/cloudflared", "changed", 0o755)
        self.refuses("SHA-256")

    def test_missing_token_blocks_every_action(self):
        (self.root / "etc/cloudflared/token").unlink()
        self.refuses("token file is missing")

    def test_cron_mismatch_is_not_overwritten(self):
        self.run.cron["aki"] = "unexpected cron\n"
        self.refuses("current cron differs")

    def test_linger_mismatch_is_not_edited(self):
        self.write("var/lib/systemd/linger/aki", "")
        self.refuses("current linger differs")

    def test_unsupported_later_state_blocks_earlier_pending_action(self):
        self.run.states["homekit-wol.service"] = ["inactive", "disabled"]
        self.run.states["gaming-sqm.service"][1] = "masked"
        self.refuses("state is unavailable")

    def test_private_plan_permissions_and_sha_are_enforced(self):
        path = self.root / "plan.json"
        self.write("plan.json", "{}", 0o644)
        with self.assertRaises(reconcile.ReconciliationError):
            reconcile._regular_bytes(path, private=True)
        path.chmod(0o600)
        with mock.patch.object(reconcile, "_safe_chain"):
            with self.assertRaisesRegex(reconcile.ReconciliationError, "SHA-256 pin"):
                reconcile._pinned_input(path, "0" * 64, private=True)

    def test_unsafe_directory_chain_refused(self):
        (self.root / "etc/systemd").chmod(0o777)
        self.refuses("unsafe directory chain")


if __name__ == "__main__":
    unittest.main()
