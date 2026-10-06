#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
workspace="$(mktemp -d)"
trap 'rm -rf "$workspace"' EXIT
home="$workspace/home"
source_root="$workspace/source"
source_dir="$source_root/deploy"
vhosts="$workspace/vhosts"
stubs="$workspace/bin"
mkdir -p "$home" "$source_dir" "$vhosts" "$stubs"

for file in app.py image_policy_app.py release.py arcturusctl.py requirements.txt \
  arcturus-deployer@.service arcturus-podman-api.service arcturus-bus.service \
  arcturus-registry.service arcturus-router.service arcturusd.service arcturus-agent.service arcturusctl; do
  : >"$source_dir/$file"
done
cp "$root/deploy/arcturus_paths.py" "$source_dir/arcturus_paths.py"
cp "$root/deploy/render-oci-registry-quadlet.sh" "$source_dir/render-oci-registry-quadlet.sh"
cp "$root/deploy/configure-oci-tailnet-ingress.sh" "$source_dir/configure-oci-tailnet-ingress.sh"
cp "$root/deploy/arcturus-oci-publish.sh" "$source_dir/arcturus-oci-publish.sh"
printf '#!/usr/bin/env bash\nexit 0\n' >"$source_dir/arcturusd"
printf '#!/usr/bin/env bash\nexit 0\n' >"$source_dir/arcturus-agent"
chmod +x "$source_dir/render-oci-registry-quadlet.sh" "$source_dir/configure-oci-tailnet-ingress.sh" \
  "$source_dir/arcturus-oci-publish.sh" "$source_dir/arcturusd" "$source_dir/arcturus-agent"
for module in bus registry router; do
  mkdir -p "$source_root/modules/$module/dist"
  : >"$source_root/modules/$module/dist/index.js"
done

real_python="$(command -v python3.12 || command -v python3)"
cat >"$stubs/python" <<STUB
#!/usr/bin/env bash
if [[ "\${1:-}" == -c ]]; then
  exit 0
fi
exec "$real_python" "\$@"
STUB
cat >"$stubs/node" <<'STUB'
#!/usr/bin/env bash
if [[ "${1:-}" == -p ]]; then
  echo 22
else
  exit 0
fi
STUB
cat >"$stubs/podman" <<'STUB'
#!/usr/bin/env bash
if [[ "${1:-}" == --version ]]; then
  echo 'podman version 5.8.0'
fi
STUB
cat >"$stubs/systemd" <<'STUB'
#!/usr/bin/env bash
echo 'systemd 252 (fixture)'
STUB
cat >"$stubs/tailscale" <<'STUB'
#!/usr/bin/env bash
if [[ "${1:-}" == version ]]; then
  echo '1.94.0'
fi
STUB
for command in systemctl systemd-analyze getent curl; do
  cat >"$stubs/$command" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
done
cat >"$workspace/podman-system-generator" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$stubs"/* "$workspace/podman-system-generator"

common=(
  --source-dir "$source_dir"
  --host-home "$home"
  --vhosts-dir "$vhosts"
  --validate-only
)
digest="sha256:$(printf 'd%.0s' {1..64})"
image="registry.example.org/distribution/distribution@$digest"

run_installer() {
  env PATH="$stubs:$PATH" ARCTURUS_PYTHON="$stubs/python" \
    ARCTURUS_QUADLET_GENERATOR="$workspace/podman-system-generator" \
    "$root/deploy/install-host.sh" "${common[@]}" "$@"
}

run_installer \
  --oci-registry-image "$image" --oci-registry-port 9443 \
  --oci-registry-storage "$home/registry" \
  | grep -Fq 'OCI data plane:'

run_installer --enable-fleet-control-plane --fleet-listen-address 100.64.0.10 \
  | grep -Fq 'Fleet control:     enabled on 100.64.0.10:9190'
if run_installer --enable-fleet-control-plane --fleet-listen-address 0.0.0.0 \
  >/dev/null 2>&1; then
  echo 'installer accepted an unrestricted fleet listener' >&2
  exit 1
fi
if run_installer --enable-worker-agent >/dev/null 2>&1; then
  echo 'installer accepted a worker agent without identity or credentials' >&2
  exit 1
fi
worker_token="$workspace/worker.token"
printf 'fixture-worker-credential\n' >"$worker_token"
chmod 0600 "$worker_token"
run_installer --enable-worker-agent --worker-id worker-a \
  --control-plane-url http://192.0.2.10:9190 --worker-token-file "$worker_token" \
  | grep -Fq 'Worker agent:      enabled as worker-a -> http://192.0.2.10:9190'

run_installer --config-root "$workspace/xdg-config" --data-root "$workspace/xdg-data" \
  --cache-root "$workspace/xdg-cache" --runtime-root "$workspace/xdg-runtime" \
  --bin-dir "$workspace/bin-root" --workload-root "$workspace/workloads" \
  | grep -Fq "configuration:     $workspace/xdg-config/arcturus"
run_installer --config-root "$workspace/xdg-config" --data-root "$workspace/xdg-data" \
  --cache-root "$workspace/xdg-cache" --runtime-root "$workspace/xdg-runtime" \
  --bin-dir "$workspace/bin-root" --workload-root "$workspace/workloads" \
  | grep -Fq "state:             $workspace/xdg-data/arcturus-deployer"

local_version_before="$(run_installer | awk '/  version:/ {print $2}')"
printf 'export const changed = true;\n' >>"$source_root/modules/router/dist/index.js"
local_version_after="$(run_installer | awk '/  version:/ {print $2}')"
[[ "$local_version_before" == local-* && "$local_version_after" == local-* \
  && "$local_version_before" != "$local_version_after" ]] || {
  echo 'source install version did not change with compiled module output' >&2
  exit 1
}

if run_installer \
  --oci-registry-image registry.example.org/distribution/distribution:latest \
  >/dev/null 2>&1; then
  echo 'installer accepted an unpinned OCI registry image' >&2
  exit 1
fi

if run_installer --oci-registry-image "$image" --oci-registry-port 9090 \
  >/dev/null 2>&1; then
  echo 'installer accepted an OCI port conflicting with the API' >&2
  exit 1
fi

if run_installer --enable-oci-auth >/dev/null 2>&1; then
  echo 'installer enabled OCI authorization without a registry' >&2
  exit 1
fi

if run_installer --oci-registry-image "$image" --oci-registry-port 9190 \
  --enable-oci-auth >/dev/null 2>&1; then
  echo 'installer accepted an OCI port conflicting with Rust authorization' >&2
  exit 1
fi

run_installer \
  --oci-registry-image "$image" --oci-registry-port 9443 \
  --oci-registry-storage "$home/registry" --enable-oci-auth \
  | grep -Fq 'OCI authorization: enabled'

if run_installer --oci-registry-image "$image" --enable-oci-auth \
  --oci-registry-host registry.tailnet.ts.net >/dev/null 2>&1; then
  echo 'installer accepted an OCI hostname without a Tailscale Service' >&2
  exit 1
fi

if run_installer --oci-registry-image "$image" \
  --oci-registry-host registry.tailnet.ts.net \
  --oci-tailscale-service svc:arcturus-oci >/dev/null 2>&1; then
  echo 'installer accepted tailnet ingress without OCI authorization' >&2
  exit 1
fi

run_installer --oci-registry-image "$image" --enable-oci-auth \
  --oci-registry-host registry.tailnet.ts.net \
  --oci-tailscale-service svc:arcturus-oci \
  | grep -Fq 'OCI write ingress: enabled-after-validation'

mkdir -p "$home/.config/arcturus"
cat >"$home/.config/arcturus/oci-registry.env" <<CONFIG
ARCTURUS_OCI_REGISTRY_IMAGE=$image
ARCTURUS_OCI_REGISTRY_PORT=9555
ARCTURUS_OCI_REGISTRY_STORAGE=$home/existing-registry
ARCTURUS_OCI_AUTH_ENABLED=true
ARCTURUS_OCI_REGISTRY_HOST=registry.example.ts.net
ARCTURUS_OCI_TAILSCALE_SERVICE=svc:old-arcturus-oci
CONFIG
if run_installer --config-root "$workspace/moved-config" >/dev/null 2>&1; then
  echo 'installer silently abandoned legacy configuration after a root change' >&2
  exit 1
fi
run_installer | grep -Fq '127.0.0.1:9555'
run_installer | grep -Fq 'OCI authorization: enabled'
run_installer --disable-oci-auth | grep -Fq 'OCI authorization: disabled'
run_installer --disable-oci-auth | grep -Fq 'OCI private HTTPS: disabled'
run_installer --disable-oci-auth | grep -Fq 'OCI write ingress: read-only'
run_installer --enable-oci-auth \
  --oci-registry-host registry-new.example.ts.net \
  --oci-tailscale-service svc:new-arcturus-oci \
  | grep -Fq 'via svc:new-arcturus-oci'
grep -Fq 'REGISTRY_STORAGE_MAINTENANCE_READONLY_ENABLED=true' \
  "$root/deploy/install-host.sh"
grep -Fq 'oci_unlock_guard_armed=true' \
  "$root/deploy/install-host.sh"
unlock_line="$(grep -n 'oci_unlock_guard_armed=true' "$root/deploy/install-host.sh" | tail -n1 | cut -d: -f1)"
disarm_line="$(grep -n '^oci_unlock_guard_armed=false$' "$root/deploy/install-host.sh" | tail -n1 | cut -d: -f1)"
deployer_restart_line="$(grep -nF 'systemctl --user restart "${deployer_units[@]}"' "$root/deploy/install-host.sh" | cut -d: -f1)"
((unlock_line < deployer_restart_line && deployer_restart_line < disarm_line)) || {
  echo 'OCI write rollback guard is disarmed before installation completes' >&2
  exit 1
}
grep -Fq 'set_oci_registry_read_only true' \
  "$root/deploy/install-host.sh"
grep -Fq 'REGISTRY_STORAGE_DELETE_ENABLED=false' \
  "$root/deploy/install-host.sh"

grep -Fq 'REGISTRY_AUTH_TOKEN_JWKS=/etc/distribution/oci-jwks.json' \
  "$root/deploy/install-host.sh"
grep -Fq 'REGISTRY_AUTH_TOKEN_SIGNINGALGORITHMS_0=EdDSA' \
  "$root/deploy/install-host.sh"
grep -Fq 'ARCTURUSD_UPLOAD_AUTH_ENABLED=true' \
  "$root/deploy/install-host.sh"
grep -Fq 'ARCTURUS_OCI_RECEIPT_DB=' \
  "$root/deploy/install-host.sh"
grep -Fq 'ARCTURUS_LEGACY_ALLOW_MUTABLE_MAIN=' \
  "$root/deploy/install-host.sh"
grep -Fq -- '--allow-legacy-mutable-main' \
  "$root/deploy/install-host.sh"
grep -Fq 'configure-oci-tailnet-ingress.sh' \
  "$root/deploy/install-host.sh"
grep -Fq 'tailscale serve clear "$OCI_TAILSCALE_SERVICE_TO_CLEAR"' \
  "$root/deploy/install-host.sh"
grep -Fq 'OCI_TAILSCALE_SERVICE_TO_CLEAR="$existing_oci_service"' \
  "$root/deploy/install-host.sh"
grep -Fq '"$OCI_TAILSCALE_SERVICE_TO_CLEAR" != "$OCI_TAILSCALE_SERVICE"' \
  "$root/deploy/install-host.sh"
grep -Fq 'OCI_AUTH_STATE_DIR="$ARCTURUS_PATH_OCI_AUTH_STATE_DIR"' \
  "$root/deploy/install-host.sh"
grep -Fq 'ReadWritePaths="@OCI_AUTH_STATE_DIR@"' \
  "$root/deploy/arcturusd.service"
! grep -Fq 'ReadWritePaths=%h/' \
  "$root/deploy/arcturusd.service"

echo 'OCI registry installer validation tests passed.'
