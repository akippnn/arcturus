# Release process

This process applies to public source releases. Private host inventory and credential-rotation records remain in the private operations repository.

## 1. Prepare the candidate

- update `VERSION` and `CHANGELOG.md`
- confirm the roadmap status and compatibility notes
- ensure public documentation uses example infrastructure values
- remove generated/runtime files and verify `.gitignore`
- confirm internal Node packages are marked private
- review dependency and base-image changes

## 2. Validate the current tree

Run:

```bash
python3 -m compileall deploy
python3 -W error::ResourceWarning -m unittest discover -s deploy/tests -v
for module in modules/bus modules/registry modules/router; do npm --prefix "$module" ci; npm --prefix "$module" run build; done
npm --prefix modules/router test
for module in modules/bus modules/registry modules/router; do npm --prefix "$module" audit --audit-level=low; done
bash -n deploy/*.sh deploy/arcturusctl
```

Run Terraform formatting/validation for compatibility modules when Terraform is installed. Build the control-plane test target with Buildah when available.

## 3. Scan the exact public history

Use a clean public repository, not a mirror of private refs. Run Gitleaks across the entire history and an independent search for:

- bearer tokens, webhooks, API keys, private keys, and credential URLs
- `.env`, credential, certificate, state, database, and log files
- production domains, usernames, home paths, private addresses, and service inventory
- stashes, backup refs, rewritten-history refs, worktree refs, and tool checkpoints

## 4. Validate the archive and clone

```bash
git archive --format=tar HEAD | tar -tf -
git clone --no-local <bare-candidate> /tmp/arcturus-release-check
```

Run the test suite from the clean external clone. Confirm the archive contains only files expected in a source release.

## 5. Hosted CI

Require GitHub/Gitea CI to pass from the candidate commit. Do not treat a locally passing workflow as proof that hosted permissions, package availability, and expression syntax are correct.

## 6. Tag and publish

- push `main` first and require all hosted validation to pass
- create an annotated tag that exactly matches `v$(cat VERSION)`
- push only that intended tag; never push private backup branches or bundles containing auxiliary refs
- the tag workflow verifies `VERSION` and `CHANGELOG.md`, creates a source archive and complete Git bundle, records SHA-256 checksums, and opens a GitHub prerelease when the version contains a prerelease suffix
- publish the immutable control-plane OCI bundle separately and record its digest in the release notes

## 7. v1.0 host acceptance

Before v1.0, execute the real-host acceptance matrix in the [roadmap](ROADMAP.md), including reboot, unhealthy-release rollback, routing restoration, persistent-data preservation, and control-plane upgrade.
