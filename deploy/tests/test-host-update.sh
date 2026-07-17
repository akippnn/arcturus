#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
workspace="$(mktemp -d)"
trap 'rm -rf "$workspace"' EXIT
export HOME="$workspace/home"
export ARCTURUS_CONFIG_DIR="$HOME/.config/arcturus"
export ARCTURUS_STATE_DIR="$HOME/.local/share/arcturus-deployer"
export ARCTURUS_BIN_DIR="$HOME/.local/bin"
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
  --bundle "$bundle1" --host-user appsvc --network internal_routing --allowed-bind-root /srv/apps \
  --oci-registry-image "$oci_image" --oci-registry-port 9443 \
  --oci-registry-storage /srv/arcturus-registry --enable-oci-auth \
  --oci-registry-host arcturus.tailnet.ts.net --oci-tailscale-service svc:arcturus-oci
[[ -x "$HOME/.local/bin/arcturus-host-update" ]]
grep -q "$bundle1" "$ARCTURUS_CONFIG_DIR/host-install.json"
python3 - "$ARCTURUS_CONFIG_DIR/host-install.json" <<'PY'
import json, sys
state = json.load(open(sys.argv[1]))
assert state['installArgs'] == [
    '--host-user', 'appsvc', '--network', 'internal_routing',
    '--allowed-bind-root', '/srv/apps',
    '--oci-registry-image',
    'registry.example.org/distribution/distribution@sha256:' + 'c' * 64,
    '--oci-registry-port', '9443',
    '--oci-registry-storage', '/srv/arcturus-registry',
    '--enable-oci-auth',
    '--oci-registry-host', 'arcturus.tailnet.ts.net',
    '--oci-tailscale-service', 'svc:arcturus-oci',
]
PY

"$HOME/.local/bin/arcturus-host-update" apply \
  --installer "$workspace/deploy2/install-host.sh" --bundle "$bundle2"
grep -q "$bundle2" "$ARCTURUS_CONFIG_DIR/host-install.json"
python3 - "$log.2" <<'PY'
from pathlib import Path
import sys
args = Path(sys.argv[1]).read_bytes().split(b'\0')
assert b'--host-user' in args and b'appsvc' in args
assert b'--bundle' in args
assert b'--oci-registry-image' in args
assert b'--oci-registry-port' in args and b'9443' in args
assert b'--oci-registry-storage' in args and b'/srv/arcturus-registry' in args
assert b'--enable-oci-auth' in args
assert b'--oci-registry-host' in args and b'arcturus.tailnet.ts.net' in args
assert b'--oci-tailscale-service' in args and b'svc:arcturus-oci' in args
PY
[[ "$(wc -l < "$ARCTURUS_STATE_DIR/host-install-history.jsonl")" -eq 2 ]]
"$HOME/.local/bin/arcturus-host-update" show | grep -q '<new-image@sha256:digest>'

echo 'Host updater tests passed.'
