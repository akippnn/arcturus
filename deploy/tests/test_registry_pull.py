import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from image_policy_app import (
    AuthenticatedReleaseDeployer,
    BaseReleaseDeployer,
    HardenedPodmanClient,
    _pull_messages,
)


class FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class FakeHTTPClient:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, path, *, params):
        self.calls.append((path, params))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TestPodmanClient(HardenedPodmanClient):
    def __init__(self, outcomes, **kwargs):
        self.fake_client = FakeHTTPClient(list(outcomes))
        super().__init__("/tmp/fake-podman.sock", **kwargs)

    def _client(self, _timeout):
        return self.fake_client


class PullResponseTests(unittest.TestCase):
    def test_accepts_successful_json_payload(self):
        client = TestPodmanClient(
            [FakeResponse(200, json.dumps({"images": ["sha256:abc"]}))],
            pull_attempts=1,
        )
        client.pull("registry.example.org/team/app@sha256:" + "a" * 64, 30)
        self.assertEqual(len(client.fake_client.calls), 1)

    def test_accepts_successful_ndjson_payload(self):
        client = TestPodmanClient(
            [FakeResponse(200, '{"stream":"pulling"}\n{"images":["sha256:abc"]}\n')],
            pull_attempts=1,
        )
        client.pull("registry.example.org/team/app@sha256:" + "a" * 64, 30)

    def test_rejects_http_200_error_field(self):
        client = TestPodmanClient(
            [FakeResponse(200, json.dumps({"error": "authentication required"}))],
            pull_attempts=3,
        )
        with self.assertRaisesRegex(RuntimeError, "authentication required"):
            client.pull("registry.example.org/team/app@sha256:" + "a" * 64, 30)
        self.assertEqual(len(client.fake_client.calls), 1)

    def test_rejects_terminal_ndjson_error_and_redacts_credentials(self):
        body = (
            '{"stream":"pulling"}\n'
            '{"errorDetail":{"message":"password=fixture token=fixture denied"}}\n'
        )
        client = TestPodmanClient([FakeResponse(200, body)], pull_attempts=1)
        with self.assertRaises(RuntimeError) as captured:
            client.pull("registry.example.org/team/app@sha256:" + "a" * 64, 30)
        message = str(captured.exception)
        self.assertNotIn("password=fixture", message)
        self.assertNotIn("token=fixture", message)
        self.assertIn("<redacted>", message)

    def test_401_and_403_fail_without_retry(self):
        for status in (401, 403):
            with self.subTest(status=status):
                client = TestPodmanClient(
                    [FakeResponse(status, "registry authentication required")],
                    pull_attempts=3,
                )
                with self.assertRaisesRegex(RuntimeError, str(status)):
                    client.pull("registry.example.org/team/app@sha256:" + "a" * 64, 30)
                self.assertEqual(len(client.fake_client.calls), 1)

    def test_5xx_retries_then_succeeds(self):
        sleeps = []
        client = TestPodmanClient(
            [FakeResponse(500, "temporary"), FakeResponse(200, "{}")],
            pull_attempts=3,
            retry_delay_seconds=0.25,
            sleep=sleeps.append,
        )
        client.pull("registry.example.org/team/app@sha256:" + "a" * 64, 30)
        self.assertEqual(len(client.fake_client.calls), 2)
        self.assertEqual(sleeps, [0.25])

    def test_transport_error_retries_then_reports_direct_failure(self):
        request = httpx.Request("POST", "http://localhost/images/pull")
        sleeps = []
        client = TestPodmanClient(
            [
                httpx.ConnectError("socket unavailable", request=request),
                httpx.ConnectError("socket unavailable", request=request),
            ],
            pull_attempts=2,
            retry_delay_seconds=0.1,
            sleep=sleeps.append,
        )
        with self.assertRaisesRegex(RuntimeError, "transport failed after 2 attempts"):
            client.pull("registry.example.org/team/app@sha256:" + "a" * 64, 30)
        self.assertEqual(sleeps, [0.1])

    def test_invalid_progress_payload_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "invalid JSON progress"):
            _pull_messages('{"stream":"ok"}\nnot-json\n')


class RegistryAuthenticationTests(unittest.TestCase):
    def write_authfile(self, root: Path, payload: dict, mode: int = 0o600) -> Path:
        path = root / "auth.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(mode)
        return path

    def test_finds_scoped_host_credentials_in_protected_authfile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write_authfile(
                Path(temp_dir),
                {"auths": {"https://registry.example.org/v2/": {"auth": "fixture"}}},
            )
            client = HardenedPodmanClient(
                "/tmp/fake.sock",
                registry_auth_file=path,
                private_registries={"registry.example.org"},
                pull_attempts=1,
            )
            self.assertTrue(client.registry_auth_available("registry.example.org"))

    def test_rejects_missing_or_overexposed_authfile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing = root / "missing.json"
            client = HardenedPodmanClient(
                "/tmp/fake.sock", registry_auth_file=missing, pull_attempts=1
            )
            self.assertFalse(client.registry_auth_available("registry.example.org"))

            path = self.write_authfile(
                root, {"auths": {"registry.example.org": {"auth": "fixture"}}}, 0o644
            )
            client = HardenedPodmanClient(
                "/tmp/fake.sock", registry_auth_file=path, pull_attempts=1
            )
            self.assertFalse(client.registry_auth_available("registry.example.org"))

    def test_preflight_reports_missing_private_registry_auth(self):
        podman = Mock()
        podman.registry_requires_auth.return_value = True
        podman.registry_auth_available.return_value = False
        deployer = object.__new__(AuthenticatedReleaseDeployer)
        deployer.podman = podman
        manifest = SimpleNamespace(
            spec=SimpleNamespace(
                components={
                    "web": SimpleNamespace(
                        image="registry.example.org/team/app@sha256:" + "a" * 64
                    )
                }
            )
        )
        with patch.object(
            BaseReleaseDeployer,
            "preflight",
            return_value={"status": "ready", "checked": {}},
        ):
            with self.assertRaisesRegex(
                RuntimeError, "host prerequisites missing: registryAuth=registry.example.org"
            ):
                deployer.preflight(manifest)

    def test_pull_failure_prevents_misleading_inspect_404(self):
        podman = Mock()
        podman.pull.side_effect = RuntimeError("Podman image pull failed: denied")
        deployer = object.__new__(AuthenticatedReleaseDeployer)
        deployer.podman = podman
        manifest = SimpleNamespace(
            spec=SimpleNamespace(
                components={
                    "web": SimpleNamespace(
                        image="registry.example.org/team/app@sha256:" + "a" * 64
                    )
                },
                deployment=SimpleNamespace(timeoutSeconds=60),
            )
        )
        with self.assertRaisesRegex(RuntimeError, "image pull failed: denied"):
            deployer._pull_and_verify(manifest)
        podman.inspect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
