#!/usr/bin/env bash
set -euo pipefail

image="${1:?usage: build-bundle.sh <repository:version> [digest-file]}"
digest_file="${2:-arcturus-bundle.digest}"
deploy_dir="$(cd "$(dirname "$0")" && pwd)"
repository_root="$(cd "$deploy_dir/.." && pwd)"
buildah bud --file "$deploy_dir/Containerfile.bundle" --tag "$image" "$repository_root"
buildah push --digestfile "$digest_file" "$image"
digest="$(tr -d '[:space:]' <"$digest_file")"
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "registry did not return a sha256 digest" >&2
  exit 1
}
printf '%s@%s\n' "${image%:*}" "$digest"
