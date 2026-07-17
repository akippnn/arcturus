# Contributing

Arcturus welcomes focused changes that preserve the single-host, manifest-driven design.

## Before opening a change

- Discuss large schema, lifecycle, or provider changes in an issue or design note.
- Keep public examples generic and free of operational data.
- Do not add a second production lifecycle owner alongside Quadlet/systemd.
- Do not weaken digest pinning, service-scoped authentication, secret references, or rollback guarantees for convenience.

## Development checks

```bash
PYTHONPATH=deploy python3 -m unittest discover -s deploy/tests -v
for module in modules/bus modules/registry modules/router; do npm --prefix "$module" ci; npm --prefix "$module" run build; done
npm --prefix modules/router test
(cd rust && cargo fmt --check && cargo clippy --workspace --all-targets --all-features -- -D warnings && cargo test --workspace --all-targets)
bash -n deploy/*.sh deploy/arcturusctl
```

Also run production dependency audits and secret scans. Use Buildah and Terraform validation when changing the OCI bundle or compatibility modules.

## Pull requests

A pull request should explain:

- the operational problem
- the selected design and alternatives
- compatibility or migration effects
- security and rollback implications
- tests performed
- documentation changed

Keep commits reviewable and avoid mixing generated/runtime artifacts with source changes.
