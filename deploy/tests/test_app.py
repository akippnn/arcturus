import os
import json
import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("WEBHOOK_SECRET", "")
os.environ.setdefault("CLOUDFLARE_API_TOKEN", "")
os.environ.setdefault("CLOUDFLARE_ZONE_ID", "")
os.environ.setdefault("DISCORD_WEBHOOK_URL", "")

import app as deploy_app
from fastapi import Response
from pydantic import ValidationError
from release import DeploymentFailure, DeploymentRequest


class ValidationTests(unittest.TestCase):
    def test_v2_health_is_authenticated_and_returns_aggregate_state(self):
        deployer = unittest.mock.Mock()
        deployer.health.return_value = {"status": "healthy", "services": {}}
        with (
            patch.object(deploy_app, "verify_auth") as verify,
            patch.object(deploy_app, "get_release_deployer", return_value=deployer),
        ):
            result = deploy_app.get_aggregate_health("Bearer test")
        verify.assert_called_once_with("Bearer test")
        self.assertEqual(result["status"], "healthy")

    def test_hashed_and_legacy_token_files_coexist_during_migration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            hashed = root / "tokens.json"
            legacy = root / "legacy.json"
            hashed.write_text(json.dumps({"version": 2, "tokens": [{"id": "hashed"}]}))
            legacy.write_text(json.dumps([{"name": "legacy", "token": "value"}]))
            with (
                patch.object(deploy_app, "TOKEN_FILE", hashed),
                patch.object(deploy_app, "LEGACY_TOKEN_FILE", legacy),
            ):
                records = deploy_app._token_records()
            self.assertEqual([record.get("id") or record.get("name") for record in records], ["hashed", "legacy"])

    def test_hashed_scoped_token_authentication(self):
        token = "test-token-that-is-not-logged"
        salt = b"0123456789abcdef"
        digest = hashlib.scrypt(token.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
        payload = {
            "version": 2,
            "tokens": [{
                "id": "test",
                "algorithm": "scrypt",
                "salt": base64.urlsafe_b64encode(salt).decode(),
                "hash": base64.urlsafe_b64encode(digest).decode(),
                "services": ["example-portal"],
            }],
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            deploy_app, "TOKEN_FILE", Path(temp_dir) / "tokens.json"
        ):
            deploy_app.TOKEN_FILE.write_text(json.dumps(payload))
            deploy_app.authorize_service(f"Bearer {token}", "example-portal")
            with self.assertRaises(deploy_app.HTTPException) as forbidden:
                deploy_app.authorize_service(f"Bearer {token}", "another-service")
            self.assertEqual(forbidden.exception.status_code, 403)

    def test_deploy_request_rejects_path_traversal(self):
        with self.assertRaises(ValidationError):
            deploy_app.DeployRequest(stack="../outside")

    def test_deploy_request_rejects_domains_outside_managed_zone(self):
        with self.assertRaises(ValidationError):
            deploy_app.DeployRequest(stack="example", domain="example.net")

    def test_dns_request_rejects_domains_outside_managed_zone(self):
        with self.assertRaises(ValidationError):
            deploy_app.DNSRequest(
                name="example.net",
                content="target.example.net",
            )

    def test_resolve_stack_dir_rejects_escape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(deploy_app, "STACKS_BASE", Path(temp_dir)):
                with self.assertRaises(deploy_app.HTTPException):
                    deploy_app.resolve_stack_dir("../outside")

    def test_apex_uses_qualified_cloudflare_record_name(self):
        response = unittest.mock.Mock()
        response.json.side_effect = [
            {"success": True, "result": []},
            {"success": True, "result": {}},
        ]
        response.raise_for_status.return_value = None

        with (
            patch.object(deploy_app, "CF_API_TOKEN", "test-token"),
            patch.object(deploy_app, "CF_ZONE_ID", "test-zone"),
            patch("httpx.request", return_value=response) as request,
        ):
            deploy_app.cloudflare_dns("@", deploy_app.DOMAIN)

        create_call = request.call_args_list[1]
        self.assertEqual(create_call.kwargs["json"]["name"], deploy_app.DOMAIN)

    def test_tcp_registration_rolls_back_invalid_nginx_config(self):
        failed_test = unittest.mock.Mock(returncode=1, stderr="invalid config")
        request = deploy_app.TCPRequest(
            name="example-service",
            domain=deploy_app.DOMAIN,
            listen_port=2525,
            target="mail-service:2525",
        )

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(deploy_app, "PORTAL_STREAMS", Path(temp_dir)),
            patch.object(deploy_app, "verify_auth"),
            patch.object(deploy_app.subprocess, "run", return_value=failed_test),
        ):
            result = deploy_app.register_tcp(request, "Bearer test")
            self.assertEqual(result["status"], "error")
            self.assertFalse((Path(temp_dir) / "example-service.conf").exists())

    def test_standalone_dns_updates_existing_record(self):
        request = deploy_app.DNSRequest(
            name=f"app.{deploy_app.DOMAIN}",
            content=f"target.{deploy_app.DOMAIN}",
        )
        with (
            patch.object(deploy_app, "CF_API_TOKEN", "test-token"),
            patch.object(deploy_app, "CF_ZONE_ID", "test-zone"),
            patch.object(deploy_app, "verify_auth"),
            patch.object(
                deploy_app,
                "cloudflare_request",
                side_effect=[
                    {"success": True, "result": [{"id": "record-id"}]},
                    {"success": True, "result": {"id": "record-id"}},
                ],
            ) as cloudflare,
        ):
            result = deploy_app.update_dns(request, "Bearer test")

        self.assertEqual(result["operation"], "updated")
        self.assertEqual(cloudflare.call_args_list[1].args[0], "PUT")
        self.assertIn("record-id", cloudflare.call_args_list[1].args[1])

    def test_v2_deployment_failure_returns_non_success_http_status(self):
        digest = "sha256:" + "a" * 64
        revision = "1" * 40
        payload = {
            "service": "example-portal",
            "commit_sha": revision,
            "manifest": {
                "apiVersion": "arcturus.u128.org/v2",
                "kind": "ServiceRelease",
                "metadata": {"name": "example-portal", "revision": revision},
                "spec": {
                    "components": {
                        "web": {
                            "image": f"registry.example.org/example/portal@{digest}",
                            "healthCheck": {"command": "wget -q -O /dev/null http://127.0.0.1/"},
                        }
                    }
                },
            },
        }
        deployer = unittest.mock.Mock()
        deployer.deploy.side_effect = DeploymentFailure(
            "activation failed token=hidden", rollback={"status": "succeeded"}
        )
        with (
            patch.object(deploy_app, "authorize_service"),
            patch.object(deploy_app, "get_release_deployer", return_value=deployer),
        ):
            response = Response()
            body = deploy_app.create_deployment(
                DeploymentRequest.model_validate(payload), response, "Bearer test"
            )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(body["status"], "failed")
        self.assertNotIn("hidden", json.dumps(body))

    def test_v2_deployment_success_requires_succeeded_result(self):
        digest = "sha256:" + "a" * 64
        revision = "1" * 40
        payload = {
            "service": "example-portal",
            "commit_sha": revision,
            "manifest": {
                "apiVersion": "arcturus.u128.org/v2",
                "kind": "ServiceRelease",
                "metadata": {"name": "example-portal", "revision": revision},
                "spec": {
                    "components": {
                        "web": {"image": f"registry.example.org/example/portal@{digest}"}
                    }
                },
            },
        }
        deployer = unittest.mock.Mock()
        deployer.deploy.return_value = {"status": "succeeded", "deployment_id": "test"}
        with (
            patch.object(deploy_app, "authorize_service"),
            patch.object(deploy_app, "get_release_deployer", return_value=deployer),
        ):
            response = Response()
            body = deploy_app.create_deployment(
                DeploymentRequest.model_validate(payload), response, "Bearer test"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "succeeded")

    def test_v2_failed_rollback_returns_http_500(self):
        digest = "sha256:" + "a" * 64
        revision = "1" * 40
        payload = {
            "service": "example-portal",
            "commit_sha": revision,
            "manifest": {
                "apiVersion": "arcturus.u128.org/v2",
                "kind": "ServiceRelease",
                "metadata": {"name": "example-portal", "revision": revision},
                "spec": {"components": {"web": {"image": f"registry.example.org/example/portal@{digest}"}}},
            },
        }
        deployer = unittest.mock.Mock()
        deployer.deploy.side_effect = DeploymentFailure(
            "routing restoration failed", rollback={"status": "failed"}
        )
        with (
            patch.object(deploy_app, "authorize_service"),
            patch.object(deploy_app, "get_release_deployer", return_value=deployer),
        ):
            response = Response()
            body = deploy_app.create_deployment(
                DeploymentRequest.model_validate(payload), response, "Bearer test"
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(body["rollback"]["status"], "failed")

    def test_standalone_dns_creates_missing_record(self):
        request = deploy_app.DNSRequest(
            name=f"app.{deploy_app.DOMAIN}",
            content=f"target.{deploy_app.DOMAIN}",
        )
        with (
            patch.object(deploy_app, "CF_API_TOKEN", "test-token"),
            patch.object(deploy_app, "CF_ZONE_ID", "test-zone"),
            patch.object(deploy_app, "verify_auth"),
            patch.object(
                deploy_app,
                "cloudflare_request",
                side_effect=[
                    {"success": True, "result": []},
                    {"success": True, "result": {"id": "new-record-id"}},
                ],
            ) as cloudflare,
        ):
            result = deploy_app.update_dns(request, "Bearer test")

        self.assertEqual(result["operation"], "created")
        self.assertEqual(cloudflare.call_args_list[1].args[0], "POST")


if __name__ == "__main__":
    unittest.main()
