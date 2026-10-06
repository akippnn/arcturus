---
title: DIST-001 physical evidence runbook
kind: runbook
lifecycle: operational
authority: DIST-001 physical owner procedure
summary: Exercise the distributed fleet on arm64 and amd64 AlmaLinux workers.
maintenance:
  - The supported install, CLI, worker, or evidence procedure changes.
slice: DIST-001
---

# DIST-001 two-worker evidence runbook

This procedure is the owner-visible hardware gate. It requires one AlmaLinux
`arm64` Raspberry Pi worker, one AlmaLinux `amd64` worker, a control-plane URL
reachable by outbound connections from both workers, and one already-managed
Redis/Valkey endpoint.

The JSON under `rust/fixtures/fleet/` is contract test data. Its repeated
`bbbb...` OCI digest is intentionally not a runnable image digest. Generate the
release from an existing blueprint and replace its image with a verified,
multi-architecture manifest-list digest before this procedure. Keep the
release service-only, without local volumes or legacy migration, so its
target-first overlap safety can be established mechanically.

## 1. Prepare the control plane

Create a fleet-operator credential with the existing token command:

```console
config_dir="$(arcturus_paths.py --get config_dir)"
arcturusctl token create \
  --database "$config_dir/fleet-tokens.json" \
  --fleet-operator \
  --token-id dist-001-operator \
  --output "$config_dir/fleet-operator.token"
```

The extracted bundle must contain `deploy/` and its sibling `modules/`. Pass
`deploy/` as `--source-dir`; the installer requires an existing vhosts directory
even when routing is disabled. Create it before either installation:

```console
mkdir -p "$HOME/.local/share/arcturus/vhosts"
```

Install or update a dedicated fleet control plane on a private or tailnet IP:

```console
deploy/install-host.sh --source-dir /path/to/unpacked-arcturus-bundle/deploy \
  --vhosts-dir "$HOME/.local/share/arcturus/vhosts" \
  --enable-fleet-control-plane \
  --fleet-listen-address 100.64.0.10
```

Set the operator defaults:

```console
export ARCTURUS_FLEET_API_URL=http://100.64.0.10:9190
export ARCTURUS_FLEET_TOKEN_FILE="$config_dir/fleet-operator.token"
```

DIST-001 permits a private non-loopback listener for a dedicated fleet control
plane. The installer refuses an unrestricted listener. A combined non-loopback
fleet listener plus OCI authorization is deferred until `arcturusd` supports
separate listeners; the existing OCI/Tailscale ingress remains loopback-based.
The HTTP examples assume that `100.64.0.10` is carried by an authenticated,
encrypted tailnet. Use HTTPS when the control-plane connection is not already
protected by a trusted encrypted transport; worker credentials are long-lived
bearer secrets and must not cross an untrusted plaintext network.

## 2. Enroll and install both workers

Run on the operator machine:

```console
arcturusctl fleet worker enroll pi-arm64 \
  --topology site=edge-a \
  --credential-output ./pi-arm64.worker-token
arcturusctl fleet worker enroll alma-amd64 \
  --topology site=edge-a \
  --credential-output ./alma-amd64.worker-token
arcturusctl fleet worker list
```

Transfer each credential only to its named worker. On each worker, create the
existing service-scoped lifecycle token used by the agent:

```console
config_dir="$(arcturus_paths.py --get config_dir)"
arcturusctl token create \
  --database "$config_dir/tokens.json" \
  --service dist-redis-client \
  --token-id dist-redis-client-agent \
  --output "$config_dir/lifecycle-tokens/dist-redis-client.token"
```

Then install the outbound agent, substituting that worker's ID and credential:

```console
deploy/install-host.sh --source-dir /path/to/unpacked-arcturus-bundle/deploy \
  --vhosts-dir "$HOME/.local/share/arcturus/vhosts" \
  --enable-worker-agent \
  --worker-id pi-arm64 \
  --control-plane-url http://100.64.0.10:9190 \
  --worker-token-file ./pi-arm64.worker-token
```

No worker listener, SSH orchestration, or public worker address is required.
Confirm that `arcturusctl fleet worker list` reports fresh inventory for both
workers and shows `arm64` and `amd64` respectively.

## 3. Prepare the imported Redis binding

On both workers, create the same existing Podman secret from the imported
Redis URL. The URL remains data-plane material consumed by `redis-cli` over
RESP; the fleet database never stores it.

```console
printf '%s' "$REDIS_URL" | podman secret create dist-redis-url -
```

Import only the resource identity and material reference:

```console
arcturusctl fleet resource import rust/fixtures/fleet/imported-redis-resource.json
```

## 4. Apply and recover the release

Create the workload-intent file by wrapping the authoritative,
blueprint-generated `ServiceRelease v2`. Set `placement.workerId` to the first
worker and retain the stateless transition policy shown in the fixture. The
CLI validates and canonicalizes the release and derives its characteristics:

```console
arcturusctl fleet service apply ./dist-001-intent.json
arcturusctl fleet service status dist-redis-client
arcturusctl fleet service explain dist-redis-client
```

The status must become `healthy`, show the exact release digest and deployment
ID, and show published local routing state when the release declares routing.
The fixture client logs repeated `PONG` responses from `redis-cli`, proving
normal RESP access through the injected secret.

On the assigned worker, kill its container:

```console
podman kill arcturus-dist-redis-client-client
```

Verify systemd restarts the existing release and fleet status returns to
`healthy` without a new deployment ID or assignment generation.

## 5. Move target-first

Move explicitly to the other worker, using the current intent generation from
status:

```console
arcturusctl fleet service move dist-redis-client \
  --worker-id alma-amd64 \
  --expected-generation 1
arcturusctl fleet service status dist-redis-client
```

Capture status at `awaitingTarget`, after exact target health, and after the
source reports `absent`. The source assignment must remain `ensurePresent`
until target health matches both target assignment generation and release
digest. The final source assignment remains an authoritative `ensureAbsent`
tombstone; DIST-001 performs no tombstone garbage collection.

## 6. Record outage behavior

While the target is healthy, stop the control plane briefly. Confirm the agent
logs connection failures while systemd and the application remain healthy.
No new move or placement decision should be possible. Restart the control
plane and confirm heartbeat, polling, and observations resume without changing
the accepted workload generation.

Record command output, worker journals, deployment IDs, assignment generations,
release digests, container recovery timing, `PONG` evidence, and the movement
phase ordering in a new immutable file under `evidence/` before requesting
owner acceptance. Add that file to `manifest.yaml` and point the hardware gate
to it.

## Single-Pi engineering trial and SD write reduction

The 2026-10-06 Hori trial verified one AlmaLinux ARM64 worker, RESP binding,
container recovery, and continued operation during a control-plane outage.
The second worker and cross-architecture movement remain pending. See
[evidence](evidence/hori-arm64-ram-20261006.md) for exact scope and artifacts.

Build Linux ARM64 artifacts on the laptop. For the bounded Pi trial, fleet,
agent, and lifecycle databases; active manifests; rootless Podman graph/run
storage; and staging logs use `/run/user/1000/arcturus/trial`, a dedicated
512 MiB tmpfs mounted with `noswap`. The verified memory headroom was more
than 6 GiB. Journald was already using `/run/log/journal`. This reduces trial
SD writes; it is not evidence of the card's remaining lifespan. Production
rootful containers keep their existing storage and services.

This configuration is volatile. The user-unit drop-in
`~/.config/systemd/user/arcturus-.service.d/20-volatile-trial.conf` and
`~/.config/systemd/user/arcturusd.service.d/20-volatile-trial.conf` require
the mount, so Arcturus trial units remain stopped after a reboot until RAM
state is restored. Do not rerun the original migration script blindly: it
refuses existing backups and rootless storage configuration.

Before stopping or rebooting Hori, take consistent SQLite backups using the
SQLite backup API and copy them to the laptop. The captured laptop checkpoint
contains fleet, agent, and lifecycle databases in a private directory; keep
these out of Git and public evidence. Restoring requires mounting tmpfs with
the same owner and options, restoring all three databases to their configured
paths, rebuilding the rootless network/provider/secret/image store, and
restoring active manifests from the matching deployment. Verify local release
identity before starting trial units. Credentials remain in the mode-0600
Pi configuration files.

For rollback to the pre-trial SD state, stop only the rootless trial units
and containers first. Preserve a new private checkpoint, restore the four
`~/.config/arcturus/*.env.pre-ram-checkpoint` files to their original `.env`
paths, archive the trial `~/.config/containers/storage.conf` (there was no
prior file), and archive both volatile-trial drop-ins. Restore the staging path
from `~/.cache/arcturus-dist001-20261005-sd-checkpoint`, then reload the user
manager. The original rootless storage and pre-trial databases remain on SD;
they predate the successful RAM workload. Reconcile deliberately before
starting any workload. Do not replace rootful production storage.
