# Host validation and issue reporting

Arcturus is designed so a host update can be reproduced, inspected, and reported without relying on hidden state. This playbook is the acceptance test for a control-plane update and the preferred evidence bundle for a public issue.

Use example or redacted hostnames, addresses, registry names, and service names in public reports. Never include deployment tokens, registry credentials, secret values, private keys, or complete environment files.

## What a healthy update means

A successful installer exit is necessary, but it is not the complete health signal. After an update:

- the exact source commit or bundle digest is known;
- the recorded host profile replays successfully through `--validate-only`;
- the Podman API, bus, registry, router, and deployer listeners are active with zero restart loops;
- the deployer health endpoint is ready;
- the operator-owned nginx configuration passes `nginx -t`;
- the registry accepts every active manifest;
- the router completes an initial reconciliation and at least one later polling cycle without errors;
- the router status file contains no failed publications;
- public endpoint results are interpreted together with access-control and application behavior.

`active (running)` alone is not sufficient. A long-running service may remain active while its polling or reconciliation loop fails repeatedly.

## 1. Pin and validate the candidate

For a local source test, check out the exact candidate ref and record it:

```bash
git fetch origin
git switch <candidate-branch>
git pull --ff-only origin <candidate-branch>
git rev-parse HEAD
git status --short --branch
```

Install, build, and test the control-plane modules:

```bash
for module in bus registry router; do
  npm ci --ignore-scripts --no-audit --no-fund --prefix "modules/$module"
  npm run build --prefix "modules/$module"
done

node --test modules/router/dist/*.test.js
node --test modules/registry/dist/*.test.js

PYTHONPATH=deploy \
  "$HOME/.local/share/arcturus-deployer/current/venv/bin/python" \
  -W error::ResourceWarning \
  -m unittest discover -s deploy/tests -q

bash -n deploy/*.sh deploy/arcturusctl deploy/arcturus-host-update
```

The bus currently has a build command but no Node test command. The registry and router suites must run, not merely compile.

## 2. Verify the replayed host profile

Inspect the saved non-secret update contract:

```bash
arcturus-host-update show
```

Check only the routing keys needed for the update:

```bash
grep -E \
  '^(VHOSTS_DIR|NGINX_CONTAINER|BASE_DOMAIN|CERT_DOMAIN|CONTAINER_CLI)=' \
  "$HOME/.config/arcturus/platform.env"
```

Confirm the configured certificate files are readable from inside the ingress container:

```bash
podman exec <nginx-container> \
  test -r /etc/letsencrypt/live/<certificate-name>/fullchain.pem

podman exec <nginx-container> \
  test -r /etc/letsencrypt/live/<certificate-name>/privkey.pem
```

Validate the candidate without changing the recorded state:

```bash
arcturus-host-update apply \
  --source-dir /path/to/arcturus/deploy \
  --validate-only
```

For production, prefer a digest-pinned bundle instead of a mutable source checkout.

## 3. Apply and record the observation window

```bash
deployed_at="$(date --iso-8601=seconds)"

arcturus-host-update apply \
  --source-dir /path/to/arcturus/deploy
```

The updater should finish with `Arcturus host update applied and recorded.` Inspect the resulting source, arguments, and update command:

```bash
arcturus-host-update show
readlink -f "$HOME/.local/share/arcturus-deployer/current"
```

The updater records successful host installation and update actions. It does not record unrelated shell commands such as `git switch`, `npm ci`, or manual configuration edits.

## 4. Verify the control plane

```bash
systemctl --user show \
  arcturus-podman-api.service \
  arcturus-bus.service \
  arcturus-registry.service \
  arcturus-router.service \
  'arcturus-deployer@*.service' \
  -p Id \
  -p ActiveState \
  -p SubState \
  -p Result \
  -p ExecMainStatus \
  -p NRestarts \
  --no-pager
```

Expected values are `ActiveState=active`, `SubState=running`, `Result=success`, `ExecMainStatus=0`, and no unexpected restart count.

Check the API and ingress syntax:

```bash
curl --fail --silent --show-error \
  http://127.0.0.1:9090/healthz | jq

podman exec <nginx-container> nginx -t
```

## 5. Read the actual runtime result

Prefer the journal when it is available:

```bash
journalctl --user \
  -u arcturus-registry.service \
  -u arcturus-router.service \
  --since "$deployed_at" \
  --no-pager -l
```

If journald prints `No journal files were found`, the check is inconclusive rather than successful. Use the unit status output, which includes recent log lines:

```bash
systemctl --user status \
  arcturus-registry.service \
  arcturus-router.service \
  --no-pager -l
```

Wait through at least one router polling interval and inspect again:

```bash
sleep 35
systemctl --user status arcturus-router.service --no-pager -l
```

Treat any repeated `Initial sync failed`, `Sync failed`, manifest rejection, unsupported engine, or network mutation error as a failed host acceptance test even when the unit remains active.

## 6. Inspect publication state correctly

List route receipts:

```bash
jq -r '
  .services
  | to_entries[]
  | [.key, .value.status, (.value.revision // "legacy"), (.value.error.code // "-")]
  | @tsv
' "$XDG_RUNTIME_DIR/arcturus/router-status.json"
```

Count failed publications using fields that exist in the current receipt contract:

```bash
jq '
  [
    .services[]
    | select(.status != "published" or has("error"))
  ]
  | length
' "$XDG_RUNTIME_DIR/arcturus/router-status.json"
```

Do not query `.verification.status` unless the installed router contract explicitly provides that field. A missing field can turn every route into a false-positive failure.

## 7. Interpret endpoint probes

A compact public probe is useful, but status codes are not interchangeable:

- `000` indicates DNS, connection, or TLS failure;
- `502`, `503`, or `504` usually indicates an upstream or routing failure;
- `200`, `301`, or `302` normally indicates a reachable application path;
- `401` or `403` proves that ingress and TLS were reachable but may be produced intentionally by the access-control layer.

A `403` is therefore not sufficient proof that the application itself is healthy.

## Common findings

### Reserved example certificate appears at runtime

An `example.org` certificate path outside fixtures means the real router host configuration was not loaded. Inspect the saved host profile and `platform.env`; do not bypass the reserved-domain guard in production.

### Registry reports that it expected v1 `Stack`

The registry accepts both the legacy v1 `Stack` and current v2 `ServiceRelease` contracts. A union-validation error may display the v1 branch prominently even when the real defect is in the v2 branch. Inspect the complete active manifest and validate its `apiVersion`, `kind`, metadata, components, routing records, and any migration policy.

### Podman fails under the router systemd sandbox

Errors involving a read-only rootless Podman runtime directory indicate a mismatch between the router's container-engine mode and its systemd sandbox. Capture the active `CONTAINER_CLI`, router unit, drop-ins, Podman API unit, and complete error before changing permissions. Prefer the intended Podman API/remote boundary over broadly weakening the unit sandbox.

### Router status is clean but the router logs are failing

The status file may still contain the last successfully published receipts. Always pair it with the current registry/router logs and a post-polling-cycle check.

## Public issue template

Include the following, with private values redacted:

```text
### Candidate
- Arcturus commit or bundle digest:
- Host OS, Podman, systemd, Python, and Node versions:

### Update
- `arcturus-host-update show`:
- `--validate-only` result:
- Apply result:

### Control plane
- `systemctl --user show` summary:
- `/healthz` result:
- `nginx -t` result:

### Registry and router
- Registry status and recent errors:
- Router status and recent errors after at least 35 seconds:
- Failed publication count:

### Application impact
- Affected service or route:
- HTTP status or client-visible behavior:
- Did automatic rollback occur?
```

Attach the smallest relevant manifest fragment or redacted log excerpt needed to reproduce the defect. Do not attach complete environment files or secret-bearing generated state.
