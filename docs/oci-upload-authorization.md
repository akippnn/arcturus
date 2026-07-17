# OCI upload grants and registry token authorization

This document describes the Rust preview implementation for policy-gated OCI uploads. It is not yet an installed production service and it does not change the read-only, loopback-only registry introduced by the local OCI data-plane phase.

## Boundary

The Rust `arcturusd` preview owns two related credentials:

1. An existing service-scoped Arcturus control-plane token authorizes creation of an upload grant.
2. A newly generated, short-lived upload username and secret authenticates only to the Registry v2 token endpoint.

The registry credential is not stored in GitHub and is returned only once. Arcturus stores a one-way hash of its high-entropy secret.

## Upload grant request

```http
POST /v1/artifact-uploads
Authorization: Bearer <service-scoped Arcturus token>
Content-Type: application/json
```

```json
{
  "service": "stellar-project",
  "revision": "0123456789abcdef0123456789abcdef01234567",
  "components": ["api", "web"]
}
```

The response binds the grant to exact repository names:

```json
{
  "uploadId": "<uuid>",
  "registry": "arcturus.internal:9443",
  "repositories": {
    "api": "stellar-project/api",
    "web": "stellar-project/web"
  },
  "expiresAt": "<RFC3339 timestamp>",
  "credential": {
    "username": "upload-<uuid>",
    "secret": "<one-time high-entropy secret>"
  }
}
```

Responses containing credentials use `Cache-Control: no-store` and `Pragma: no-cache`.

## Registry v2 token flow

A standard OCI client first receives the Registry Bearer challenge, then requests a token from:

```http
GET /auth/token?service=arcturus-oci&scope=repository:stellar-project/api:pull,push
Authorization: Basic <upload username:secret>
```

The Rust issuer:

- authenticates the stored upload grant in constant time;
- rejects expired or near-expiry grants;
- requires the configured registry service/audience;
- intersects requested actions with `pull` and `push`;
- omits every repository outside the exact grant;
- issues an Ed25519-signed JWT with `iss`, `sub`, `aud`, `exp`, `nbf`, `iat`, `jti`, and `access` claims;
- returns no refresh token.

The OCI token protocol authorizes repositories and actions, not individual tags. The future artifact-completion and receipt phase must therefore enforce the requested Git revision, manifest digest, component ownership, and policy before deployment eligibility.

## Key and state files

The preview binary expects:

```text
ARCTURUSD_STATE_DB
RUNNER_TOKENS_FILE
ARCTURUS_OCI_SIGNING_KEY
ARCTURUS_OCI_JWKS_FILE
ARCTURUS_OCI_REGISTRY
ARCTURUS_OCI_TOKEN_ISSUER
ARCTURUS_OCI_TOKEN_SERVICE
ARCTURUS_OCI_UPLOAD_TTL_SECONDS
```

`ARCTURUS_OCI_SIGNING_KEY` points to a mode `0600` file containing a base64 or base64url encoded 32-byte Ed25519 seed. The token database must also be mode `0600`. The grant database is created mode `0600` under a mode `0700` parent.

At startup, `arcturusd` atomically writes a public-only JWKS file at `ARCTURUS_OCI_JWKS_FILE` (mode `0644` beneath a mode `0700` state directory). Distribution currently consumes a local JWKS file, so the host-integration phase will mount that file read-only into the registry container.

The same public verification key is also available for diagnostics from:

```http
GET /v1/oci/jwks.json
```

The response contains only the Ed25519 public key. The private signing seed is never returned or logged.

## Current non-production status

This phase intentionally does not:

- install or start `arcturusd` on production hosts;
- configure Distribution token authentication;
- make the registry writable;
- bind the registry to a Tailscale address;
- accept an uploaded manifest as a deployable artifact;
- create artifact receipts;
- alter CrownFi CI.

Those changes require the next host-integration and artifact-verification phases. Until then, the installed OCI data plane remains read-only on loopback.
