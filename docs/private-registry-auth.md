# Private registry authentication

Arcturus pulls application images on the host through the always-on rootless Podman API. CI push credentials are intentionally separate and are never forwarded in a release manifest.

## Host-owned credential boundary

Create a dedicated auth file as the rootless Arcturus service user. Use a pull-only registry account where the registry supports scoped credentials.

```bash
mkdir -p "$HOME/.config/arcturus"
umask 077
authfile="$HOME/.config/arcturus/registry-auth.json"
install -m 0600 /dev/null "$authfile"

printf '%s' "$REGISTRY_TOKEN" |
  podman login \
    --authfile "$authfile" \
    --username "$REGISTRY_USER" \
    --password-stdin \
    registry.example.org
```

Do not place the username or token in a release manifest, command history, systemd unit, or repository file.

Create the optional host registry environment file:

```bash
cat >"$HOME/.config/arcturus/registry.env" <<'EOF_ENV'
REGISTRY_AUTH_FILE=/home/appsvc/.config/arcturus/registry-auth.json
ARCTURUS_PRIVATE_REGISTRIES=registry.example.org
EOF_ENV
chmod 0600 "$HOME/.config/arcturus/registry.env"
```

Replace `/home/appsvc` and the example registry with the actual rootless service account path and registry host. `REGISTRY_AUTH_FILE` must be an absolute path. Multiple private registries are comma-separated.

The deployer and `arcturus-podman-api.service` load the same optional environment file. Podman consumes `REGISTRY_AUTH_FILE`; Arcturus uses the same path only to confirm that configured private registries have a protected, host-scoped credential entry.

Restart the affected user services after provisioning or rotating credentials:

```bash
systemctl --user daemon-reload
systemctl --user restart arcturus-podman-api.service
systemctl --user restart 'arcturus-deployer@*.service'
```

## Preflight behavior

For every image whose registry appears in `ARCTURUS_PRIVATE_REGISTRIES`, preflight verifies that:

- `REGISTRY_AUTH_FILE` points to a readable regular file;
- the file is not readable or writable by group or other users;
- the JSON document contains an `auths` entry for that exact registry host;
- the entry contains usable Podman-compatible credential material.

A missing or overexposed credential file fails before any image pull with an error such as:

```text
host prerequisites missing: registryAuth=registry.example.org
```

Registries not listed in `ARCTURUS_PRIVATE_REGISTRIES` are treated as public. An authentication error returned by such a registry is still surfaced directly from the Podman pull instead of becoming a later image-inspect 404.

## Pull failure behavior

Arcturus parses the complete JSON or newline-delimited JSON response returned by Podman's image-pull endpoint. Any `error` or `errorDetail` entry fails the deployment immediately, even when Podman returned HTTP 200.

Retries are limited to transport failures and HTTP 5xx responses. Authentication and authorization failures, malformed progress responses, digest mismatches, and terminal pull errors are not retried. Error text is passed through Arcturus redaction before it is logged or returned to CI.

## Verification

```bash
stat -c '%a %n' \
  "$HOME/.config/arcturus/registry.env" \
  "$HOME/.config/arcturus/registry-auth.json"

REGISTRY_AUTH_FILE="$HOME/.config/arcturus/registry-auth.json" \
  podman pull registry.example.org/team/example@sha256:<digest>
```

Both files should normally report mode `600`. Keep application deployment tokens and CI push credentials separate from this host pull credential.
