#!/usr/bin/env bash
set -euo pipefail

image="${1:?usage: render-oci-registry-quadlet.sh IMAGE@DIGEST PORT STORAGE_DIR RUNTIME_ENV}"
port="${2:?usage: render-oci-registry-quadlet.sh IMAGE@DIGEST PORT STORAGE_DIR RUNTIME_ENV}"
storage="${3:?usage: render-oci-registry-quadlet.sh IMAGE@DIGEST PORT STORAGE_DIR RUNTIME_ENV}"
runtime_env="${4:?usage: render-oci-registry-quadlet.sh IMAGE@DIGEST PORT STORAGE_DIR RUNTIME_ENV}"

[[ "$image" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}$ ]] || {
  echo "OCI registry image must be digest-pinned" >&2
  exit 2
}
[[ "$port" =~ ^[0-9]+$ ]] && ((port >= 1024 && port <= 65535)) || {
  echo "OCI registry port must be between 1024 and 65535" >&2
  exit 2
}
for path in "$storage" "$runtime_env"; do
  [[ "$path" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
    echo "OCI registry paths must be absolute and contain no whitespace" >&2
    exit 2
  }
done

cat <<QUADLET
[Unit]
Description=Arcturus local OCI artifact data plane
After=network-online.target
Wants=network-online.target

[Container]
Image=$image
ContainerName=arcturus-oci-registry
Pull=never
PublishPort=127.0.0.1:$port:5000
Volume=$storage:/var/lib/registry:Z
EnvironmentFile=$runtime_env
Label=io.u128.arcturus.role=oci-ingress

[Service]
Restart=on-failure
RestartSec=3s
TimeoutStartSec=120

[Install]
WantedBy=default.target
QUADLET
