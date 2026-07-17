#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
workspace="$(mktemp -d)"
trap 'rm -rf "$workspace"' EXIT
stubs="$workspace/bin"
log="$workspace/buildah.log"
mkdir -p "$stubs"
revision="$(printf 'a%.0s' {1..40})"
digest_web="sha256:$(printf 'b%.0s' {1..64})"
digest_api="sha256:$(printf 'c%.0s' {1..64})"

cat >"$stubs/curl" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
[[ -z "${ARCTURUS_CONTROL_TOKEN:-}" ]]
output=""
data=""
url=""
max_time=""
while (($#)); do
  case "$1" in
    --output) output="$2"; shift 2 ;;
    --data-binary) data="$2"; shift 2 ;;
    --max-time) max_time="$2"; shift 2 ;;
    --connect-timeout) shift 2 ;;
    --config|--header|--request|--write-out) shift 2 ;;
    --silent|--show-error) shift ;;
    http*) url="$1"; shift ;;
    *) shift ;;
  esac
done
if [[ "$url" == */v1/artifact-uploads ]]; then
  request="$(tr -d '\n' <"${data#@}")"
  expected_request='{"service": "crownfi", "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "components": ["web", "api"]}'
  [[ "$request" == "$expected_request" ]]
  upload_id="${TEST_UPLOAD_ID:-123e4567-e89b-42d3-a456-426614174000}"
  registry="${TEST_GRANT_REGISTRY:-registry.example.ts.net}"
  cat >"$output" <<JSON
{"uploadId":"$upload_id","registry":"$registry","repositories":{"web":"crownfi/web","api":"crownfi/api"},"expiresAt":"2026-07-17T00:00:00Z","credential":{"username":"upload-user","secret":"upload-secret"}}
JSON
  printf 201
elif [[ "$url" == */v1/artifact-uploads/123e4567-e89b-42d3-a456-426614174000/complete ]]; then
  [[ "$max_time" == "${TEST_EXPECT_COMPLETION_TIMEOUT:-600}" ]]
  request="$(tr -d '\n' <"${data#@}")"
  expected_request="{\"components\": {\"api\": {\"digest\": \"$TEST_DIGEST_API\"}, \"web\": {\"digest\": \"$TEST_DIGEST_WEB\"}}}"
  [[ "$request" == "$expected_request" ]]
  cat >"$output" <<JSON
{"uploadId":"${TEST_UPLOAD_ID:-123e4567-e89b-42d3-a456-426614174000}","status":"accepted","receipts":[{"component":"web","manifestDigest":"$TEST_DIGEST_WEB"},{"component":"api","manifestDigest":"$TEST_DIGEST_API"}]}
JSON
  printf 201
else
  echo "unexpected curl URL: $url" >&2
  exit 1
fi
STUB

cat >"$stubs/buildah" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
[[ -z "${ARCTURUS_CONTROL_TOKEN:-}" ]]
printf '%q ' "$@" >>"$TEST_BUILDAH_LOG"
printf '\n' >>"$TEST_BUILDAH_LOG"
while [[ "${1:-}" == --root || "${1:-}" == --runroot ]]; do shift 2; done
case "$1" in
  login)
    IFS= read -r secret || true
    [[ "$secret" == upload-secret ]]
    ;;
  inspect)
    cat <<JSON
{"Docker":{"config":{"Labels":{"org.opencontainers.image.revision":"${TEST_REVISION}"}}}}
JSON
    ;;
  push)
    digest_file=""
    image=""
    while (($#)); do
      case "$1" in
        --digestfile) digest_file="$2"; shift 2 ;;
        --authfile) shift 2 ;;
        push) shift ;;
        docker://*) shift ;;
        *) image="$1"; shift ;;
      esac
    done
    case "$image" in
      local-web) printf '%s\n' "$TEST_DIGEST_WEB" >"$digest_file" ;;
      local-api) printf '%s\n' "$TEST_DIGEST_API" >"$digest_file" ;;
      *) exit 1 ;;
    esac
    ;;
  *) exit 1 ;;
esac
STUB
chmod +x "$stubs/curl" "$stubs/buildah"

digest_dir="$workspace/digests"
receipt_file="$workspace/receipt.json"
output="$(env PATH="$stubs:$PATH" \
  ARCTURUS_URL=https://registry.example.ts.net \
  ARCTURUS_CONTROL_TOKEN=control-token \
  ARCTURUS_BUILDAH_ROOT="$workspace/storage" \
  ARCTURUS_BUILDAH_RUNROOT="$workspace/runroot" \
  ARCTURUS_OCI_DIGEST_DIR="$digest_dir" \
  ARCTURUS_OCI_RECEIPT_FILE="$receipt_file" \
  TEST_BUILDAH_LOG="$log" TEST_REVISION="$revision" \
  TEST_DIGEST_WEB="$digest_web" TEST_DIGEST_API="$digest_api" \
  "$root/deploy/arcturus-oci-publish.sh" crownfi "$revision" web=local-web api=local-api)"

grep -Fq '"status":"accepted"' <<<"$output"
grep -Fq 'login --authfile' "$log"
grep -Fq 'push --authfile' "$log"
grep -Fq -- '--root' "$log"
test "$(cat "$digest_dir/web.digest")" = "$digest_web"
test "$(cat "$digest_dir/api.digest")" = "$digest_api"
grep -Fq '"status":"accepted"' "$receipt_file"
grep -Fq 'docker://registry.example.ts.net/crownfi/web:upload-123e4567-e89b-42d3-a456-426614174000' "$log"
grep -Fq 'docker://registry.example.ts.net/crownfi/api:upload-123e4567-e89b-42d3-a456-426614174000' "$log"

if env PATH="$stubs:$PATH" ARCTURUS_URL=https://registry.example.ts.net \
  ARCTURUS_CONTROL_TOKEN=control-token TEST_BUILDAH_LOG="$log" \
  TEST_REVISION="$(printf 'd%.0s' {1..40})" TEST_DIGEST_WEB="$digest_web" \
  "$root/deploy/arcturus-oci-publish.sh" crownfi "$revision" web=local-web api=local-api \
  >/dev/null 2>&1; then
  echo 'publisher accepted an image with the wrong revision label' >&2
  exit 1
fi

if env PATH="$stubs:$PATH" ARCTURUS_URL=https://registry.example.ts.net \
  ARCTURUS_CONTROL_TOKEN=$'control-token\nheader = "X-Injected: true"' TEST_BUILDAH_LOG="$log" \
  TEST_REVISION="$revision" TEST_DIGEST_WEB="$digest_web" \
  "$root/deploy/arcturus-oci-publish.sh" crownfi "$revision" web=local-web api=local-api \
  >/dev/null 2>&1; then
  echo 'publisher accepted a control token containing a header injection' >&2
  exit 1
fi

if env PATH="$stubs:$PATH" ARCTURUS_URL=https://registry.example.ts.net \
  ARCTURUS_CONTROL_TOKEN=control-token TEST_BUILDAH_LOG="$log" \
  TEST_REVISION="$revision" TEST_DIGEST_WEB="$digest_web" TEST_DIGEST_API="$digest_api" \
  TEST_GRANT_REGISTRY=attacker.example.ts.net \
  "$root/deploy/arcturus-oci-publish.sh" crownfi "$revision" web=local-web api=local-api \
  >/dev/null 2>&1; then
  echo 'publisher accepted credentials for a different registry host' >&2
  exit 1
fi

if env PATH="$stubs:$PATH" ARCTURUS_URL=https://registry.example.ts.net \
  ARCTURUS_CONTROL_TOKEN=control-token TEST_BUILDAH_LOG="$log" \
  TEST_REVISION="$revision" TEST_DIGEST_WEB="$digest_web" TEST_DIGEST_API="$digest_api" \
  TEST_UPLOAD_ID=../../unexpected \
  "$root/deploy/arcturus-oci-publish.sh" crownfi "$revision" web=local-web api=local-api \
  >/dev/null 2>&1; then
  echo 'publisher accepted a non-UUID upload ID' >&2
  exit 1
fi

if env PATH="$stubs:$PATH" ARCTURUS_URL='https://user@registry.example.ts.net' \
  ARCTURUS_CONTROL_TOKEN=control-token TEST_BUILDAH_LOG="$log" \
  TEST_REVISION="$revision" TEST_DIGEST_WEB="$digest_web" TEST_DIGEST_API="$digest_api" \
  "$root/deploy/arcturus-oci-publish.sh" crownfi "$revision" web=local-web api=local-api \
  >/dev/null 2>&1; then
  echo 'publisher accepted an HTTPS origin containing user information' >&2
  exit 1
fi

echo 'OCI publisher tests passed.'
