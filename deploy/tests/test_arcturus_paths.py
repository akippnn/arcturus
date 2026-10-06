import json
import tempfile
import unittest
from pathlib import Path

from arcturus_paths import PathResolutionError, render_systemd_unit, resolve_paths


class ArcturusPathTests(unittest.TestCase):
    def test_canonical_fhs_fixture(self):
        root = Path(__file__).resolve().parents[2]
        fixture = json.loads(
            (root / "rust/fixtures/paths/fhs-layout.json").read_text(encoding="utf-8")
        )
        actual = resolve_paths(fixture["environment"]).values()
        for key, expected in fixture["expected"].items():
            self.assertEqual(actual[key], expected)

    def test_rootless_defaults_follow_xdg(self):
        paths = resolve_paths(
            {
                "HOME": "/home/service",
                "XDG_CONFIG_HOME": "/srv/config",
                "XDG_DATA_HOME": "/srv/data",
                "XDG_CACHE_HOME": "/srv/cache",
                "XDG_RUNTIME_DIR": "/run/user/1234",
            },
            uid=1234,
        )
        self.assertEqual(paths.config_dir, Path("/srv/config/arcturus"))
        self.assertEqual(paths.deployer_state_dir, Path("/srv/data/arcturus-deployer"))
        self.assertEqual(paths.cache_dir, Path("/srv/cache/arcturus"))
        self.assertEqual(paths.runtime_dir, Path("/run/user/1234/arcturus"))
        self.assertEqual(paths.systemd_dir, Path("/srv/config/systemd/user"))
        self.assertEqual(paths.quadlet_dir, Path("/srv/config/containers/systemd/arcturus"))

    def test_fhs_roots_and_final_compatibility_overrides_are_independent(self):
        paths = resolve_paths(
            {
                "HOME": "/var/lib/arcturus",
                "ARCTURUS_CONFIG_ROOT": "/etc",
                "ARCTURUS_DATA_ROOT": "/var/lib",
                "ARCTURUS_CACHE_ROOT": "/var/cache",
                "ARCTURUS_RUNTIME_ROOT": "/run",
                "ARCTURUS_STATE_DIR": "/srv/legacy-deployer",
                "ARCTURUS_BIN_DIR": "/usr/bin",
            },
            uid=987,
        )
        self.assertEqual(paths.config_dir, Path("/etc/arcturus"))
        self.assertEqual(paths.deployer_state_dir, Path("/srv/legacy-deployer"))
        self.assertEqual(paths.fleet_state_dir, Path("/var/lib/arcturus-fleet"))
        self.assertEqual(paths.cache_dir, Path("/var/cache/arcturus"))
        self.assertEqual(paths.runtime_dir, Path("/run/arcturus"))
        self.assertEqual(paths.bin_dir, Path("/usr/bin"))

    def test_relative_roots_fail_closed(self):
        with self.assertRaises(PathResolutionError):
            resolve_paths({"HOME": "/home/service", "XDG_DATA_HOME": "relative"})
        with self.assertRaises(PathResolutionError):
            resolve_paths({"HOME": "/home/service", "XDG_DATA_HOME": "~/data"})

    def test_empty_xdg_values_are_unset(self):
        paths = resolve_paths(
            {
                "HOME": "/home/service",
                "XDG_CONFIG_HOME": "",
                "ARCTURUS_STATE_DIR": "",
            },
            uid=1001,
        )
        self.assertEqual(paths.config_dir, Path("/home/service/.config/arcturus"))
        self.assertEqual(
            paths.deployer_state_dir,
            Path("/home/service/.local/share/arcturus-deployer"),
        )

    def test_control_characters_fail_closed(self):
        with self.assertRaisesRegex(PathResolutionError, "forbidden control character"):
            resolve_paths(
                {"HOME": "/home/service", "ARCTURUS_CONFIG_ROOT": "/etc/bad\npath"}
            )

    def test_systemd_renderer_escapes_specifiers_and_rejects_missing_values(self):
        rendered = render_systemd_unit('ExecStart="@EXECUTABLE@"\n', {"EXECUTABLE": "/srv/100%/app"})
        self.assertEqual(rendered, 'ExecStart="/srv/100%%/app"\n')
        with self.assertRaises(PathResolutionError):
            render_systemd_unit("ExecStart=@MISSING@\n", {})

    def test_every_systemd_template_uses_only_canonical_layout_values(self):
        root = Path(__file__).resolve().parents[2]
        fixture = json.loads(
            (root / "rust/fixtures/paths/fhs-layout.json").read_text(encoding="utf-8")
        )
        paths = resolve_paths(fixture["environment"])
        values = {
            "CONFIG_DIR": str(paths.config_dir),
            "STATE_DIR": str(paths.deployer_state_dir),
            "QUADLET_DIR": str(paths.quadlet_dir),
            "UNIT_DIR": str(paths.systemd_dir),
            "RUNTIME_DIR": str(paths.runtime_dir),
            "WORKLOAD_ROOT": str(paths.workload_root),
            "OCI_AUTH_STATE_DIR": str(paths.oci_auth_state_dir),
            "FLEET_STATE_DIR": str(paths.fleet_state_dir),
            "AGENT_STATE_DIR": str(paths.agent_state_dir),
        }
        for template in sorted((root / "deploy").glob("*.service")):
            rendered = render_systemd_unit(template.read_text(encoding="utf-8"), values)
            self.assertNotRegex(rendered, r"@[A-Z][A-Z0-9_]*@", template.name)
            self.assertNotIn("%h/", rendered, template.name)
            self.assertNotIn("%t/", rendered, template.name)


if __name__ == "__main__":
    unittest.main()
