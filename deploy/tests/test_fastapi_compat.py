import os
import unittest

os.environ.setdefault("WEBHOOK_SECRET", "")
os.environ.setdefault("CLOUDFLARE_API_TOKEN", "")
os.environ.setdefault("CLOUDFLARE_ZONE_ID", "")
os.environ.setdefault("DISCORD_WEBHOOK_URL", "")
os.environ.setdefault("CERT_DOMAIN", "example.org")

import app as deploy_app


class FastAPICompatibilityTests(unittest.TestCase):
    def test_route_table_and_openapi_schema_build(self):
        expected_paths = {"/healthz", "/v1/deployments", "/v1/preflight"}
        route_paths = {route.path for route in deploy_app.app.routes}

        self.assertTrue(expected_paths.issubset(route_paths))

        schema = deploy_app.app.openapi()
        self.assertTrue(expected_paths.issubset(schema["paths"]))
        self.assertEqual(schema["info"]["title"], "Arcturus Deploy Service")


if __name__ == "__main__":
    unittest.main()
