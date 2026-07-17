#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: arcturus-oci-publish.sh SERVICE REVISION COMPONENT=LOCAL_IMAGE [...]

Required environment:
  ARCTURUS_URL             Private Arcturus HTTPS origin, for example https://registry.tailnet.ts.net
  ARCTURUS_CONTROL_TOKEN   Service-scoped Arcturus control-plane token

The local image config must contain org.opencontainers.image.revision=REVISION.
The script requests a short-lived grant, pushes each image with Buildah, asks
Rust to verify every manifest/blob, and prints the immutable receipt response.
USAGE
}

(($# >= 3)) || { usage >&2; exit 2; }
service="$1"
revision="$2"
shift 2
: "${ARCTURUS_URL:?ARCTURUS_URL is required}"
: "${ARCTURUS_CONTROL_TOKEN:?ARCTURUS_CONTROL_TOKEN is required}"

[[ "$service" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || {
  echo "service must be a lowercase DNS-style name" >&2
  exit 2
}
[[ "$revision" =~ ^[0-9a-fA-F]{40}$ ]] || {
  echo "revision must be a 40-character Git SHA" >&2
  exit 2
}
revision="${revision,,}"
[[ "$ARCTURUS_URL" =~ ^https://[a-z0-9][a-z0-9.-]*(:[0-9]+)?/?$ ]] || {
  echo "ARCTURUS_URL must be a lowercase HTTPS hostname with optional port and no path" >&2
  exit 2
}
ARCTURUS_URL="${ARCTURUS_URL%/}"
expected_registry="${ARCTURUS_URL#https://}"
for command in curl buildah python3; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "missing prerequisite: $command" >&2
    exit 2
  }
done

buildah_cmd=(buildah)
if [[ -n "${ARCTURUS_BUILDAH_ROOT:-}" ]]; then
  [[ "$ARCTURUS_BUILDAH_ROOT" == /* ]] || { echo "ARCTURUS_BUILDAH_ROOT must be absolute" >&2; exit 2; }
  buildah_cmd+=(--root "$ARCTURUS_BUILDAH_ROOT")
fi
if [[ -n "${ARCTURUS_BUILDAH_RUNROOT:-}" ]]; then
  [[ "$ARCTURUS_BUILDAH_RUNROOT" == /* ]] || { echo "ARCTURUS_BUILDAH_RUNROOT must be absolute" >&2; exit 2; }
  buildah_cmd+=(--runroot "$ARCTURUS_BUILDAH_RUNROOT")
fi

workdir="$(mktemp -d)"
authfile="$workdir/auth.json"
curl_config="$workdir/curl.conf"
cleanup() {
  rm -rf "$workdir"
}
trap cleanup EXIT
umask 077
ARCTURUS_CONTROL_TOKEN="$ARCTURUS_CONTROL_TOKEN" python3 - >"$curl_config" <<'PY'
import os

token = os.environ["ARCTURUS_CONTROL_TOKEN"]
if any(character in token for character in ("\r", "\n", "\x00")):
    raise SystemExit("ARCTURUS_CONTROL_TOKEN contains a forbidden control character")
escaped = token.replace("\\", "\\\\").replace('\"', '\\\"')
print(f'header = "Authorization: Bearer {escaped}"')
PY
unset ARCTURUS_CONTROL_TOKEN

components=()
images=()
declare -A seen=()
for mapping in "$@"; do
  component="${mapping%%=*}"
  image="${mapping#*=}"
  if [[ "$mapping" != *=* || -z "$image" || ! "$component" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
    echo "invalid component mapping: $mapping" >&2
    exit 2
  fi
  [[ -z "${seen[$component]:-}" ]] || {
    echo "duplicate component: $component" >&2
    exit 2
  }
  seen[$component]=1
  components+=("$component")
  images+=("$image")
done
((${#components[@]} <= 32)) || {
  echo "no more than 32 components may be uploaded at once" >&2
  exit 2
}

request="$workdir/request.json"
python3 - "$service" "$revision" "${components[@]}" >"$request" <<'PY'
import json, sys
service, revision, *components = sys.argv[1:]
json.dump({"service": service, "revision": revision, "components": components}, sys.stdout)
PY

grant="$workdir/grant.json"
status="$(curl --silent --show-error --connect-timeout 10 --max-time 60 \
  --output "$grant" --write-out '%{http_code}' \
  --config "$curl_config" --header 'Content-Type: application/json' \
  --data-binary "@$request" --request POST "$ARCTURUS_URL/v1/artifact-uploads")"
[[ "$status" == 201 ]] || {
  echo "upload grant request failed with HTTP $status" >&2
  cat "$grant" >&2 || true
  exit 1
}

readarray -t grant_fields < <(python3 - "$grant" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
print(value["uploadId"])
print(value["registry"])
print(value["credential"]["username"])
print(value["credential"]["secret"])
for component, repository in sorted(value["repositories"].items()):
    print(f"{component}\t{repository}")
PY
)
((${#grant_fields[@]} >= 4)) || { echo "upload grant response is incomplete" >&2; exit 1; }
upload_id="${grant_fields[0]}"
registry="${grant_fields[1]}"
username="${grant_fields[2]}"
secret="${grant_fields[3]}"
[[ "$upload_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] || {
  echo "upload grant returned an invalid upload ID" >&2
  exit 1
}
[[ "$registry" == "$expected_registry" ]] || {
  echo "upload grant registry does not match ARCTURUS_URL" >&2
  exit 1
}
printf '%s' "$secret" | "${buildah_cmd[@]}" login --authfile "$authfile" --username "$username" --password-stdin "$registry" >/dev/null

completion_pairs=()
for index in "${!components[@]}"; do
  component="${components[$index]}"
  image="${images[$index]}"
  repository=""
  for field in "${grant_fields[@]:4}"; do
    [[ "${field%%$'\t'*}" == "$component" ]] && repository="${field#*$'\t'}"
  done
  [[ "$repository" == "$service/$component" ]] || {
    echo "grant repository mismatch for $component" >&2
    exit 1
  }

  inspect="$workdir/inspect-$index.json"
  "${buildah_cmd[@]}" inspect --type image "$image" >"$inspect"
  image_revision="$(python3 -c '
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
labels = {}
for path in (("Docker", "config", "Labels"), ("Docker", "Config", "Labels"), ("OCIv1", "config", "Labels"), ("OCIv1", "Config", "Labels")):
    node = value
    for key in path:
        if not isinstance(node, dict):
            node = None
            break
        node = node.get(key)
    if isinstance(node, dict):
        labels.update(node)
print(labels.get("org.opencontainers.image.revision", ""))
' "$inspect")"
  [[ "${image_revision,,}" == "$revision" ]] || {
    echo "image $image revision label does not match $revision" >&2
    exit 1
  }

  digest_file="$workdir/digest-$index"
  "${buildah_cmd[@]}" push --authfile "$authfile" --digestfile "$digest_file" \
    "$image" "docker://$registry/$repository:upload-$upload_id" >/dev/null
  digest="$(tr -d '[:space:]' <"$digest_file")"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    echo "Buildah returned an invalid manifest digest for $component" >&2
    exit 1
  }
  completion_pairs+=("$component=$digest")
done

completion="$workdir/completion.json"
python3 - "${completion_pairs[@]}" >"$completion" <<'PY'
import json, sys
components = {}
for pair in sys.argv[1:]:
    component, digest = pair.split("=", 1)
    components[component] = {"digest": digest}
json.dump({"components": components}, sys.stdout, sort_keys=True)
PY

response="$workdir/receipt.json"
# Rust bounds verification queueing and work separately. Allow the full
# server-side budget plus transport and persistence overhead without leaving a
# CI job hanging indefinitely.
completion_timeout=600
status="$(curl --silent --show-error --connect-timeout 10 --max-time "$completion_timeout" \
  --output "$response" --write-out '%{http_code}' \
  --config "$curl_config" --header 'Content-Type: application/json' \
  --data-binary "@$completion" --request POST \
  "$ARCTURUS_URL/v1/artifact-uploads/$upload_id/complete")"
[[ "$status" == 200 || "$status" == 201 ]] || {
  echo "artifact completion failed with HTTP $status" >&2
  cat "$response" >&2 || true
  exit 1
}
python3 - "$response" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
if value.get("status") != "accepted" or not isinstance(value.get("receipts"), list):
    raise SystemExit("artifact completion response is invalid")
for receipt in value["receipts"]:
    component = receipt.get("component")
    digest = receipt.get("manifestDigest")
    if not isinstance(component, str) or not isinstance(digest, str):
        raise SystemExit("artifact receipt is incomplete")
PY
if [[ -n "${ARCTURUS_OCI_RECEIPT_FILE:-}" ]]; then
  mkdir -p "$(dirname "$ARCTURUS_OCI_RECEIPT_FILE")"
  install -m 0600 "$response" "$ARCTURUS_OCI_RECEIPT_FILE"
fi
if [[ -n "${ARCTURUS_OCI_DIGEST_DIR:-}" ]]; then
  mkdir -p "$ARCTURUS_OCI_DIGEST_DIR"
  python3 - "$response" "$ARCTURUS_OCI_DIGEST_DIR" <<'PY'
import json, os, pathlib, re, sys, tempfile
value = json.load(open(sys.argv[1], encoding="utf-8"))
out = pathlib.Path(sys.argv[2])
for receipt in value["receipts"]:
    component = receipt["component"]
    digest = receipt["manifestDigest"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", component):
        raise SystemExit("invalid component in artifact receipt")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise SystemExit("invalid digest in artifact receipt")
    fd, temporary = tempfile.mkstemp(dir=out, prefix=f".{component}.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(digest + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, out / f"{component}.digest")
PY
fi
cat "$response"
printf '\n'
