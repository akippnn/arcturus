import json
import io
import tempfile
import unittest
from unittest.mock import patch
from argparse import Namespace
from pathlib import Path

from arcturusctl import (
    api_request,
    arcturus_config_dir,
    build_parser,
    command_fleet_service_apply,
    command_project_preflight,
    command_project_render,
    load_project,
)
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
            "provider": "github",
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

    def test_fleet_cli_defaults_follow_the_central_config_root(self):
        with patch.dict(
            "os.environ",
            {"ARCTURUS_CONFIG_ROOT": "/etc", "ARCTURUS_FLEET_TOKEN_FILE": ""},
            clear=False,
        ):
            self.assertEqual(arcturus_config_dir(), Path("/etc/arcturus"))
            args = build_parser().parse_args(["fleet", "worker", "list"])
            self.assertEqual(args.token_file, "/etc/arcturus/fleet-operator.token")

    def test_api_request_accepts_list_responses_used_by_fleet_list_commands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token = Path(temp_dir) / "operator.token"
            token.write_text("secret\n")
            args = Namespace(
                api_url="http://127.0.0.1:9190",
                token_file=str(token),
                timeout=1,
            )
            with patch("arcturusctl.urllib.request.urlopen", return_value=io.BytesIO(b"[]")):
                self.assertEqual(api_request(args, "GET", "/v1/fleet/workers"), [])

    def test_shared_build_and_fixed_components_validate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            definition, _, release = load_project(self.fixture(Path(temp_dir)))
            self.assertEqual(definition.builds["web"].components, ["web", "db-init"])
            self.assertEqual(release.spec.components["postgres"].image.split("@", 1)[1], FIXED_DIGEST)

    def test_github_is_the_default_ci_provider(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = self.fixture(root)
            value = project()
            value["ci"].pop("provider")
            path.write_text(json.dumps(value))
            definition, _, _ = load_project(path)
            self.assertEqual(definition.ci.provider, "github")

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


    def test_owned_registry_maps_shared_build_to_isolated_component_repositories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = self.fixture(root)
            value = project()
            value["registry"] = {
                "mode": "owned",
                "host": "registry.tailnet.ts.net",
                "origin": "https://registry.tailnet.ts.net",
            }
            value["builds"]["web"]["repository"] = "registry.tailnet.ts.net/multi-app/web"
            value["builds"]["web"]["componentRepositories"] = {
                "web": "registry.tailnet.ts.net/multi-app/web",
                "db-init": "registry.tailnet.ts.net/multi-app/db-init",
            }
            release = manifest()
            release["spec"]["components"]["web"]["image"] = (
                "registry.tailnet.ts.net/multi-app/web@sha256:" + "0" * 64
            )
            release["spec"]["components"]["db-init"]["image"] = (
                "registry.tailnet.ts.net/multi-app/db-init@sha256:" + "0" * 64
            )
            (root / "arcturus.release.json").write_text(json.dumps(release))
            path.write_text(json.dumps(value))
            definition, _, _ = load_project(path)
            self.assertEqual(definition.registry.mode, "owned")
            digest_path = root / "web.digest"
            digest_path.write_text(DIGEST)
            command_project_render(Namespace(
                project=str(path), revision=REVISION, digest=[f"web={digest_path}"],
                output=str(root / "release.json"), request_output=str(root / "request.json"),
            ))
            rendered = json.loads((root / "release.json").read_text())
            self.assertEqual(
                rendered["spec"]["components"]["db-init"]["image"],
                f"registry.tailnet.ts.net/multi-app/db-init@{DIGEST}",
            )

    def test_v1_compatibility_requires_safe_host_feature(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.fixture(Path(temp_dir))
            value = project()
            value["compatibility"] = {
                "manifestApis": ["arcturus.u128.org/v1", "arcturus.u128.org/v2"],
                "v1Mode": "routing-mirror",
                "v1Manifest": ".arcturus/compat-v1.json",
            }
            path.write_text(json.dumps(value))
            with patch("arcturusctl.api_request", return_value={
                "status": "ok", "version": "4.0.0-alpha.1",
                "features": ["authenticated-preflight", "legacy-compose-handoff"],
            }):
                with self.assertRaisesRegex(SystemExit, "manifest-v1-safe-routing-mirror"):
                    command_project_preflight(Namespace(
                        project=str(path), api_url=None, token_file=None, timeout=10,
                        release=None, readiness_only=True,
                    ))

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
                    "version": "4.0.0-alpha.1",
                    "features": ["authenticated-preflight", "legacy-compose-handoff"],
                },
                {"status": "ready"},
            ]
            with patch("arcturusctl.api_request", side_effect=responses) as request:
                command_project_preflight(Namespace(
                    project=str(path), api_url=None, token_file=None, timeout=10
                ))
            self.assertEqual(request.call_count, 2)

    def test_fleet_apply_derives_release_characteristics_before_submission(self):
        repository = Path(__file__).resolve().parents[2]
        fixture = repository / "rust/fixtures/fleet/workload-intent.json"
        with patch("arcturusctl.api_request", return_value={"status": "placed"}) as request:
            command_fleet_service_apply(Namespace(
                intent=str(fixture), api_url="http://127.0.0.1:9190",
                token_file="fleet.token", timeout=10,
            ))
        _, path, payload = request.call_args.args[1:]
        self.assertEqual(path, "/v1/fleet/workloads/dist-redis-client")
        self.assertEqual(payload["releaseDigest"], payload["releaseCharacteristics"]["releaseDigest"])
        self.assertEqual(payload["releaseCharacteristics"]["secretUses"][0]["secretName"], "dist-redis-url")


if __name__ == "__main__":
    unittest.main()
