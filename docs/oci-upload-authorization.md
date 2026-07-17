# OCI upload grants, verification, and receipts

Rust `arcturusd` owns the security boundary between a service-scoped deployment token and an accepted Arcturus artifact.

## Credential model

1. A service-scoped Arcturus control-plane token authorizes an upload-grant request.
2. Rust returns a one-time, short-lived username and secret restricted to exact service/component repositories.
3. Distribution exchanges that credential for an Ed25519-signed Registry v2 Bearer token.
4. Rust independently pulls the submitted digest from loopback Distribution and accepts it only after policy verification.
5. Completion persists immutable artifact/layer receipts and disables further push authentication for the grant.

The high-entropy registry secret is returned only once. Arcturus stores a one-way hash.

## Request an upload grant

```http
POST /v1/artifact-uploads
Authorization: Bearer <service-scoped Arcturus token>
Content-Type: application/json
```

```json
{
  "service": "crownfi",
  "revision": "0123456789abcdef0123456789abcdef01234567",
  "components": ["api", "web"]
}
```

The response binds the grant to exact repositories and advertises the private HTTPS registry origin:

```json
{
  "uploadId": "<uuid>",
  "registry": "registry.example-tailnet.ts.net",
  "repositories": {
    "api": "crownfi/api",
    "web": "crownfi/web"
  },
  "expiresAt": "<RFC3339 timestamp>",
  "credential": {
    "username": "upload-<uuid>",
    "secret": "<one-time secret>"
  }
}
```

Credential-bearing responses use `Cache-Control: no-store` and `Pragma: no-cache`. A request contains at most 32 components.

## Registry v2 token flow

Distribution challenges the OCI client with the same private HTTPS realm:

```http
GET /auth/token?service=arcturus-oci&scope=repository:crownfi/api:pull,push
Authorization: Basic <upload username:secret>
```

Rust:

- authenticates the stored grant in constant time;
- rejects expired, completed, revoked, or near-expiry grants;
- requires the configured service/audience;
- permits only `pull` and `push` on exact granted repositories;
- issues no refresh token;
- signs `iss`, `sub`, `aud`, `exp`, `nbf`, `iat`, `jti`, and repository `access` claims with Ed25519.

## Complete and verify the upload

After Buildah pushes every component, CI submits the returned manifest digests:

```http
POST /v1/artifact-uploads/<uploadId>/complete
Authorization: Bearer <same service-scoped Arcturus token>
Content-Type: application/json
```

```json
{
  "components": {
    "api": {"digest": "sha256:<64 lowercase hex characters>"},
    "web": {"digest": "sha256:<64 lowercase hex characters>"}
  }
}
```

Completion is idempotent only when the request exactly matches the already accepted receipts. A completed grant cannot be reused with different digests.

For each component, Rust obtains a fresh pull-only verification token and reads from the loopback Distribution endpoint. It verifies:

- the submitted manifest or index digest;
- OCI/Docker schema 2 and supported media types;
- exactly one matching configured OS/architecture entry in an index;
- config and layer descriptor sizes and digests;
- every referenced blob by streaming and hashing it;
- the `org.opencontainers.image.revision` label against the grant revision;
- component ownership through the exact `<service>/<component>` repository;
- maximum config, layer, total compressed artifact, and layer-count policy.

Verification is bounded by both queue and per-component work timeouts and by a host-configured global semaphore. The default permits two component verifications across all completion requests at once, with an accepted range of 1–16.

A successful response contains immutable accepted receipts and layer records. SQLite uses foreign keys, WAL, and a busy timeout to make concurrent API use predictable.

## Deployment eligibility

The manifest-v2 schema does not carry receipt IDs and has not been replaced.

The Python lifecycle preflight detects references hosted at `ARCTURUS_OCI_REGISTRY_HOST` and queries the same receipt database. It rejects an owned image unless an accepted receipt matches service, component, repository, manifest revision, and digest. External registry references keep the existing digest-pinned pre-pull behavior.

## Required configuration

Authorization is enabled explicitly with:

```text
ARCTURUSD_UPLOAD_AUTH_ENABLED=true
```

Managed settings include:

```text
ARCTURUSD_STATE_DB
RUNNER_TOKENS_FILE
ARCTURUS_OCI_SIGNING_KEY
ARCTURUS_OCI_JWKS_FILE
ARCTURUS_OCI_REGISTRY
ARCTURUS_OCI_REGISTRY_INTERNAL
ARCTURUS_OCI_EXPECTED_OS
ARCTURUS_OCI_EXPECTED_ARCH
ARCTURUS_OCI_MAX_LAYER_BYTES
ARCTURUS_OCI_MAX_ARTIFACT_BYTES
ARCTURUS_OCI_MAX_CONCURRENT_VERIFICATIONS
ARCTURUS_OCI_TOKEN_ISSUER
ARCTURUS_OCI_TOKEN_SERVICE
ARCTURUS_OCI_UPLOAD_TTL_SECONDS
```

The signing seed and token database must be mode `0600`. The authorization state directory is mode `0700`; the public JWKS is mode `0644` inside that protected directory. Rust also exposes the public-only key at `GET /v1/oci/jwks.json`.

When authorization is disabled, Rust exposes health routes only and Distribution remains read-only.

## Current boundary

Rust owns upload authorization, verification, and receipt persistence. It does not yet own activation, rollback, recovery, retention policy, or garbage collection. Those lifecycle operations remain in the tested Python compatibility service until parity and live-host acceptance are demonstrated.
