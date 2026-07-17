# Arcturus OCI ingress

Arcturus can receive application images directly from GitHub Actions over Tailscale without placing Gitea or another application registry in the deployment chain.

Use a currently supported CNCF Distribution v3 image pinned by digest. At the `v1.0.0-rc.1` source freeze, v3.1.1 is the current stable release and includes the upstream fix for CVE-2026-41888. Do not treat the minimum token-auth feature version as a security-support floor: select a currently supported release and review upstream advisories before installation. The installer validates digest pinning, but the operator remains responsible for the pinned release.

The production boundary deliberately separates responsibilities:

- CNCF Distribution accepts and stores Registry v2 uploads on loopback.
- Rust `arcturusd` issues short-lived repository-scoped credentials, verifies uploaded content independently, and records immutable receipts.
- Tailscale HTTPS is the only remote ingress. `/v2/*` is routed to Distribution; API and token routes are routed to Rust.
- The existing Python/FastAPI lifecycle enforces receipts for Arcturus-owned images before activating the unchanged manifest-v2 release.

## Fail-closed modes

The installer supports two explicit modes.

### Storage-only compatibility mode

Providing only a digest-pinned Distribution image installs persistent loopback storage but leaves the registry read-only:

```bash
./deploy/install-host.sh [existing options] \
  --oci-registry-image 'registry.example.org/distribution/distribution:v3.1.1@sha256:<digest>' \
  --oci-registry-port 9443
```

### Authenticated writable mode

Writable ingress requires the complete authorization and private HTTPS boundary:

```bash
./deploy/install-host.sh [existing options] \
  --oci-registry-image 'registry.example.org/distribution/distribution:v3.1.1@sha256:<digest>' \
  --oci-registry-port 9443 \
  --enable-oci-auth \
  --oci-registry-host registry.example-tailnet.ts.net \
  --oci-tailscale-service svc:arcturus-oci
```

Distribution still binds only to `127.0.0.1:<port>`. Rust binds only to `127.0.0.1:9190`. The Tailscale Service provides private HTTPS on port 443.

The installer starts Distribution read-only and unlocks writes only after all of these checks succeed:

1. the exact digest-pinned Distribution image exists locally;
2. Rust authorization starts with protected signing and state files;
3. Distribution trusts the Rust Ed25519 issuer and mounted public JWKS;
4. the dedicated Tailscale Service is approved and reachable at the configured `.ts.net` hostname;
5. anonymous `GET /v2/` returns the expected `401` Bearer challenge whose realm is the same private HTTPS origin;
6. available storage is at least twice the configured maximum artifact size;
7. the challenge remains correct after write mode is enabled.

A failure during or after the transition—including later router, deployer, or firewall setup—restores read-only mode. Explicitly disabling authorization also removes the dedicated Tailscale Service mapping. Manifest deletion remains disabled.

## Installed state

```text
~/.config/arcturus/oci-registry.env
~/.config/arcturus/oci-registry-runtime.env
~/.config/arcturus/oci-signing.seed
~/.config/containers/systemd/arcturus/arcturus-oci-registry.container
~/.local/share/arcturus-registry/
~/.local/share/arcturus-oci-auth/grants.sqlite3
~/.local/share/arcturus-oci-auth/jwks.json
```

The signing seed, runtime environment, and databases are protected host state. The JWKS contains only the public key and is mounted read-only into Distribution.

The installer and replayable host updater preserve operator-defined environment keys while atomically replacing Arcturus-managed keys. Replaced files receive a backup before activation.

## Publish from CI

Build each local image with the revision label that will be submitted to Arcturus:

```text
org.opencontainers.image.revision=<40-character Git commit SHA>
```

Then run the installed helper from a Tailscale-connected GitHub Actions job:

```bash
export ARCTURUS_URL='https://registry.example-tailnet.ts.net'
export ARCTURUS_CONTROL_TOKEN='<service-scoped token>'

arcturus-oci-publish.sh \
  crownfi "$GITHUB_SHA" \
  web=localhost/crownfi-web:build \
  api=localhost/crownfi-api:build
```

The helper performs one transaction-shaped sequence:

```text
request grant
→ Buildah login with one-time credential
→ push exact service/component repositories
→ submit manifest digests
→ Rust verifies manifests/config/layers
→ receive immutable accepted receipts
```

The short-lived registry credential is generated for the upload and is not stored as a GitHub secret. Completion makes the push credential unusable.

## Receipt enforcement and compatibility

Manifest v2 is unchanged. Its component `image` values remain digest-pinned OCI references.

When the image host matches the configured Arcturus registry hostname, deployment preflight requires an accepted receipt for the same:

- service;
- component;
- repository;
- Git revision;
- manifest digest.

External digest-pinned registries do not require an Arcturus receipt and remain available as a bounded compatibility and recovery path.

## Verify the host

```bash
systemctl --user status arcturusd.service
systemctl --user status arcturus-oci-registry.service

curl --fail http://127.0.0.1:9190/healthz
curl --silent --output /dev/null --write-out '%{http_code}\n' \
  http://127.0.0.1:9443/v2/        # expect 401 with auth enabled
curl --silent --output /dev/null --write-out '%{http_code}\n' \
  https://registry.example-tailnet.ts.net/v2/  # expect 401
```

## Deliberately unfinished release gates

The `v1.0.0-rc.1` source candidate implements authenticated ingress, verification, receipts, and deployment enforcement. It does not yet claim operational stable-release acceptance. The remaining gates are:

- migrate the Service Blueprint and CrownFi workflow to the publisher helper;
- run real GitHub Actions upload, interruption/retry, expiry, cross-service, oversize, digest-mismatch, restart/re-pull, and registry-unavailable rollback tests;
- validate clean-host installation and a live-host upgrade;
- implement release-aware retention pins and reviewed garbage collection before enabling deletion.

Until retention exists, monitor storage and keep deletion/garbage collection disabled.
