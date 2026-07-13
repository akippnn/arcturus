# Legacy Terraform/Compose deploy module

> **Deprecated:** retained for migration reference only. New releases use the manifest-driven Quadlet control plane.


The shared `arcturus-deploy` Terraform module is the bridge between service
repositories and the host runtime. Service repos call it from their own
`terraform/main.tf`. The intended deployment contract mounts the repo copy at
`/terraform-modules/arcturus-deploy`; production verification on 2026-06-27
found that the active deployer still mounted the older host-only module source.

## Caller Contract

Every blueprint-style service should pass:

| Input | Purpose |
| --- | --- |
| `app_name` | Stack slug and managed stack directory name. |
| `domain` | Public domain used in the nginx `server_name`. |
| `tier` | `complex` for internal services, `simple` for static/external proxy targets. |
| `target_url` | Upstream URL used by nginx, usually an internal container URL. |
| `compose_content` | `file("../compose.yaml")`, copied into the managed stack. |
| `deploy_trigger` | Commit SHA or unique deployment trigger used to force lifecycle recreation. |

Optional inputs include `portal_vhosts_dir`, `stacks_base_dir`,
`protect_compose`, `cert_domain`, `skip_restart`, `nginx_container`,
`custom_nginx_server_config`, and `custom_nginx_location_config`.

## Managed Resources

The production module currently manages four major concerns:

- **Ingress**: renders an nginx vhost into a generated file and copies it into
  the portal vhost directory, then reloads nginx.
- **Compose desired state**: writes the caller's `compose.yaml` into the managed
  stack directory.
- **Dockge protection**: writes `.dockge-protect` and seals `compose.yaml` with
  read-only permissions when protection is enabled.
- **Lifecycle**: runs `scripts/up.sh` on apply and `scripts/down.sh` on destroy.

## Lifecycle Scripts

`up.sh` expects an app name and optional stacks base directory. It verifies that
the managed `compose.yaml` exists, runs `docker compose pull`, then runs
`docker compose up -d --remove-orphans`. If `scripts/post-up.sh` exists in the
stack repo, it is made executable and run after the stack starts.

`down.sh` runs `scripts/pre-down.sh` when present, calls
`docker compose down --remove-orphans`, removes lingering containers whose names
match the app name, and removes `.dockge-protect`.

These hooks are useful for migrations and cleanup, but they are code execution
from the service repository. Treat them with the same review expectations as
application code.

## Important Behavior

- `deploy_trigger` is the preferred way to tie deployment to a specific commit.
  New stacks should include it in both Terraform and workflow payloads.
- The module supports raw nginx config injection. This is powerful, but it means
  repository owners can affect gateway behavior.
- The module shells out to Docker and nginx. Its input validation needs to be
  paired with deployer-side request validation.
- The module source is vendored at `terraform-modules/arcturus-deploy`. Keep
  production deployments mounted to this source so module changes are reviewable
  and recoverable from Git.

## Remaining Hardening

- Strengthen `target_url` validation beyond its current scheme check.
- Replace or tightly allowlist raw custom nginx snippets.
- Fail deployment when nginx config testing fails instead of only deferring
  reloads.
- Make hook execution opt-in per stack, or require explicit metadata that says
  hooks are expected.
- Keep generated files and Terraform state out of Git.
- Tag or otherwise version module changes so a service deployment can be traced
  to both an app commit and a module commit.
