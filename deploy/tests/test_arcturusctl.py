import json
import tempfile
import unittest
from unittest.mock import patch
from argparse import Namespace
from pathlib import Path

from arcturusctl import command_project_preflight, command_project_render, load_project
from pydantic import ValidationError


DIGEST = "sha256:" + "a" * 64
FIXED_DIGEST = "sha256:" + "b" * 64
REVISION = "1" * 40


def manifest() -> dict:
    return {
        "apiVersion": "arcturus.u128.org/v2",
        "kind": "ServiceRelease",
        "metadata": {"name": "multi-app", "revision": "0" * 40},
        "spec": {
            "components": {
                "web": {
                    "image": f"registry.example.org/team/web@{'sha256:' + '0' * 64}",
                    "containerName": "multi-app-web",
                    "networks": ["internal_routing"],
                },
                "db-init": {
                    "image": f"registry.example.org/team/web@{'sha256:' + '0' * 64}",
                    "mode": "oneshot",
                    "networks": ["internal_routing"],
                },
                "postgres": {
                    "image": f"docker.io/library/postgres@{FIXED_DIGEST}",
                    "networks": ["internal_routing"],
                },
            },
            "routing": {
                "web": {
                    "component": "web",
                    "port": 3000,
                    "domains": ["multi.example.org"],
                }
            },
        },
    }


def project() -> dict:
    return {
        "apiVersion": "arcturus.u128.org/project/v1",
        "service": "multi-app",
        "manifest": "arcturus.release.json",
        "ci": {
            "provider": "gitea",
            "apiUrl": "http://192.0.2.10:9090",
            "storage": "isolated",
        },
        "registry": {"host": "registry.example.org"},
        "builds": {
            "web": {
                "repository": "registry.example.org/team/web",
                "context": ".",
                "containerfile": "Containerfile",
                "validationTargets": ["test"],
                "releaseTarget": "runtime",
                "components": ["web", "db-init"],
            }
        },
        "fixedComponents": ["postgres"],
        "verification": {
            "publicUrl": "https://multi.example.org",
            "publicMode": "cloudflare-challenge",
            "requireRouting": True,
        },
    }


class ProjectConfigurationTests(unittest.TestCase):
    def fixture(self, root: Path) -> Path:
        (root / ".arcturus").mkdir()
        (root / "arcturus.release.json").write_text(json.dumps(manifest()))
        path = root / ".arcturus" / "project.json"
        path.write_text(json.dumps(project()))
        return path

    def test_shared_build_and_fixed_components_validate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            definition, _, release = load_project(self.fixture(Path(temp_dir)))
            self.assertEqual(definition.builds["web"].components, ["web", "db-init"])
            self.assertEqual(release.spec.components["postgres"].image.split("@", 1)[1], FIXED_DIGEST)

    def test_render_reuses_one_digest_for_shared_components(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_path = self.fixture(root)
            digest_path = root / "web.digest"
            digest_path.write_text(DIGEST)
            command_project_render(Namespace(
                project=str(project_path),
                revision=REVISION,
                digest=[f"web={digest_path}"],
                output=str(root / "release.json"),
                request_output=str(root / "request.json"),
            ))
            rendered = json.loads((root / "release.json").read_text())
            web_image = rendered["spec"]["components"]["web"]["image"]
            self.assertEqual(web_image, rendered["spec"]["components"]["db-init"]["image"])
            self.assertEqual(rendered["spec"]["components"]["postgres"]["image"], f"docker.io/library/postgres@{FIXED_DIGEST}")

    def test_rejects_obsolete_secret_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.fixture(Path(temp_dir))
            invalid = project()
            invalid["ci"]["deployTokenSecret"] = "DEPLOY_WEBHOOK_SECRET"
            path.write_text(json.dumps(invalid))
            with self.assertRaises(ValidationError):
                load_project(path)

    def test_rejects_unmapped_component(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.fixture(Path(temp_dir))
            invalid = project()
            invalid["fixedComponents"] = []
            path.write_text(json.dumps(invalid))
            with self.assertRaisesRegex(SystemExit, "no image source"):
                load_project(path)

    def test_preflight_rejects_host_without_required_features(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.fixture(Path(temp_dir))
            with patch("arcturusctl.api_request", return_value={"status": "ok", "version": "0.99.0-rc.1"}):
                with self.assertRaisesRegex(SystemExit, "missing features: authenticated-preflight, legacy-compose-handoff"):
                    command_project_preflight(Namespace(
                        project=str(path), api_url=None, token_file=None, timeout=10
                    ))

    def test_preflight_accepts_rc2_capabilities(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.fixture(Path(temp_dir))
            responses = [
                {
                    "status": "ok",
                    "version": "0.99.0-rc.2",
                    "features": ["authenticated-preflight", "legacy-compose-handoff"],
                },
                {"status": "ready"},
            ]
            with patch("arcturusctl.api_request", side_effect=responses) as request:
                command_project_preflight(Namespace(
                    project=str(path), api_url=None, token_file=None, timeout=10
                ))
            self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
