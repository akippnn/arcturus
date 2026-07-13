# Legacy Terraform/Compose compatibility

These files document the original Arcturus deployment architecture. They are retained only to support controlled migration.

The legacy path used a mutable host source checkout, a broad `/deploy` endpoint, Terraform local provisioners, generated Compose files, an operator portal, and optional Cloudflare/notification integrations. It has a larger trust surface and weaker release determinism than the current manifest/Quadlet path.

New services must not use the legacy endpoint or Terraform application-release module. Existing services should follow [Migration](../migration.md), then remove Compose, Watchtower, and Terraform lifecycle ownership.

- [Legacy module contract](terraform-compose-module.md)

Legacy code and examples may be removed in a future major release after the migration window closes.
