#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
workspace="$(mktemp -d)"
trap 'rm -rf "$workspace"' EXIT
stubs="$workspace/bin"
log="$workspace/tailscale.log"
mkdir -p "$stubs"

cat >"$stubs/tailscale" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == version ]]; then
  echo "${TAILSCALE_TEST_VERSION:-1.98.9}"
  exit 0
fi
printf '%q ' "$@" >>"$TAILSCALE_TEST_LOG"
printf '\n' >>"$TAILSCALE_TEST_LOG"
STUB
cat >"$stubs/curl" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
headers=""
url=""
while (($#)); do
  case "$1" in
    --dump-header) headers="$2"; shift 2 ;;
    http://*|https://*) url="$1"; shift ;;
    *) shift ;;
  esac
done
if [[ "$url" == */healthz ]]; then
  if [[ "${TAILSCALE_TEST_HEALTH:-ok}" == ok ]]; then
    printf '{"status":"ok"}'
    exit 0
  fi
  printf '{"status":"error"}'
  exit 22
fi
cat >"$headers" <<HEADERS
HTTP/2 ${TAILSCALE_TEST_STATUS:-401}
Www-Authenticate: Bearer realm="https://${TAILSCALE_TEST_HOST}/auth/token",service="arcturus-oci"
HEADERS
printf '%s' "${TAILSCALE_TEST_STATUS:-401}"
STUB
chmod +x "$stubs/tailscale" "$stubs/curl"

run_helper() {
  env PATH="$stubs:$PATH" TAILSCALE_TEST_LOG="$log" \
    TAILSCALE_TEST_HOST=registry.example.ts.net "$root/deploy/configure-oci-tailnet-ingress.sh" \
    svc:arcturus-oci registry.example.ts.net 9443
}

run_helper
[[ "$(wc -l <"$log")" -eq 2 ]]
grep -Fq -- 'serve --service=svc:arcturus-oci --https=443 --yes http://127.0.0.1:9190' "$log"
grep -Fq -- 'serve --service=svc:arcturus-oci --https=443 --set-path=/v2 --yes http://127.0.0.1:9443/v2' "$log"

if env PATH="$stubs:$PATH" TAILSCALE_TEST_LOG="$log" TAILSCALE_TEST_VERSION=1.98.8 \
  TAILSCALE_TEST_HOST=registry.example.ts.net "$root/deploy/configure-oci-tailnet-ingress.sh" \
  svc:arcturus-oci registry.example.ts.net 9443 >/dev/null 2>&1; then
  echo 'helper accepted an unsupported Tailscale version' >&2
  exit 1
fi

if env PATH="$stubs:$PATH" TAILSCALE_TEST_LOG="$log" TAILSCALE_TEST_STATUS=503 \
  TAILSCALE_TEST_HOST=registry.example.ts.net "$root/deploy/configure-oci-tailnet-ingress.sh" \
  svc:arcturus-oci registry.example.ts.net 9443 >/dev/null 2>&1; then
  echo 'helper accepted an unreachable service route' >&2
  exit 1
fi

if env PATH="$stubs:$PATH" TAILSCALE_TEST_LOG="$log" \
  TAILSCALE_TEST_HOST=wrong.example.ts.net "$root/deploy/configure-oci-tailnet-ingress.sh" \
  svc:arcturus-oci registry.example.ts.net 9443 >/dev/null 2>&1; then
  echo 'helper accepted an incorrect token realm' >&2
  exit 1
fi

if env PATH="$stubs:$PATH" TAILSCALE_TEST_LOG="$log" TAILSCALE_TEST_HEALTH=bad \
  TAILSCALE_TEST_HOST=registry.example.ts.net "$root/deploy/configure-oci-tailnet-ingress.sh" \
  svc:arcturus-oci registry.example.ts.net 9443 >/dev/null 2>&1; then
  echo 'helper accepted a broken Rust API route' >&2
  exit 1
fi

echo 'OCI tailnet ingress tests passed.'
