# Operations

Arcturus records deployment and lifecycle operations in its state database, but systemd and journald remain the runtime authorities.

## Authentication environment

```bash
export ARCTURUS_API_URL='http://127.0.0.1:9090'
export ARCTURUS_TOKEN_FILE="$HOME/.config/arcturus/my-api.token"
```

The CLI also accepts `ARCTURUS_DEPLOY_TOKEN` for CI compatibility. Prefer a protected token file for interactive use.

Before an expensive build, validate that the API is ready and that the token is scoped to the project service:

```bash
arcturusctl project preflight .arcturus/project.json \
  --token-file "$ARCTURUS_TOKEN_FILE"
```

The preflight also verifies every Podman secret and every external named volume/network referenced by the release template. It does not expose secret values.

## Validate and preview

```bash
arcturusctl validate arcturus.release.json
arcturusctl render arcturus.release.json --output-dir /tmp/arcturus-render
arcturusctl preview arcturus.release.json
```

Project-aware commands can validate the build graph and release template together:

```bash
arcturusctl project validate --project .arcturus/project.json
arcturusctl project plan --project .arcturus/project.json --revision '<40-char-sha>'
```

## Deploy and verify

```bash
arcturusctl deploy \
  --api-url "$ARCTURUS_API_URL" \
  --service my-api \
  --commit-sha '<40-char-sha>' \
  --manifest arcturus.release.json

arcturusctl status --api-url "$ARCTURUS_API_URL" --service my-api
arcturusctl verify --api-url "$ARCTURUS_API_URL" --service my-api
```

A successful HTTP response is not sufficient by itself; automation must also require the returned operation/release status to be successful and verify the expected revision and digests.

## Runtime status and logs

```bash
systemctl --user status arcturus-my-api.target
systemctl --user list-units 'arcturus-my-api-*'
journalctl --user -u 'arcturus-my-api-*' --since today
podman ps --filter label=io.containers.autoupdate=disabled
```

For scheduled components:

```bash
systemctl --user list-timers 'arcturus-my-api-*'
journalctl --user -u 'arcturus-my-api-*.service' --since today
```

## Rollback

Roll back to the previous eligible release:

```bash
arcturusctl rollback --api-url "$ARCTURUS_API_URL" --service my-api
```

Or select a known deployment ID using the CLI's `--deployment-id` option. Verify the active revision, image digests, health, and routing receipt after rollback.

A deployment failure with successful automatic rollback is still a failed deployment. Preserve both the failure record and the restored release identity.

## Disable and enable

```bash
arcturusctl disable --api-url "$ARCTURUS_API_URL" --service my-api
arcturusctl enable  --api-url "$ARCTURUS_API_URL" --service my-api
```

Disable stops runtime ownership and withdraws active routing publication while retaining the release and data. Enable reactivates the selected release and re-runs readiness checks.

## Remove

```bash
arcturusctl remove --api-url "$ARCTURUS_API_URL" --service my-api
```

Remove stops and removes generated active ownership. It intentionally preserves:

- release archives
- audit and operation records
- bind-mounted data
- named volumes
- Podman secrets

Delete data only through a separate, reviewed storage operation.

## Token rotation

Create a replacement token before revoking the old one:

```bash
umask 077
arcturusctl token create \
  --database "$HOME/.config/arcturus/tokens.json" \
  --service my-api \
  --token-id my-api-ci-2026-02 \
  --output "$HOME/.config/arcturus/my-api-ci-2026-02.token"
```

Update CI, verify a deployment or status call, then revoke the previous token ID.

The plaintext token exists only in the output file. Store its contents as the protected CI secret `ARCTURUS_DEPLOY_TOKEN`; do not commit the file or the token database.

## Common failures

### Manifest rejected

Run local validation and check for floating image tags, secret-like environment keys, invalid references, cycles, undeclared networks, invalid schedules, or host bind paths outside the allowlist.

### Image pull or inspect failed

Verify the service account's pull-only registry login and test the exact digest-pinned image with rootless Podman.

### Unit did not become ready

Inspect the generated target and component journals. For health-checked services, inspect Podman health output. For one-shots, verify the command exits zero and is safe to rerun.

### Deployment API returned HTTP 502

When the response body contains `status: failed`, the request was authenticated and reached the release engine. Arcturus uses HTTP 502 when activation failed but rollback succeeded. This is not a missing API key. Read the returned `error.message` and `rollback` object, then inspect:

```bash
systemctl --user status 'arcturus-deployer@*'
journalctl --user -u 'arcturus-deployer@*' -n 200 --no-pager
journalctl --user -u 'arcturus-<service>-*' -n 300 --no-pager
```

### Router receipt missing or stale

Check the active manifest, registry and router services, router status file, generated vhost, nginx test/reload logs, and whether the service joins the routing network.

### Rootless services disappear after reboot

Verify lingering, the user manager, the dedicated Podman API unit, the active service targets/timers, and required external networks. Do not treat an aggregate compatibility unit as authoritative when individual units are healthy.
