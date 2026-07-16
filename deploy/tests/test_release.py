import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from release import (
    CommandRunner,
    DeploymentFailure,
    DeploymentRequest,
    QuadletRenderer,
    ReleaseDeployer,
    ServiceRelease,
    redact,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
COMMIT_A = "1" * 40
COMMIT_B = "2" * 40


def manifest(commit: str = COMMIT_A, digest: str = DIGEST_A) -> dict:
    return {
        "apiVersion": "arcturus.u128.org/v2",
        "kind": "ServiceRelease",
        "metadata": {"name": "example-portal", "revision": commit},
        "spec": {
            "components": {
                "assets": {
                    "image": f"registry.example.org/example/portal@{digest}",
                    "mode": "oneshot",
                    "restart": "no",
                    "command": ["sh", "-c", "cp -a /usr/share/nginx/html/. /export/"],
                    "volumes": [
                        {
                            "source": "/srv/portal/maintenance-fallback",
                            "target": "/export",
                            "type": "bind",
                            "selinuxRelabel": "shared",
                        }
                    ],
                },
                "web": {
                    "image": f"registry.example.org/example/portal@{digest}",
                    "containerName": "example-portal",
                    "dependsOn": ["assets"],
                    "healthCheck": {
                        "command": "wget -q -O /dev/null http://127.0.0.1/"
                    },
                },
            },
            "routing": {
                "web": {
                    "component": "web",
                    "port": 80,
                    "domains": ["example.org"],
                }
            },
            "deployment": {"timeoutSeconds": 60, "rollbackOnFailure": True},
        },
    }


class FakeRunner(CommandRunner):
    def __init__(self):
        self.commands: list[list[str]] = []
        self.fail_restart = False
        self.fail_restart_once = False

    def run(self, command, *, timeout=120, env=None, check=True):
        self.commands.append(command)
        returncode = 0
        stdout = ""
        stderr = ""
        if command[:3] == ["podman", "image", "inspect"]:
            digest = command[3].split("@", 1)[1]
            stdout = json.dumps([{"Digest": digest, "RepoDigests": [command[3]]}])
        elif command[:3] == ["systemctl", "--user", "is-active"]:
            stdout = "active\n"
        elif command[:3] == ["systemctl", "--user", "restart"]:
            if self.fail_restart_once:
                self.fail_restart_once = False
                returncode = 1
                stderr = "simulated failure token=should-not-leak"
            elif self.fail_restart:
                returncode = 1
                stderr = "simulated failure token=should-not-leak"
        result = subprocess.CompletedProcess(command, returncode, stdout, stderr)
        if check and returncode:
            from release import CommandError

            raise CommandError(command, result)
        return result


class FakePodman:
    def __init__(self):
        self.images: set[str] = set()

    def pull(self, image: str, timeout: int):
        self.images.add(image)

    def inspect(self, image: str):
        digest = image.split("@", 1)[1]
        return {"Digest": digest, "RepoDigests": [image]}


class ManifestTests(unittest.TestCase):
    def test_canonical_manifest_omits_optional_nulls(self):
        release = ServiceRelease.model_validate(manifest())
        encoded = json.loads(release.canonical_json())
        self.assertNotIn("schedule", encoded["spec"]["components"]["web"])
        self.assertNotIn("healthCheck", encoded["spec"]["components"]["assets"])

    def test_requires_digest_pinned_image(self):
        raw = manifest()
        raw["spec"]["components"]["web"]["image"] = "registry.example.org/example/portal:latest"
        with self.assertRaises(ValidationError):
            ServiceRelease.model_validate(raw)

    def test_rejects_secret_like_environment_values(self):
        raw = manifest()
        raw["spec"]["components"]["web"]["environment"] = {"API_TOKEN": "bad"}
        with self.assertRaises(ValidationError):
            ServiceRelease.model_validate(raw)

    def test_rejects_dependency_cycles(self):
        raw = manifest()
        raw["spec"]["components"]["assets"]["dependsOn"] = ["web"]
        with self.assertRaises(ValidationError):
            ServiceRelease.model_validate(raw)

    def test_request_identity_must_match_manifest(self):
        with self.assertRaises(ValidationError):
            DeploymentRequest(
                service="different",
                commit_sha=COMMIT_A,
                manifest=ServiceRelease.model_validate(manifest()),
            )

    def test_redacts_nested_secrets_and_authorization(self):
        result = redact(
            {
                "password": "value",
                "message": "Authorization: Bearer abc123",
                "safe": "visible",
            }
        )
        self.assertEqual(result["password"], "<redacted>")
        self.assertNotIn("abc123", result["message"])
        self.assertEqual(result["safe"], "visible")

    def test_scheduled_components_require_schedule(self):
        raw = manifest()
        raw["spec"]["components"]["web"]["mode"] = "scheduled"
        raw["spec"]["components"]["web"].pop("healthCheck")
        with self.assertRaises(ValidationError):
            ServiceRelease.model_validate(raw)

    def test_accepts_additive_legacy_compose_migration_policy(self):
        raw = manifest()
        raw["spec"]["migration"] = {
            "legacyCompose": [
                {"project": "legacy_portal", "cleanup": "retain", "required": False}
            ]
        }
        release = ServiceRelease.model_validate(raw)
        self.assertEqual(release.spec.migration.legacyCompose[0].project, "legacy_portal")


class RendererTests(unittest.TestCase):
    def test_accepts_existing_named_volume_with_compose_underscore(self):
        raw = manifest()
        raw["spec"]["components"]["web"]["volumes"] = [{
            "source": "legacy_project_data",
            "target": "/data",
            "type": "volume",
            "external": True,
        }]
        ServiceRelease.model_validate(raw)

    def test_renders_dependencies_health_and_digest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "quadlet"
            renderer = QuadletRenderer([Path("/srv")])
            units = renderer.render(ServiceRelease.model_validate(manifest()), destination)
            web = (destination / "arcturus-example-portal-web.container").read_text()
            self.assertIn("Image=registry.example.org/example/portal@sha256:", web)
            self.assertIn("Requires=arcturus-example-portal-assets.service", web)
            self.assertIn("Notify=healthy", web)
            self.assertIn("arcturus-example-portal-web.service", units)
            assets = (destination / "arcturus-example-portal-assets.container").read_text()
            self.assertIn("Volume=/srv/portal/maintenance-fallback:/export:z", assets)

    def test_rejects_bind_mount_outside_allowlist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            renderer = QuadletRenderer([Path(temp_dir)])
            with self.assertRaises(ValueError):
                renderer.render(ServiceRelease.model_validate(manifest()), Path(temp_dir) / "q")

    def test_renders_scheduled_component_as_timer(self):
        raw = manifest()
        scheduled = raw["spec"]["components"]["web"]
        scheduled.pop("healthCheck")
        scheduled["mode"] = "scheduled"
        scheduled["schedule"] = {
            "onCalendar": "hourly",
            "persistent": True,
            "randomizedDelaySeconds": 30,
        }
        raw["spec"]["routing"] = {}
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir)
            units = QuadletRenderer([Path("/srv")]).render(
                ServiceRelease.model_validate(raw), destination
            )
            timer = (destination / "arcturus-example-portal-web.timer").read_text()
            self.assertIn("OnCalendar=hourly", timer)
            self.assertIn("RandomizedDelaySec=30", timer)
            self.assertIn("arcturus-example-portal-web.timer", units)


class DeployerTests(unittest.TestCase):
    def make_deployer(self, base: Path, runner: FakeRunner) -> ReleaseDeployer:
        return ReleaseDeployer(
            state_dir=base / "state",
            quadlet_dir=base / "quadlets",
            systemd_dir=base / "systemd",
            allowed_bind_roots=[Path("/srv")],
            runner=runner,
            podman=FakePodman(),
            validate_generator=False,
        )

    def request(self, commit: str = COMMIT_A, digest: str = DIGEST_A) -> DeploymentRequest:
        return DeploymentRequest(
            service="example-portal",
            commit_sha=commit,
            manifest=ServiceRelease.model_validate(manifest(commit, digest)),
        )

    def route_deployer(self, base: Path, runner: FakeRunner):
        status = base / "router-status.json"
        deployer = ReleaseDeployer(
            state_dir=base / "state",
            quadlet_dir=base / "quadlets",
            systemd_dir=base / "systemd",
            allowed_bind_roots=[Path("/srv")],
            runner=runner,
            podman=FakePodman(),
            validate_generator=False,
            route_status_file=status,
        )
        return deployer, status

    @staticmethod
    def publish_route_from_active(deployer, status: Path, *, fail_revision: str | None = None):
        active = deployer.active_manifest_dir / "example-portal" / "arcturus.json"
        if not active.exists():
            status.write_text(json.dumps({"version": 1, "services": {}}))
            return
        published = json.loads(active.read_text())
        revision = published["metadata"]["revision"]
        failed = revision == fail_revision
        status.write_text(json.dumps({
            "version": 1,
            "services": {
                "example-portal": {
                    "status": "failed" if failed else "published",
                    "revision": revision,
                    "deploymentId": published["metadata"]["deploymentId"],
                    "domains": ["example.org"],
                    "upstreams": ["example-portal:80"],
                    "configDigest": DIGEST_A,
                    "appliedAt": "2026-07-13T00:00:00Z",
                    "verification": {"status": "passed", "routes": []},
                    **({"error": {"code": "nginx_apply_failed", "message": "invalid nginx"}} if failed else {}),
                }
            },
        }))

    def test_success_persists_active_release(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = FakeRunner()
            deployer = self.make_deployer(Path(temp_dir), runner)
            result = deployer.deploy(self.request())
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(deployer.active("example-portal")["deployment_id"], result["deployment_id"])
            self.assertTrue((Path(temp_dir) / "quadlets" / "example-portal").is_symlink())
            self.assertTrue(
                (Path(temp_dir) / "state" / "active-manifests" / "example-portal" / "arcturus.json").is_file()
            )
            published = json.loads(
                (Path(temp_dir) / "state" / "active-manifests" / "example-portal" / "arcturus.json").read_text()
            )
            self.assertEqual(published["metadata"]["deploymentId"], result["deployment_id"])
            self.assertIn(
                ["systemctl", "--user", "enable", "arcturus-example-portal.target"],
                runner.commands,
            )

    def test_activation_waits_for_replacement_unit_invocation(self):
        class InvocationRunner(FakeRunner):
            def __init__(self):
                super().__init__()
                self.restarted = False
                self.activation_checks = 0

            def run(self, command, *, timeout=120, env=None, check=True):
                if command[:3] == ["systemctl", "--user", "show"]:
                    self.commands.append(command)
                    if not self.restarted:
                        stdout = "InvocationID=old-invocation\n"
                    else:
                        self.activation_checks += 1
                        if self.activation_checks == 1:
                            stdout = "ActiveState=activating\nInvocationID=old-invocation\n"
                        else:
                            stdout = "ActiveState=active\nInvocationID=new-invocation\n"
                    return subprocess.CompletedProcess(command, 0, stdout, "")
                result = super().run(command, timeout=timeout, env=env, check=check)
                if command[:3] == ["systemctl", "--user", "restart"]:
                    self.restarted = True
                return result

        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            runner = InvocationRunner()
            deployer = self.make_deployer(base, runner)
            release_path = base / "release"
            quadlets = release_path / "quadlet"
            quadlets.mkdir(parents=True)
            (quadlets / "arcturus-example.target").write_text(
                "[Unit]\nDescription=example\n"
            )
            deployer._activate(
                "example",
                release_path,
                ["arcturus-example-web.service"],
                5,
            )
            self.assertGreaterEqual(runner.activation_checks, 2)

    def test_active_response_includes_matching_router_receipt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            route_status = base / "router-status.json"
            route_status.write_text(json.dumps({
                "version": 1,
                "services": {
                    "example-portal": {
                        "status": "published",
                        "revision": COMMIT_A,
                        "domains": ["example.org"],
                        "upstreams": ["example-portal:80"],
                        "configDigest": DIGEST_A,
                        "appliedAt": "2026-07-13T00:00:00Z",
                        "verification": {"status": "passed", "routes": []},
                    }
                },
            }))
            deployer = ReleaseDeployer(
                state_dir=base / "state",
                quadlet_dir=base / "quadlets",
                systemd_dir=base / "systemd",
                allowed_bind_roots=[Path("/srv")],
                runner=FakeRunner(),
                podman=FakePodman(),
                validate_generator=False,
                route_status_file=route_status,
            )
            receipt = deployer._routing_state(self.request().manifest)
            self.assertEqual(receipt["status"], "published")
            self.assertEqual(receipt["revision"], COMMIT_A)

    def test_deployment_waits_for_matching_route_publication(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            deployer, status = self.route_deployer(base, FakeRunner())
            deployer._request_registry_rescan = lambda: self.publish_route_from_active(deployer, status)
            result = deployer.deploy(self.request())
            self.assertEqual(result["routing"]["status"], "published")
            self.assertEqual(result["routing"]["deploymentId"], result["deployment_id"])

    def test_route_failure_rolls_back_and_republishes_previous_release(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            deployer, status = self.route_deployer(base, FakeRunner())
            deployer._request_registry_rescan = lambda: self.publish_route_from_active(
                deployer, status, fail_revision=COMMIT_B
            )
            first = deployer.deploy(self.request())
            with self.assertRaises(DeploymentFailure) as failure:
                deployer.deploy(self.request(COMMIT_B, DIGEST_B))
            self.assertTrue(failure.exception.rollback_succeeded)
            restored = deployer.active("example-portal")
            self.assertEqual(restored["deployment_id"], first["deployment_id"])
            self.assertEqual(restored["routing"]["status"], "published")

    def test_disable_enable_and_remove_publish_route_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            deployer, status = self.route_deployer(base, FakeRunner())
            deployer._request_registry_rescan = lambda: self.publish_route_from_active(deployer, status)
            deployer.deploy(self.request())
            disabled = deployer.disable("example-portal")
            self.assertTrue(disabled["result"]["routing"]["withdrawn"])
            enabled = deployer.enable("example-portal")
            self.assertEqual(enabled["result"]["routing"]["status"], "published")
            removed = deployer.remove("example-portal")
            self.assertTrue(removed["result"]["routing"]["withdrawn"])

    def test_route_publication_timeout_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            deployer, status = self.route_deployer(base, FakeRunner())
            status.write_text(json.dumps({"version": 1, "services": {}}))
            deployer._request_registry_rescan = lambda: None
            with (
                patch("release.time.monotonic", side_effect=[0, 0, 61]),
                patch("release.time.sleep"),
                self.assertRaises(TimeoutError),
            ):
                deployer._wait_for_routing(self.request().manifest, 60, "new-deployment")

    def test_rollback_route_publication_retries_transient_failed_receipt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            deployer, status = self.route_deployer(base, FakeRunner())
            calls = 0

            def rescan():
                nonlocal calls
                calls += 1
                published = calls > 1
                status.write_text(json.dumps({
                    "version": 1,
                    "services": {
                        "example-portal": {
                            "status": "published" if published else "failed",
                            "revision": COMMIT_A,
                            "deploymentId": "restored-deployment",
                            "domains": ["example.org"],
                            "upstreams": ["example-portal:80"],
                            "configDigest": DIGEST_A,
                            "verification": {
                                "status": "passed" if published else "failed",
                                "routes": [],
                            },
                            **(
                                {}
                                if published
                                else {
                                    "error": {
                                        "code": "upstream_unreachable",
                                        "message": "route runtime verification failed",
                                    }
                                }
                            ),
                        }
                    },
                }))

            deployer._request_registry_rescan = rescan
            receipt = deployer._wait_for_routing(
                self.request().manifest,
                60,
                "restored-deployment",
                retry_failed_receipts=True,
            )
            self.assertEqual(receipt["status"], "published")
            self.assertGreaterEqual(calls, 2)

    def test_failed_second_release_rolls_back_first(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = FakeRunner()
            deployer = self.make_deployer(Path(temp_dir), runner)
            first = deployer.deploy(self.request())
            runner.fail_restart = True
            with self.assertRaises(DeploymentFailure) as failure:
                deployer.deploy(self.request(COMMIT_B, DIGEST_B))
            self.assertFalse(failure.exception.rollback_succeeded)
            self.assertEqual(deployer.active("example-portal")["deployment_id"], first["deployment_id"])

    def test_failed_second_release_restores_known_good_release(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = FakeRunner()
            deployer = self.make_deployer(Path(temp_dir), runner)
            first = deployer.deploy(self.request())
            runner.fail_restart_once = True
            with self.assertRaises(DeploymentFailure) as failure:
                deployer.deploy(self.request(COMMIT_B, DIGEST_B))
            self.assertTrue(failure.exception.rollback_succeeded)
            self.assertEqual(failure.exception.rollback["deployment_id"], first["deployment_id"])
            self.assertEqual(deployer.active("example-portal")["deployment_id"], first["deployment_id"])

    def test_lock_rejects_concurrent_deployment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            deployer = self.make_deployer(Path(temp_dir), FakeRunner())
            with deployer.lock("example-portal"):
                with self.assertRaises(FileExistsError):
                    with deployer.lock("example-portal"):
                        pass

    def test_explicit_rollback_disable_enable_and_remove_are_recorded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = FakeRunner()
            deployer = self.make_deployer(Path(temp_dir), runner)
            first = deployer.deploy(self.request())
            second = deployer.deploy(self.request(COMMIT_B, DIGEST_B))
            rollback = deployer.rollback("example-portal", first["deployment_id"])
            self.assertEqual(rollback["status"], "succeeded")
            self.assertEqual(
                deployer.active("example-portal")["deployment_id"], first["deployment_id"]
            )
            self.assertNotEqual(first["deployment_id"], second["deployment_id"])
            disabled = deployer.disable("example-portal")
            self.assertEqual(disabled["status"], "succeeded")
            self.assertEqual(deployer.active("example-portal")["desired_state"], "disabled")
            enabled = deployer.enable("example-portal")
            self.assertEqual(enabled["status"], "succeeded")
            removed = deployer.remove("example-portal")
            self.assertEqual(removed["status"], "succeeded")
            self.assertIsNone(deployer.active("example-portal"))
            self.assertTrue((Path(temp_dir) / "state" / "releases" / "example-portal").is_dir())


if __name__ == "__main__":
    unittest.main()
