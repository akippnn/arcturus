#!/usr/bin/env bash
set -euo pipefail

service="${1:?usage: configure-oci-tailnet-ingress.sh svc:NAME HOSTNAME PORT}"
hostname="${2:?usage: configure-oci-tailnet-ingress.sh svc:NAME HOSTNAME PORT}"
port="${3:?usage: configure-oci-tailnet-ingress.sh svc:NAME HOSTNAME PORT}"

[[ "$service" =~ ^svc:[a-z0-9][a-z0-9-]{0,62}$ ]] || {
  echo "Tailscale Service must use svc:<lowercase-name> format" >&2
  exit 2
}
[[ "$hostname" =~ ^[a-z0-9][a-z0-9.-]*\.ts\.net$ ]] || {
  echo "OCI registry hostname must be a full Tailscale HTTPS name ending in .ts.net" >&2
  exit 2
}
[[ "$port" =~ ^[0-9]+$ ]] && ((port >= 1024 && port <= 65535)) || {
  echo "OCI registry port must be between 1024 and 65535" >&2
  exit 2
}
command -v tailscale >/dev/null 2>&1 || {
  echo "tailscale CLI is required for OCI tailnet ingress" >&2
  exit 2
}
command -v curl >/dev/null 2>&1 || {
  echo "curl is required for OCI tailnet ingress validation" >&2
  exit 2
}

version="$(tailscale version 2>/dev/null | awk 'NR==1 {print $1}')"
normalized_version="${version%%-*}"
IFS=. read -r version_major version_minor version_patch <<<"${normalized_version:-0.0.0}"
if [[ ! "$version_major" =~ ^[0-9]+$ || ! "$version_minor" =~ ^[0-9]+$ || ! "$version_patch" =~ ^[0-9]+$ ]] ||   ((version_major < 1 || (version_major == 1 && (version_minor < 98 || (version_minor == 98 && version_patch < 9))))); then
  echo "Tailscale 1.98.9 or newer is required for the Serve/Services security fixes used by OCI ingress (found ${version:-unknown})" >&2
  exit 2
fi

# A dedicated Tailscale Service avoids replacing the node's unrelated Serve
# configuration. Service-mode Serve is persistent/backgrounded by tailscaled.
tailscale serve --service="$service" --https=443 --yes http://127.0.0.1:9190 >/dev/null
# Tailscale Serve strips the selected mount point before proxying. Include
# /v2 in the target URL so Distribution still receives its required /v2/ path.
tailscale serve --service="$service" --https=443 --set-path=/v2 --yes \
  "http://127.0.0.1:$port/v2" >/dev/null

headers="$(mktemp)"
trap 'rm -f "$headers"' EXIT
status="$(curl --silent --show-error --output /dev/null --dump-header "$headers" \
  --connect-timeout 10 --max-time 30 --write-out '%{http_code}' \
  "https://$hostname/v2/" || true)"
if [[ "$status" != 401 ]] || \
  ! grep -Eqi "^Www-Authenticate: Bearer .*realm=\"https://$hostname/auth/token\"" "$headers" || \
  ! grep -Eqi "^Www-Authenticate: Bearer .*service=\"?arcturus-oci\"?" "$headers"; then
  echo "Tailscale OCI service is not approved, reachable, or advertising the expected HTTPS token realm: https://$hostname" >&2
  exit 1
fi

health="$(curl --silent --show-error --fail --connect-timeout 10 --max-time 30   "https://$hostname/healthz" || true)"
if ! grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' <<<"$health"; then
  echo "Tailscale OCI service does not route the Rust health endpoint correctly: https://$hostname/healthz" >&2
  exit 1
fi
