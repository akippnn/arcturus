"""Side-effect-free plan and mocked-apply tests for restore-host.py."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

DEPLOY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEPLOY_DIR))
MODULE_PATH = DEPLOY_DIR / "pi-migration" / "restore-host.py"
SPEC = importlib.util.spec_from_file_location("arcturus_restore_host", MODULE_PATH)
restore_host = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(restore_host)

HOMEKIT_DEPS = [
    "base36==0.1.1", "cffi==2.1.1", "chacha20poly1305-reuseable==0.13.2",
    "cryptography==50.0.1", "h11==0.16.0", "HAP-python==5.0.0",
    "ifaddr==0.2.0", "orjson==3.12.0", "pycparser==3.0",
    "PyQRCode==1.2.1", "zeroconf==0.151.3",
]


def unit_record(content, *, active="active", enabled="enabled"):
    return {"content": content, "active": active, "enabled": enabled}


def base_inventory(custom_units=None, cron=None):
    return {
        "schemaVersion": 1,
        "users": [{"name": "aki", "uid": 1000, "gid": 1000, "home": "/home/aki"}],
        "custom_units": custom_units or {},
        "cron": cron or {},
        "user_units": {"aki": ""},
        "system_units": "",
        "active_units": "",
    }


def homekit_unit():
    return """[Unit]
Description=HomeKit wake-on-LAN helper
[Service]
User=aki
WorkingDirectory=/opt/homekit-wol
ExecStart=/opt/homekit-wol/venv/bin/python3 /opt/homekit-wol/wol.py
[Install]
WantedBy=multi-user.target
"""


def gaming_sqm_unit():
    return """[Unit]
After=network.target NetworkManager.service
[Service]
Type=oneshot
ExecStart=/sbin/tc qdisc replace dev wlan0 root cake bandwidth 190mbit diffserv4
ExecStart=/sbin/tc qdisc replace dev eth0 root cake bandwidth 190mbit diffserv4
ExecStop=/sbin/tc qdisc del dev wlan0 root 2>/dev/null || true
ExecStop=/sbin/tc qdisc del dev eth0 root 2>/dev/null || true
"""


class RestoreHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "opt/homekit-wol").mkdir(parents=True)
        (self.root / "usr/local/bin").mkdir(parents=True)
        cloudflared = self.root / "usr/local/bin/cloudflared"
        cloudflared.write_bytes(b"provided binary fixture")
        cloudflared.chmod(0o755)
        (self.root / "usr/bin").mkdir(parents=True)
        executable = self.root / "usr/bin/true"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        cloud_hash = hashlib.sha256(cloudflared.read_bytes()).hexdigest()
        self.dependencies = {
            "homekit_requirements": HOMEKIT_DEPS,
            "cloudflared": {"file": {"path": "/usr/bin/cloudflared"}, "sha256": cloud_hash},
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_adapts_known_services_and_excludes_runtime_and_package_units(self):
        custom = {
            "homekit-wol.service": unit_record(homekit_unit()),
            "cloudflared.service": unit_record("[Unit]\n[Service]\nExecStart=/usr/bin/cloudflared tunnel run\n"),
            "thermal-governor.service": unit_record(
                "[Unit]\nAfter=network.target multi-user.target\n[Service]\nExecStart=/usr/bin/true\n"
            ),
            "gaming-sqm.service": unit_record(gaming_sqm_unit()),
            "cloudflared-update.service": unit_record(
                "[Service]\nExecStart=/bin/bash -c '/usr/bin/cloudflared update; code=$?; if [ $code -eq 11 ]; then systemctl restart cloudflared; exit 0; fi; exit $code'\n"
            ),
            "cloudflared-update.timer": unit_record(
                "[Unit]\nDescription=Daily cloudflared update\n[Timer]\nOnCalendar=daily\nPersistent=true\n[Install]\nWantedBy=timers.target\n"
            ),
            "tailscaled.service": unit_record("[Service]\nExecStart=/usr/sbin/tailscaled\n"),
            "docker-app.service": unit_record("[Service]\nExecStart=/usr/bin/docker run app\n"),
        }
        plan = restore_host.build_plan(base_inventory(custom), self.dependencies, self.root)
        self.assertTrue(plan["ready"], plan["blockers"])
        by_name = {item["name"]: item for item in plan["units"]}
        self.assertEqual(set(by_name), {
            "homekit-wol.service", "cloudflared.service", "cloudflared-update.service",
            "cloudflared-update.timer", "thermal-governor.service", "gaming-sqm.service"
        })
        self.assertIn("venv-almalinux/bin/python3", by_name["homekit-wol.service"]["content"])
        self.assertEqual(by_name["homekit-wol.service"]["requirements_packages"], HOMEKIT_DEPS)
        self.assertIn("ExecStart=/usr/local/bin/cloudflared tunnel run", by_name["cloudflared.service"]["content"])
        self.assertIn("After=network.target", by_name["thermal-governor.service"]["content"])
        self.assertNotIn("After=network.target multi-user.target", by_name["thermal-governor.service"]["content"])
        sqm = by_name["gaming-sqm.service"]["content"]
        self.assertNotIn("ConditionPathExists", sqm)
        self.assertEqual(sqm.count("ExecStart="), 2)
        self.assertEqual(sqm.count("ExecStop="), 2)
        self.assertIn("if test -e /sys/class/net/wlan0; then exec /usr/sbin/tc qdisc replace dev wlan0", sqm)
        self.assertIn("if test -e /sys/class/net/eth0; then exec /usr/sbin/tc qdisc replace dev eth0", sqm)
        self.assertIn("/usr/sbin/tc qdisc del dev wlan0 root 2>/dev/null || true", sqm)
        self.assertIn("/usr/sbin/tc qdisc del dev eth0 root 2>/dev/null || true", sqm)
        self.assertIn("After=network.target NetworkManager.service", sqm)
        self.assertIn("/usr/local/bin/cloudflared update", by_name["cloudflared-update.service"]["content"])
        self.assertNotIn("/usr/bin/cloudflared update", by_name["cloudflared-update.service"]["content"])
        self.assertEqual(by_name["cloudflared-update.timer"]["facts"]["schedule"], "daily")
        self.assertEqual({item["unit"] for item in plan["excluded_units"]}, {
            "tailscaled.service", "docker-app.service"
        })
        summary = restore_host._summary(plan)
        self.assertNotIn("requirements_packages", json.dumps(summary))
        self.assertNotIn("cloudflared tunnel run", json.dumps(summary))

    def test_homekit_requires_coordinator_dependency_inventory_of_exact_pins(self):
        inv = base_inventory({"homekit-wol.service": unit_record(homekit_unit())})
        missing = restore_host.build_plan(inv, None, self.root)
        self.assertFalse(missing["ready"])
        self.assertTrue(any("dependencies.json" in item for item in missing["blockers"]))
        unpinned = restore_host.build_plan(inv, {"homekit_requirements": ["cryptography>=50"]}, self.root)
        self.assertFalse(unpinned["ready"])
        self.assertTrue(any("exact package pin" in item for item in unpinned["blockers"]))

    def test_cloudflared_digest_must_match_separately_provided_binary(self):
        inv = base_inventory({
            "cloudflared.service": unit_record("[Service]\nExecStart=/usr/bin/cloudflared tunnel run\n")
        })
        dependencies = dict(self.dependencies)
        dependencies["cloudflared"] = {"file": "/usr/bin/cloudflared", "sha256": "0" * 64}
        plan = restore_host.build_plan(inv, dependencies, self.root)
        self.assertFalse(plan["ready"])
        self.assertTrue(any("SHA-256" in item for item in plan["blockers"]))

    def test_cloudflared_token_file_is_pinned_and_only_exact_reviewed_path_is_supported(self):
        unit = "[Unit]\n[Service]\nExecStart=/usr/bin/cloudflared tunnel run --token-file /etc/cloudflared/token\n"
        plan = restore_host.build_plan(base_inventory({
            "cloudflared.service": unit_record(unit)
        }), self.dependencies, self.root, verify_target=False)
        self.assertTrue(plan["ready"], plan["blockers"])
        cloudflared = next(item for item in plan["units"] if item["name"] == "cloudflared.service")
        self.assertEqual(cloudflared["facts"]["token_file"], "/etc/cloudflared/token")
        self.assertIn("--token-file /etc/cloudflared/token", cloudflared["content"])

        for unsafe in (
            "[Service]\nExecStart=/usr/bin/cloudflared tunnel run --token-file /etc/cloudflared/other\n",
            "[Service]\nExecStart=/usr/bin/cloudflared tunnel run --token-file=/home/aki/token\n",
            "[Service]\nExecStart=/usr/bin/cloudflared tunnel run --token-file\n",
        ):
            with self.subTest(unit=unsafe):
                rejected = restore_host.build_plan(base_inventory({
                    "cloudflared.service": unit_record(unsafe)
                }), self.dependencies, self.root, verify_target=False)
                self.assertFalse(rejected["ready"])
                self.assertTrue(any("token" in item for item in rejected["blockers"]))

    def test_cloudflared_token_preflight_fails_before_any_host_action(self):
        unit_content = "[Unit]\n[Service]\nExecStart=/usr/bin/cloudflared tunnel run --token-file /etc/cloudflared/token\n"
        plan = restore_host.build_plan(base_inventory({
            "cloudflared.service": unit_record(unit_content)
        }), self.dependencies, self.root, verify_target=False)
        (self.root / "etc/systemd/system").mkdir(parents=True)
        (self.root / "etc/os-release").parent.mkdir(parents=True, exist_ok=True)
        (self.root / "etc/os-release").write_text('ID="almalinux"\n', encoding="utf-8")
        calls = []
        with mock.patch.object(restore_host.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(restore_host.HostRestoreError, "token file is missing"):
                restore_host._apply_plan(plan, self.root, run=lambda argv, **kwargs: calls.append(argv))
        self.assertEqual(calls, [])

    def test_cloudflared_token_requires_protected_root_owned_regular_file(self):
        token = self.root / "etc/cloudflared/token"
        token.parent.mkdir(parents=True)
        token.write_text("credential", encoding="utf-8")
        token.chmod(0o644)
        original_lstat = Path.lstat
        def root_lstat(path):
            info = original_lstat(path)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_gid=0, st_size=info.st_size)
        with mock.patch.object(Path, "lstat", root_lstat):
            with self.assertRaisesRegex(restore_host.HostRestoreError, "root-owned, and protected"):
                restore_host._validate_cloudflared_token(token, self.root)

    def test_old_frozen_cloudflared_plan_derives_exact_token_path_without_refreezing(self):
        unit_content = "[Unit]\n[Service]\nExecStart=/usr/bin/cloudflared tunnel run --token-file /etc/cloudflared/token\n"
        plan = restore_host.build_plan(base_inventory({
            "cloudflared.service": unit_record(unit_content)
        }), self.dependencies, self.root, verify_target=False)
        cloudflared_unit = next(item for item in plan["units"] if item["name"] == "cloudflared.service")
        cloudflared_unit["facts"].pop("token_file")  # Schema frozen before this field was added.
        token = self.root / "etc/cloudflared/token"
        token.parent.mkdir(parents=True)
        token.write_text("source credential", encoding="utf-8")
        token.chmod(0o600)
        (self.root / "etc/systemd/system").mkdir(parents=True)
        original_lstat = Path.lstat
        def root_lstat(path):
            info = original_lstat(path)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_gid=0, st_size=info.st_size)
        with mock.patch.object(Path, "lstat", root_lstat):
            restore_host._preflight(plan, self.root,
                                    run=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""))

    def test_portable_plan_defers_target_binary_and_application_checks_until_apply(self):
        custom = {
            "homekit-wol.service": unit_record(homekit_unit()),
            "cloudflared.service": unit_record("[Service]\nExecStart=/usr/bin/cloudflared tunnel run\n"),
        }
        portable_target = self.root / "portable-target"
        portable_target.mkdir()
        (portable_target / "opt/homekit-wol").mkdir(parents=True)
        binary = portable_target / "usr/local/bin/cloudflared"
        binary.parent.mkdir(parents=True)
        binary.write_bytes((self.root / "usr/local/bin/cloudflared").read_bytes())
        binary.chmod(0o755)
        plan = restore_host.build_plan(base_inventory(custom), self.dependencies, self.root, verify_target=False)
        self.assertTrue(plan["ready"], plan["blockers"])
        self.assertIn("provided /usr/local/bin/cloudflared binary and SHA-256", plan["target_dependencies_pending"])
        no_target = restore_host.build_plan(base_inventory(custom), self.dependencies, portable_target, verify_target=False)
        self.assertTrue(no_target["ready"], no_target["blockers"])
        self.assertTrue(no_target["target_dependencies_pending"])
        binary.unlink()
        failed_live = restore_host.build_plan(base_inventory(custom), self.dependencies, portable_target, verify_target=True)
        self.assertFalse(failed_live["ready"])
        self.assertTrue(any("cloudflared binary" in item for item in failed_live["blockers"]))

    def test_unknown_units_and_unsafe_service_adaptations_block_apply(self):
        unknown = restore_host.build_plan(base_inventory({
            "random-host.service": unit_record("[Service]\nExecStart=/usr/bin/true\n")
        }), {}, self.root)
        self.assertFalse(unknown["ready"])
        self.assertIn("unsupported custom unit: random-host.service", unknown["blockers"])
        bad_sqm = restore_host.build_plan(base_inventory({
            "gaming-sqm.service": unit_record("[Unit]\n[Service]\nExecStop=/bin/sh -c 'rm -rf /'\n")
        }), {}, self.root)
        self.assertFalse(bad_sqm["ready"])
        self.assertTrue(any("gaming-sqm" in item for item in bad_sqm["blockers"]))

    def test_incomplete_source_service_state_fails_closed(self):
        inv = base_inventory({
            "thermal-governor.service": unit_record(
                "[Unit]\n[Service]\nExecStart=/usr/bin/true\n", active={"error": "", "status": 4},
                enabled={"error": "", "status": 1},
            )
        })
        plan = restore_host.build_plan(inv, self.dependencies, self.root)
        self.assertFalse(plan["ready"])
        self.assertTrue(any("activity state was not captured" in item for item in plan["blockers"]))

        inv["active_units"] = "thermal-governor.service loaded inactive dead Thermal governor\n"
        inv["system_units"] = "thermal-governor.service disabled disabled\n"
        fallback = restore_host.build_plan(inv, {}, self.root)
        self.assertTrue(fallback["ready"], fallback["blockers"])
        self.assertFalse(fallback["units"][0]["active"])
        self.assertEqual(fallback["units"][0]["enablement"], "disabled")

    def test_cron_is_preserved_only_when_commands_and_script_dependencies_are_checkable(self):
        raw = {"aki": "PATH=/usr/bin\n*/5 * * * * /usr/bin/true\n"}
        plan = restore_host.build_plan(base_inventory(cron=raw), {}, self.root)
        self.assertTrue(plan["ready"], plan["blockers"])
        self.assertEqual(plan["cron"][0]["content"], raw["aki"])
        shell = restore_host.build_plan(base_inventory(cron={"aki": "*/5 * * * * /usr/bin/true || true\n"}), {}, self.root)
        self.assertFalse(shell["ready"])
        missing = restore_host.build_plan(base_inventory(cron={"aki": "*/5 * * * * absent-command\n"}), {}, self.root)
        self.assertFalse(missing["ready"])
        empty = restore_host.build_plan(base_inventory(cron={
            "aki": {"error": "no crontab for aki", "status": 1}
        }), {}, self.root)
        self.assertTrue(empty["ready"], empty["blockers"])
        self.assertIsNone(empty["cron"][0]["content"])

    def test_distribution_user_units_are_not_treated_as_custom_and_custom_units_block(self):
        inv = base_inventory()
        inv["custom_user_units"] = {"aki": {}}
        empty = restore_host.build_plan(inv, self.dependencies, self.root)
        self.assertTrue(empty["ready"], empty["blockers"])
        inv["custom_user_units"] = {"aki": []}
        empty_list = restore_host.build_plan(inv, self.dependencies, self.root)
        self.assertTrue(empty_list["ready"], empty_list["blockers"])

        inv["user_units"]["aki"] = "example.service enabled enabled\n"
        plan = restore_host.build_plan(inv, self.dependencies, self.root)
        self.assertTrue(plan["ready"], plan["blockers"])
        self.assertTrue(any("may include distribution units" in item for item in plan["limitations"]))
        self.assertTrue(any("linger state" in item for item in plan["limitations"]))

        inv["custom_user_units"] = {"aki": {"example.service": {"content": "[Service]"}}}
        blocked = restore_host.build_plan(inv, {}, self.root)
        self.assertFalse(blocked["ready"])
        self.assertTrue(any("custom user systemd units are not supported" in item for item in blocked["blockers"]))

        inv["custom_user_units"] = {"aki": "not-a-unit-list"}
        malformed = restore_host.build_plan(inv, {}, self.root)
        self.assertFalse(malformed["ready"])
        self.assertTrue(any("malformed account or unit listing" in item for item in malformed["blockers"]))

        inv["custom_user_units"] = {"aki": ["example.service"]}
        populated_list = restore_host.build_plan(inv, {}, self.root)
        self.assertFalse(populated_list["ready"])
        self.assertTrue(any("custom user systemd units are not supported" in item
                            for item in populated_list["blockers"]))

    def test_gaming_sqm_rejects_altered_start_and_stop_commands(self):
        for content in (
            gaming_sqm_unit().replace("bandwidth 190mbit", "bandwidth 200mbit", 1),
            gaming_sqm_unit().replace("2>/dev/null || true", "2>/dev/null || rm -rf /", 1),
        ):
            with self.subTest(content=content):
                plan = restore_host.build_plan(
                    base_inventory({"gaming-sqm.service": unit_record(content)}), {}, self.root
                )
                self.assertFalse(plan["ready"])
                self.assertTrue(any("gaming-sqm" in item for item in plan["blockers"]))

    def test_inactive_user_manager_is_reported_without_inventing_enablement(self):
        inv = base_inventory()
        inv["user_units"]["aki"] = {"error": "Failed to connect to bus", "status": 1}
        plan = restore_host.build_plan(inv, {}, self.root)
        self.assertTrue(plan["ready"], plan["blockers"])
        self.assertTrue(any("manager for aki was unavailable" in item for item in plan["limitations"]))
        self.assertTrue(any("enablement links are preserved" in item for item in plan["limitations"]))

    def test_mocked_apply_verifies_units_before_activation_and_restores_source_state(self):
        unit_content = "[Unit]\nAfter=network.target multi-user.target\n[Service]\nExecStart=/usr/bin/true\n"
        inv = base_inventory({"thermal-governor.service": unit_record(unit_content, active="inactive", enabled="enabled")})
        plan = restore_host.build_plan(inv, {}, self.root)
        self.assertTrue(plan["ready"], plan["blockers"])
        unit_dir = self.root / "etc/systemd/system"
        unit_dir.mkdir(parents=True)
        (self.root / "etc/os-release").write_text('ID="almalinux"\n', encoding="utf-8")
        calls = []

        def run(argv, **kwargs):
            calls.append((list(argv), kwargs))
            output = "systemd 255\n" if argv[:2] == ["systemd-analyze", "--version"] else ""
            return SimpleNamespace(returncode=0, stdout=output, stderr="")

        with mock.patch.object(restore_host.os, "geteuid", return_value=0):
            result = restore_host._apply_plan(plan, self.root, run=run)
        names = [call[0] for call in calls]
        verify = next(i for i, command in enumerate(names) if command[:2] == ["systemd-analyze", "verify"])
        daemon_reload = names.index(["systemctl", "daemon-reload"])
        enable = names.index(["systemctl", "enable", "thermal-governor.service"])
        stop = names.index(["systemctl", "stop", "thermal-governor.service"])
        self.assertLess(verify, daemon_reload)
        self.assertLess(daemon_reload, enable)
        self.assertLess(enable, stop)
        self.assertIn("After=network.target", (unit_dir / "thermal-governor.service").read_text())
        self.assertEqual(result["status"], "restored")
        self.assertEqual(result["services"], [{"name": "thermal-governor.service", "enablement": "enabled", "active_state": "inactive"}])

    def test_homekit_venv_is_created_at_final_path_and_source_venv_is_preserved(self):
        inv = base_inventory({"homekit-wol.service": unit_record(homekit_unit())})
        plan = restore_host.build_plan(inv, self.dependencies, self.root)
        (self.root / "etc").mkdir(exist_ok=True)
        (self.root / "etc/os-release").write_text('ID="almalinux"\n', encoding="utf-8")
        (self.root / "etc/systemd/system").mkdir(parents=True)
        old_venv = self.root / "opt/homekit-wol/venv"
        old_venv.mkdir()
        (old_venv / "forensic-marker").write_text("preserved", encoding="utf-8")
        calls = []

        def run(argv, **kwargs):
            argv = list(argv)
            calls.append((argv, kwargs))
            if argv[:3] == ["/usr/bin/python3", "-m", "venv"]:
                generated = Path(argv[-1])
                (generated / "bin").mkdir(parents=True)
                (generated / "bin/python3").write_text("fixture", encoding="utf-8")
                (generated / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="systemd 255\n", stderr="")

        with mock.patch.object(restore_host.os, "geteuid", return_value=0):
            restore_host._apply_plan(plan, self.root, run=run)
        new_venv = self.root / "opt/homekit-wol/venv-almalinux"
        self.assertTrue((new_venv / "bin/python3").is_file())
        self.assertTrue((new_venv / "pyvenv.cfg").is_file())
        self.assertEqual(next(argv[-1] for argv, _ in calls if argv[:3] == ["/usr/bin/python3", "-m", "venv"]), str(new_venv))
        self.assertEqual((old_venv / "forensic-marker").read_text(), "preserved")
        install = next(argv for argv, _ in calls if "install" in argv and "--no-deps" in argv)
        self.assertIn("--no-deps", install)
        self.assertEqual(install[-len(HOMEKIT_DEPS):], HOMEKIT_DEPS)
        unit_text = (self.root / "etc/systemd/system/homekit-wol.service").read_text(encoding="utf-8")
        self.assertIn("venv-almalinux/bin/python3", unit_text)

    def test_existing_homekit_venv_requires_pyvenv_config_and_matching_freeze(self):
        inv = base_inventory({"homekit-wol.service": unit_record(homekit_unit())})
        plan = restore_host.build_plan(inv, self.dependencies, self.root)
        (self.root / "etc/systemd/system").mkdir(parents=True)
        app = self.root / "opt/homekit-wol"
        venv = app / "venv-almalinux"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin/python3").write_text("fixture", encoding="utf-8")
        with self.assertRaisesRegex(restore_host.HostRestoreError, "incomplete"):
            restore_host._preflight(plan, self.root, run=lambda *args, **kwargs: None)

    def test_cloudflared_updater_service_and_timer_preserve_captured_lifecycle(self):
        service = "[Service]\nExecStart=/bin/bash -c '/usr/bin/cloudflared update; code=$?; if [ $code -eq 11 ]; then systemctl restart cloudflared; exit 0; fi; exit $code'\n"
        timer = "[Unit]\n[Timer]\nOnCalendar=daily\nPersistent=true\n[Install]\nWantedBy=timers.target\n"
        inv = base_inventory({
            "cloudflared-update.service": unit_record(service, active="inactive", enabled="static"),
            "cloudflared-update.timer": unit_record(timer, active="active", enabled="enabled"),
        })
        plan = restore_host.build_plan(inv, self.dependencies, self.root)
        self.assertTrue(plan["ready"], plan["blockers"])
        (self.root / "etc").mkdir(exist_ok=True)
        (self.root / "etc/os-release").write_text('ID="almalinux"\n', encoding="utf-8")
        (self.root / "etc/systemd/system").mkdir(parents=True)
        calls = []

        def run(argv, **kwargs):
            argv = list(argv)
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(restore_host.os, "geteuid", return_value=0):
            result = restore_host._apply_plan(plan, self.root, run=run)
        names = [item["name"] for item in result["services"]]
        self.assertEqual(names, ["cloudflared-update.service", "cloudflared-update.timer"])
        self.assertIn(["systemctl", "stop", "cloudflared-update.service"], calls)
        self.assertIn(["systemctl", "enable", "cloudflared-update.timer"], calls)
        self.assertIn(["systemctl", "start", "cloudflared-update.timer"], calls)
        self.assertNotIn(["systemctl", "start", "cloudflared-update.service"], calls)
        adapted_service = (self.root / "etc/systemd/system/cloudflared-update.service").read_text()
        self.assertIn("/usr/local/bin/cloudflared update", adapted_service)

    def test_cloudflared_updater_rejects_unreviewed_shell_and_timer_variants(self):
        good_service = "[Service]\nExecStart=/bin/bash -c '/usr/bin/cloudflared update; code=$?; if [ $code -eq 11 ]; then systemctl restart cloudflared; exit 0; fi; exit $code'\n"
        bad_service = good_service.replace("exit 0", "rm -rf /; exit 0")
        bad_timer = "[Timer]\nOnCalendar=hourly\n[Install]\nWantedBy=timers.target\n"
        for name, content in (("cloudflared-update.service", bad_service),
                              ("cloudflared-update.timer", bad_timer)):
            with self.subTest(name=name):
                plan = restore_host.build_plan(base_inventory({name: unit_record(content)}), {}, self.root)
                self.assertFalse(plan["ready"])
                self.assertTrue(any("reviewed" in item or "daily" in item for item in plan["blockers"]))
    def test_preflight_rejects_symlinked_unit_dir_and_unready_plan_before_commands(self):
        inv = base_inventory({"unknown.service": unit_record("[Service]\nExecStart=/usr/bin/true\n")})
        plan = restore_host.build_plan(inv, {}, self.root)
        (self.root / "etc").mkdir()
        (self.root / "etc/os-release").write_text('ID="almalinux"\n', encoding="utf-8")
        (self.root / "etc/systemd").symlink_to(self.root / "elsewhere", target_is_directory=True)
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(restore_host.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(restore_host.HostRestoreError, "unsupported items"):
                restore_host._apply_plan(plan, self.root, run=run)
        self.assertEqual(calls, [])

    def test_apply_backs_up_existing_crontab_and_replaces_it_with_source_text(self):
        inv = base_inventory(cron={"aki": "*/5 * * * * /usr/bin/true\n"})
        plan = restore_host.build_plan(inv, {}, self.root)
        (self.root / "etc").mkdir(exist_ok=True)
        (self.root / "etc/os-release").write_text('ID="almalinux"\n', encoding="utf-8")
        (self.root / "etc/systemd/system").mkdir(parents=True)
        calls = []

        def run(argv, **kwargs):
            calls.append((list(argv), kwargs))
            if argv[:3] == ["crontab", "-u", "aki"] and argv[-1] == "-l":
                return SimpleNamespace(returncode=0, stdout="@daily /usr/bin/true\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(restore_host.os, "geteuid", return_value=0), \
             mock.patch.object(restore_host.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=1000)):
            result = restore_host._apply_plan(plan, self.root, run=run)
        backup = Path(result["cron_backup"])
        saved = json.loads((backup / "aki.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["had_crontab"])
        self.assertIn("@daily", saved["content"])
        install = next(item for item in calls if item[0] == ["crontab", "-u", "aki", "-"])
        self.assertEqual(install[1]["input"], "*/5 * * * * /usr/bin/true\n")
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        self.assertEqual(result["cron_users"], ["aki"])


if __name__ == "__main__":
    unittest.main()
