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
  python3 - "${data#@}" "$output" <<'PY'
import json, os, sys
request = json.load(open(sys.argv[1], encoding='utf-8'))
assert request['service'] == 'crownfi'
assert request['revision'] == 'a' * 40
assert request['components'] == ['web', 'api']
value = {
  'uploadId': os.environ.get('TEST_UPLOAD_ID', '123e4567-e89b-42d3-a456-426614174000'),
  'registry': os.environ.get('TEST_GRANT_REGISTRY', 'registry.example.ts.net'),
  'repositories': {'web': 'crownfi/web', 'api': 'crownfi/api'},
  'expiresAt': '2026-07-17T00:00:00Z',
  'credential': {'username': 'upload-user', 'secret': 'upload-secret'},
}
json.dump(value, open(sys.argv[2], 'w', encoding='utf-8'))
PY
  printf 201
elif [[ "$url" == */v1/artifact-uploads/123e4567-e89b-42d3-a456-426614174000/complete ]]; then
  [[ "$max_time" == "${TEST_EXPECT_COMPLETION_TIMEOUT:-600}" ]]
  python3 - "${data#@}" "$output" <<'PY'
import json, os, sys
request = json.load(open(sys.argv[1], encoding='utf-8'))
assert request == {'components': {
  'api': {'digest': os.environ['TEST_DIGEST_API']},
  'web': {'digest': os.environ['TEST_DIGEST_WEB']},
}}
json.dump({'uploadId': os.environ.get('TEST_UPLOAD_ID', '123e4567-e89b-42d3-a456-426614174000'), 'status': 'accepted', 'receipts': []}, open(sys.argv[2], 'w', encoding='utf-8'))
PY
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

output="$(env PATH="$stubs:$PATH" \
  ARCTURUS_URL=https://registry.example.ts.net \
  ARCTURUS_CONTROL_TOKEN=control-token \
  TEST_BUILDAH_LOG="$log" TEST_REVISION="$revision" \
  TEST_DIGEST_WEB="$digest_web" TEST_DIGEST_API="$digest_api" \
  "$root/deploy/arcturus-oci-publish.sh" crownfi "$revision" web=local-web api=local-api)"

grep -Fq '"status": "accepted"' <<<"$output"
grep -Fq 'login --authfile' "$log"
grep -Fq 'push --authfile' "$log"
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
