#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
workspace="$(mktemp -d)"
trap 'rm -rf "$workspace"' EXIT
export HOME="$workspace/home"
config_root="$workspace/config-root"
data_root="$workspace/data-root"
cache_root="$workspace/cache-root"
runtime_root="$workspace/runtime-root"
bin_dir="$workspace/bin-root"
workload_root="$workspace/workloads"
config_dir="$config_root/arcturus"
state_dir="$data_root/arcturus-deployer"
mkdir -p "$HOME" "$workspace/deploy1" "$workspace/deploy2"
log="$workspace/installer.log"

for n in 1 2; do
  cat > "$workspace/deploy$n/install-host.sh" <<STUB
#!/usr/bin/env bash
set -euo pipefail
printf '%s\\0' "\$@" > "$log.$n"
STUB
  chmod +x "$workspace/deploy$n/install-host.sh"
done

bundle1='registry.example.org/platform/arcturus@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
bundle2='registry.example.org/platform/arcturus@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
oci_image='registry.example.org/distribution/distribution@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'

"$root/deploy/arcturus-host-update" bootstrap --installer "$workspace/deploy1/install-host.sh" \
  --bundle "$bundle1" --host-user appsvc \
  --config-root "$config_root" --data-root "$data_root" --cache-root "$cache_root" \
  --runtime-root "$runtime_root" --bin-dir "$bin_dir" --workload-root "$workload_root" \
  --network internal_routing --allowed-bind-root /srv/apps \
  --oci-registry-image "$oci_image" --oci-registry-port 9443 \
  --oci-registry-storage /srv/arcturus-registry --enable-oci-auth \
  --oci-registry-host arcturus.tailnet.ts.net --oci-tailscale-service svc:arcturus-oci \
  --enable-fleet-control-plane --fleet-listen-address 127.0.0.1 \
  --enable-worker-agent --control-plane-url https://fleet.example:9190 \
  --worker-id worker-a --worker-token-file /srv/worker-a.token
[[ -x "$bin_dir/arcturus-host-update" ]]
[[ -x "$bin_dir/arcturus_paths.py" ]]
[[ -f "$bin_dir/arcturus-layout.json" ]]
grep -q "$bundle1" "$config_dir/host-install.json"
python3 - "$config_dir/host-install.json" <<PY
import json, sys
state = json.load(open(sys.argv[1]))
assert state['installArgs'] == [
    '--host-user', 'appsvc',
    '--config-root', '$config_root', '--data-root', '$data_root',
    '--cache-root', '$cache_root', '--runtime-root', '$runtime_root',
    '--bin-dir', '$bin_dir', '--workload-root', '$workload_root',
    '--network', 'internal_routing',
    '--allowed-bind-root', '/srv/apps',
    '--oci-registry-image',
    'registry.example.org/distribution/distribution@sha256:' + 'c' * 64,
    '--oci-registry-port', '9443',
    '--oci-registry-storage', '/srv/arcturus-registry',
    '--enable-oci-auth',
    '--oci-registry-host', 'arcturus.tailnet.ts.net',
    '--oci-tailscale-service', 'svc:arcturus-oci',
    '--enable-fleet-control-plane', '--fleet-listen-address', '127.0.0.1',
    '--enable-worker-agent', '--control-plane-url', 'https://fleet.example:9190',
    '--worker-id', 'worker-a', '--worker-token-file', '/srv/worker-a.token',
]
PY

env -u ARCTURUS_CONFIG_ROOT -u ARCTURUS_DATA_ROOT -u XDG_CONFIG_HOME -u XDG_DATA_HOME \
  "$bin_dir/arcturus-host-update" apply \
  --installer "$workspace/deploy2/install-host.sh" --bundle "$bundle2"
grep -q "$bundle2" "$config_dir/host-install.json"
python3 - "$log.2" "$config_root" "$data_root" <<'PY'
from pathlib import Path
import sys
args = Path(sys.argv[1]).read_bytes().split(b'\0')
config_root = sys.argv[2].encode()
data_root = sys.argv[3].encode()
assert b'--host-user' in args and b'appsvc' in args
assert b'--bundle' in args
assert b'--oci-registry-image' in args
assert b'--oci-registry-port' in args and b'9443' in args
assert b'--oci-registry-storage' in args and b'/srv/arcturus-registry' in args
assert b'--enable-oci-auth' in args
assert b'--oci-registry-host' in args and b'arcturus.tailnet.ts.net' in args
assert b'--oci-tailscale-service' in args and b'svc:arcturus-oci' in args
assert b'--config-root' in args and config_root in args
assert b'--data-root' in args and data_root in args
assert b'--enable-fleet-control-plane' in args
assert b'--enable-worker-agent' in args
assert b'--worker-id' in args and b'worker-a' in args
PY
[[ "$(wc -l < "$state_dir/host-install-history.jsonl")" -eq 2 ]]
"$bin_dir/arcturus-host-update" show | grep -q '<new-image@sha256:digest>'

new_config_root="$workspace/new-config-root"
new_data_root="$workspace/new-data-root"
if "$bin_dir/arcturus-host-update" bootstrap \
  --installer "$workspace/deploy2/install-host.sh" --bundle "$bundle2" \
  --config-root "$new_config_root" --data-root "$new_data_root" \
  >"$workspace/path-move.stdout" 2>"$workspace/path-move.stderr"; then
  echo 'updater silently abandoned its recorded layout during a custom-root move' >&2
  exit 1
fi
grep -q 'Refusing to abandon the recorded Arcturus configuration' "$workspace/path-move.stderr"
[[ ! -e "$new_config_root/arcturus/host-install.json" ]]

echo 'Host updater tests passed.'
