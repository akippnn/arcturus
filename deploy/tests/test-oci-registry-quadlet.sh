#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
renderer="$root/deploy/render-oci-registry-quadlet.sh"
digest="sha256:$(printf 'a%.0s' {1..64})"
output="$($renderer \
  "registry.example.org/distribution/distribution@$digest" \
  9443 \
  /home/appsvc/.local/share/arcturus-registry \
  /home/appsvc/.config/arcturus/oci-registry-runtime.env)"

grep -Fq 'PublishPort=127.0.0.1:9443:5000' <<<"$output"
grep -Fq 'Pull=never' <<<"$output"
grep -Fq 'EnvironmentFile=/home/appsvc/.config/arcturus/oci-registry-runtime.env' <<<"$output"
grep -Fq 'Volume=/home/appsvc/.local/share/arcturus-registry:/var/lib/registry:Z' <<<"$output"
! grep -Fq 'Requires=arcturusd.service' <<<"$output"

auth_output="$($renderer \
  "registry.example.org/distribution/distribution@$digest" \
  9443 \
  /home/appsvc/.local/share/arcturus-registry \
  /home/appsvc/.config/arcturus/oci-registry-runtime.env \
  /home/appsvc/.local/share/arcturus-oci-auth/jwks.json)"
grep -Fq 'Requires=arcturusd.service' <<<"$auth_output"
grep -Fq 'After=network-online.target arcturusd.service' <<<"$auth_output"
grep -Fq 'Volume=/home/appsvc/.local/share/arcturus-oci-auth/jwks.json:/etc/distribution/oci-jwks.json:ro,Z' <<<"$auth_output"

if "$renderer" registry.example.org/distribution/distribution:latest 9443 /srv/registry /srv/runtime.env >/dev/null 2>&1; then
  echo 'renderer accepted an unpinned image' >&2
  exit 1
fi
if "$renderer" "registry.example.org/distribution/distribution@$digest" 443 /srv/registry /srv/runtime.env >/dev/null 2>&1; then
  echo 'renderer accepted a privileged port' >&2
  exit 1
fi
if "$renderer" "registry.example.org/distribution/distribution@$digest" 9443 relative/path /srv/runtime.env >/dev/null 2>&1; then
  echo 'renderer accepted a relative storage path' >&2
  exit 1
fi

echo 'OCI registry Quadlet renderer tests passed.'
