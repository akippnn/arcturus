#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install-host.sh [options]

  --source-dir DIR            Install from a local Arcturus deploy directory
  --bundle IMAGE@DIGEST       Extract a versioned OCI bundle (local source overrides it)
  --version VERSION           Installed release name (auto-detected by default)
  --host-user USER            Rootless service account (default: current user)
  --host-home DIR             Override resolved home (primarily fixtures/images)
  --listen-address ADDRESS    Additional private API listener; never 0.0.0.0 or ::
  --runner-cidr CIDR          Trusted CI source for optional firewalld rule
  --allowed-bind-root DIR     Repeatable bind-mount allowlist
  --network NAME              External Podman network (default: internal_routing)
  --vhosts-dir DIR           Portal nginx generated-vhost directory
  --nginx-container NAME     Portal nginx container (default: portal-nginx)
  --base-domain DOMAIN       Router-managed base domain
  --cert-domain DOMAIN       TLS certificate domain (default: base domain)
  --container-cli podman     Container CLI used by the router
  --configure-firewall        Add a source-scoped firewalld rule for port 9090
  --validate-only             Validate inputs and prerequisites without writing
  --dry-run                   Print the resolved installation without writing
  --force-config              Replace deployer.env after making a backup
EOF
}

SOURCE_DIR=""
BUNDLE=""
VERSION=""
HOST_USER="${USER:-$(id -un)}"
HOST_HOME_OVERRIDE="${ARCTURUS_HOST_HOME:-}"
LISTEN_ADDRESS=""
RUNNER_CIDR=""
NETWORK="internal_routing"
VHOSTS_DIR=""
NGINX_CONTAINER="portal-nginx"
BASE_DOMAIN=""
CERT_DOMAIN=""
CONTAINER_CLI="podman"
CONFIGURE_FIREWALL=false
VALIDATE_ONLY=false
DRY_RUN=false
FORCE_CONFIG=false
ALLOWED_BIND_ROOTS=()

while (($#)); do
  case "$1" in
    --source-dir) SOURCE_DIR="$2"; shift 2 ;;
    --bundle) BUNDLE="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --host-user) HOST_USER="$2"; shift 2 ;;
    --host-home) HOST_HOME_OVERRIDE="$2"; shift 2 ;;
    --listen-address) LISTEN_ADDRESS="$2"; shift 2 ;;
    --runner-cidr) RUNNER_CIDR="$2"; shift 2 ;;
    --allowed-bind-root) ALLOWED_BIND_ROOTS+=("$2"); shift 2 ;;
    --network) NETWORK="$2"; shift 2 ;;
    --vhosts-dir) VHOSTS_DIR="$2"; shift 2 ;;
    --nginx-container) NGINX_CONTAINER="$2"; shift 2 ;;
    --base-domain) BASE_DOMAIN="$2"; shift 2 ;;
    --cert-domain) CERT_DOMAIN="$2"; shift 2 ;;
    --container-cli) CONTAINER_CLI="$2"; shift 2 ;;
    --configure-firewall) CONFIGURE_FIREWALL=true; shift ;;
    --validate-only) VALIDATE_ONLY=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    --force-config) FORCE_CONFIG=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

errors=()
PYTHON_BIN="${ARCTURUS_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  command -v python3.12 >/dev/null 2>&1 && PYTHON_BIN=python3.12 || PYTHON_BIN=python3
fi
for command in bash "$PYTHON_BIN" node podman systemctl systemd-analyze sha256sum getent; do
  command -v "$command" >/dev/null 2>&1 || errors+=("missing prerequisite: $command")
done
if command -v "$PYTHON_BIN" >/dev/null 2>&1 && ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
  errors+=("Python 3.12 or newer is required (selected: $PYTHON_BIN)")
fi
if command -v node >/dev/null 2>&1 && [[ "$(node -p 'Number(process.versions.node.split(".")[0])')" -lt 22 ]]; then
  errors+=("Node.js 22 or newer is required")
fi
if command -v podman >/dev/null 2>&1; then
  podman_version="$(rpm -q --qf '%{VERSION}' podman 2>/dev/null || true)"
  [[ -n "$podman_version" ]] || podman_version="$(podman --version 2>/dev/null | awk '{print $3}')"
  if ! awk -F. '{exit !($1 > 5 || ($1 == 5 && $2 >= 8))}' <<<"$podman_version"; then
    errors+=("Podman 5.8 or newer is required (found ${podman_version:-unknown})")
  fi
fi
if command -v systemd >/dev/null 2>&1 && [[ "$(systemd --version | awk 'NR==1 {print $2}')" -lt 257 ]]; then
  errors+=("systemd 257 or newer is required")
fi
[[ -x /usr/lib/systemd/system-generators/podman-system-generator ]] || errors+=("Podman Quadlet generator is missing")
[[ "$HOST_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || errors+=("invalid --host-user: $HOST_USER")
[[ -z "$HOST_HOME_OVERRIDE" || "$HOST_HOME_OVERRIDE" == /* ]] || errors+=("--host-home must be absolute")
[[ "$NETWORK" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || errors+=("invalid --network: $NETWORK")
if [[ -n "$LISTEN_ADDRESS" && "$LISTEN_ADDRESS" =~ ^(0\.0\.0\.0|::)$ ]]; then
  errors+=("unrestricted deployer listener is forbidden: $LISTEN_ADDRESS")
fi
if [[ -n "$LISTEN_ADDRESS" ]] && ! "$PYTHON_BIN" - "$LISTEN_ADDRESS" <<'PY'
import ipaddress, sys
address = ipaddress.ip_address(sys.argv[1])
tailscale_cgnat = ipaddress.ip_network("100.64.0.0/10")
allowed = address.is_private or address.is_loopback or address in tailscale_cgnat
raise SystemExit(0 if allowed else 1)
PY
then
  errors+=("--listen-address must be a private or loopback IP address")
fi
if [[ -n "$RUNNER_CIDR" ]] && ! "$PYTHON_BIN" - "$RUNNER_CIDR" <<'PY'
import ipaddress, sys
ipaddress.ip_network(sys.argv[1], strict=False)
PY
then
  errors+=("--runner-cidr must be a valid IPv4 or IPv6 network")
fi
if [[ -n "$BUNDLE" && ! "$BUNDLE" =~ @sha256:[0-9a-f]{64}$ ]]; then
  errors+=("--bundle must be pinned as repository@sha256:digest")
fi
if [[ -z "$SOURCE_DIR" && -z "$BUNDLE" ]]; then
  SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [[ -n "$SOURCE_DIR" && ! -f "$SOURCE_DIR/requirements.txt" ]]; then
  errors+=("source directory does not contain requirements.txt: $SOURCE_DIR")
fi
if [[ -n "$SOURCE_DIR" ]]; then
  for file in app.py image_policy_app.py release.py arcturusctl.py arcturus-deployer@.service \
    arcturus-podman-api.service arcturus-bus.service arcturus-registry.service \
    arcturus-router.service; do
    [[ -f "$SOURCE_DIR/$file" ]] || errors+=("source artifact is missing: $SOURCE_DIR/$file")
  done
  source_root_check="$(cd "$SOURCE_DIR/.." 2>/dev/null && pwd || true)"
  for module in bus registry router; do
    [[ -f "$source_root_check/modules/$module/dist/index.js" ]] || \
      errors+=("compiled module is missing: modules/$module/dist/index.js")
  done
fi
[[ "$CONTAINER_CLI" == podman ]] || errors+=("--container-cli must be podman")
[[ "$NGINX_CONTAINER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || errors+=("invalid --nginx-container")
if $CONFIGURE_FIREWALL && [[ -z "$RUNNER_CIDR" || -z "$LISTEN_ADDRESS" ]]; then
  errors+=("--configure-firewall requires --listen-address and --runner-cidr")
fi
if ((${#errors[@]})); then
  printf 'Arcturus host validation failed:\n' >&2
  printf '  - %s\n' "${errors[@]}" >&2
  exit 2
fi

HOST_HOME="${HOST_HOME_OVERRIDE:-$(getent passwd "$HOST_USER" | cut -d: -f6)}"
HOST_UID="$(id -u "$HOST_USER")"
if [[ "$(id -un)" != "$HOST_USER" && $VALIDATE_ONLY == false && $DRY_RUN == false ]]; then
  echo "Run this installer as rootless user '$HOST_USER' (for example: sudo -iu $HOST_USER)." >&2
  exit 2
fi

# An upgrade should preserve the installed platform configuration when the
# operator only supplies a new bundle. Read generated key/value files without
# sourcing them as shell code.
existing_deployer_config="$HOST_HOME/.config/arcturus/deployer.env"
existing_platform_config="$HOST_HOME/.config/arcturus/platform.env"
read_existing_value() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 0
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}
if ((${#ALLOWED_BIND_ROOTS[@]} == 0)); then
  existing_roots="$(read_existing_value "$existing_deployer_config" ARCTURUS_ALLOWED_BIND_ROOTS)"
  if [[ -n "$existing_roots" ]]; then
    IFS=',' read -r -a ALLOWED_BIND_ROOTS <<<"$existing_roots"
  else
    ALLOWED_BIND_ROOTS=("$HOST_HOME/stacks")
  fi
fi
[[ -n "$VHOSTS_DIR" ]] || VHOSTS_DIR="$(read_existing_value "$existing_platform_config" VHOSTS_DIR)"
[[ -n "$BASE_DOMAIN" ]] || BASE_DOMAIN="$(read_existing_value "$existing_platform_config" BASE_DOMAIN)"
[[ -n "$CERT_DOMAIN" ]] || CERT_DOMAIN="$(read_existing_value "$existing_platform_config" CERT_DOMAIN)"
if [[ "$NGINX_CONTAINER" == portal-nginx ]]; then
  existing_nginx="$(read_existing_value "$existing_platform_config" NGINX_CONTAINER)"
  [[ -z "$existing_nginx" ]] || NGINX_CONTAINER="$existing_nginx"
fi

if [[ -z "$VHOSTS_DIR" ]]; then
  for candidate in \
    "$HOST_HOME/arcturus/portal/config/nginx/vhosts.d" \
    "$HOST_HOME/stacks/portal/config/nginx/vhosts.d"; do
    if [[ -d "$candidate" ]]; then
      VHOSTS_DIR="$candidate"
      break
    fi
  done
fi
if [[ -z "$VHOSTS_DIR" || ! -d "$VHOSTS_DIR" ]]; then
  echo "Cannot locate the portal nginx generated-vhost directory. Pass --vhosts-dir with the directory mounted at /etc/nginx/vhosts.d by the portal." >&2
  exit 2
fi
CERT_DOMAIN="${CERT_DOMAIN:-$BASE_DOMAIN}"

STATE_DIR="$HOST_HOME/.local/share/arcturus-deployer"
CONFIG_DIR="$HOST_HOME/.config/arcturus"
UNIT_DIR="$HOST_HOME/.config/systemd/user"
QUADLET_DIR="$HOST_HOME/.config/containers/systemd/arcturus"
BIN_DIR="$HOST_HOME/.local/bin"
RUNTIME_DIR="/run/user/$HOST_UID/arcturus"
CONFIG_FILE="$CONFIG_DIR/deployer.env"
PLATFORM_CONFIG_FILE="$CONFIG_DIR/platform.env"

if [[ -z "$VERSION" && -n "$SOURCE_DIR" ]]; then
  VERSION="local-$(find "$SOURCE_DIR" -maxdepth 1 -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)"
fi
if [[ -z "$VERSION" ]]; then
  bundle_digest="${BUNDLE##*@sha256:}"
  VERSION="oci-${bundle_digest:0:12}"
fi

cat <<EOF
Arcturus host configuration
  user:              $HOST_USER ($HOST_UID)
  home:              $HOST_HOME
  version:           $VERSION
  source:            ${SOURCE_DIR:-$BUNDLE}
  listeners:         127.0.0.1${LISTEN_ADDRESS:+, $LISTEN_ADDRESS}
  network:           $NETWORK
  router:            ${BASE_DOMAIN:-disabled}${BASE_DOMAIN:+ via $NGINX_CONTAINER}
  state:             $STATE_DIR
  allowed bind roots: $(IFS=,; echo "${ALLOWED_BIND_ROOTS[*]}")
EOF

$VALIDATE_ONLY && exit 0
$DRY_RUN && exit 0

umask 077
mkdir -p "$STATE_DIR/releases" "$STATE_DIR/active-manifests" "$CONFIG_DIR" "$UNIT_DIR" "$QUADLET_DIR" "$BIN_DIR" "$RUNTIME_DIR"
staging="$(mktemp -d "$STATE_DIR/releases/.install.XXXXXX")"
container_id=""
rendered_config=""
cleanup() {
  [[ -z "$container_id" ]] || podman rm "$container_id" >/dev/null 2>&1 || true
  [[ ! -d "$staging" ]] || rm -rf "$staging"
  [[ -z "$rendered_config" ]] || rm -f "$rendered_config"
}
trap cleanup EXIT

if [[ -n "$SOURCE_DIR" ]]; then
  for file in app.py image_policy_app.py release.py arcturusctl.py requirements.txt \
    arcturus-deployer@.service arcturus-podman-api.service arcturus-bus.service \
    arcturus-registry.service arcturus-router.service arcturusctl; do
    install -m 0644 "$SOURCE_DIR/$file" "$staging/$file"
  done
  chmod 0755 "$staging/arcturusctl" "$staging/arcturusctl.py"
  if [[ -d "$SOURCE_DIR/wheelhouse" ]]; then
    cp -a "$SOURCE_DIR/wheelhouse" "$staging/wheelhouse"
  fi
  source_root="$(cd "$SOURCE_DIR/.." && pwd)"
  mkdir -p "$staging/modules"
  for module in bus registry router; do
    [[ -f "$source_root/modules/$module/dist/index.js" ]] || {
      echo "Compiled module missing: $source_root/modules/$module/dist/index.js" >&2
      exit 2
    }
    mkdir -p "$staging/modules/$module"
    cp -a "$source_root/modules/$module/dist" "$staging/modules/$module/dist"
    install -m 0644 "$source_root/modules/$module/package.json" "$staging/modules/$module/package.json"
  done
  if [[ -d "$source_root/modules/registry/node_modules/zod" ]]; then
    mkdir -p "$staging/modules/registry/node_modules"
    cp -a "$source_root/modules/registry/node_modules/zod" "$staging/modules/registry/node_modules/zod"
  else
    echo "Registry runtime dependency zod is missing; run npm ci and build the modules first." >&2
    exit 2
  fi
else
  podman pull "$BUNDLE" >/dev/null
  container_id="$(podman create "$BUNDLE")"
  podman cp "$container_id:/opt/arcturus/deploy/." "$staging/"
  mkdir -p "$staging/modules"
  podman cp "$container_id:/opt/arcturus/modules/." "$staging/modules/"
fi

"$PYTHON_BIN" -m venv "$staging/venv"
if [[ -d "$staging/wheelhouse" ]]; then
  "$staging/venv/bin/pip" install --disable-pip-version-check --no-index \
    --find-links "$staging/wheelhouse" --requirement "$staging/requirements.txt"
else
  "$staging/venv/bin/pip" install --disable-pip-version-check \
    --requirement "$staging/requirements.txt"
fi
release_path="$STATE_DIR/releases/$VERSION"
if [[ ! -d "$release_path" ]]; then
  mv "$staging" "$release_path"
fi
ln -sfn "$release_path" "$STATE_DIR/current.new"
mv -Tf "$STATE_DIR/current.new" "$STATE_DIR/current"

rendered_config="$(mktemp)"
cat >"$rendered_config" <<EOF
ARCTURUS_STATE_DIR=$STATE_DIR
ARCTURUS_QUADLET_DIR=$QUADLET_DIR
ARCTURUS_SYSTEMD_DIR=$UNIT_DIR
ARCTURUS_ACTIVE_MANIFEST_DIR=$STATE_DIR/active-manifests
ARCTURUS_ALLOWED_BIND_ROOTS=$(IFS=,; echo "${ALLOWED_BIND_ROOTS[*]}")
PODMAN_SOCKET=$RUNTIME_DIR/podman.sock
RUNNER_TOKENS_FILE=$CONFIG_DIR/tokens.json
ARCTURUS_ROUTER_STATUS_FILE=$RUNTIME_DIR/router-status.json
ARCTURUS_REGISTRY_SOCKET=$RUNTIME_DIR/registry.sock
EOF
if [[ ! -f "$CONFIG_FILE" || $FORCE_CONFIG == true ]]; then
  if [[ -f "$CONFIG_FILE" ]]; then
    cp -a "$CONFIG_FILE" "$CONFIG_FILE.backup.$(date -u +%Y%m%dT%H%M%SZ)"
  fi
  install -m 0600 "$rendered_config" "$CONFIG_FILE"
else
  proposed="$CONFIG_FILE.proposed.$VERSION"
  install -m 0600 "$rendered_config" "$proposed"
  echo "Preserving existing $CONFIG_FILE; proposed configuration: $proposed" >&2
fi

rendered_platform="$(mktemp)"
cat >"$rendered_platform" <<EOF
BUS_SOCKET=$RUNTIME_DIR/bus.sock
REGISTRY_SOCKET=$RUNTIME_DIR/registry.sock
ROUTER_STATUS_FILE=$RUNTIME_DIR/router-status.json
STACKS_DIR=$HOST_HOME/stacks
ACTIVE_MANIFESTS_DIR=$STATE_DIR/active-manifests
VHOSTS_DIR=$VHOSTS_DIR
NGINX_CONTAINER=$NGINX_CONTAINER
BASE_DOMAIN=$BASE_DOMAIN
CERT_DOMAIN=$CERT_DOMAIN
CONTAINER_CLI=$CONTAINER_CLI
EOF
if [[ ! -f "$PLATFORM_CONFIG_FILE" || $FORCE_CONFIG == true ]]; then
  [[ ! -f "$PLATFORM_CONFIG_FILE" ]] || cp -a "$PLATFORM_CONFIG_FILE" "$PLATFORM_CONFIG_FILE.backup.$(date -u +%Y%m%dT%H%M%SZ)"
  install -m 0600 "$rendered_platform" "$PLATFORM_CONFIG_FILE"
else
  install -m 0600 "$rendered_platform" "$PLATFORM_CONFIG_FILE.proposed.$VERSION"
  echo "Preserving existing $PLATFORM_CONFIG_FILE; proposed configuration staged." >&2
fi
rm -f "$rendered_platform"

install -m 0644 "$release_path/arcturus-deployer@.service" "$UNIT_DIR/arcturus-deployer@.service"
install -m 0644 "$release_path/arcturus-podman-api.service" "$UNIT_DIR/arcturus-podman-api.service"
install -m 0644 "$release_path/arcturus-bus.service" "$UNIT_DIR/arcturus-bus.service"
install -m 0644 "$release_path/arcturus-registry.service" "$UNIT_DIR/arcturus-registry.service"
install -m 0644 "$release_path/arcturus-router.service" "$UNIT_DIR/arcturus-router.service"
mkdir -p "$UNIT_DIR/arcturus-router.service.d"
router_paths="$(mktemp)"
cat >"$router_paths" <<EOF
[Service]
ReadWritePaths=$VHOSTS_DIR
EOF
install -m 0644 "$router_paths" "$UNIT_DIR/arcturus-router.service.d/10-vhosts.conf"
rm -f "$router_paths"
install -m 0755 "$release_path/arcturusctl" "$BIN_DIR/arcturusctl"

podman network exists "$NETWORK" || podman network create "$NETWORK" >/dev/null
if command -v loginctl >/dev/null 2>&1 && [[ "$(loginctl show-user "$HOST_USER" -p Linger --value 2>/dev/null || true)" != true ]]; then
  sudo loginctl enable-linger "$HOST_USER"
fi
systemctl --user daemon-reload

# `enable --now` does not restart an already-running unit after the `current`
# release symlink changes. Preserve every active deployer listener and restart
# the platform in dependency order so the newly installed code is actually live.
deployer_units=("arcturus-deployer@127.0.0.1.service")
while read -r active_unit _; do
  [[ -n "$active_unit" ]] || continue
  found=false
  for configured_unit in "${deployer_units[@]}"; do
    [[ "$configured_unit" == "$active_unit" ]] && found=true
  done
  $found || deployer_units+=("$active_unit")
done < <(
  systemctl --user list-units --type=service --state=active --plain --no-legend     'arcturus-deployer@*.service' 2>/dev/null || true
)
if [[ -n "$LISTEN_ADDRESS" ]]; then
  requested_unit="arcturus-deployer@$LISTEN_ADDRESS.service"
  found=false
  for configured_unit in "${deployer_units[@]}"; do
    [[ "$configured_unit" == "$requested_unit" ]] && found=true
  done
  $found || deployer_units+=("$requested_unit")
fi

systemctl --user enable arcturus-podman-api.service
systemctl --user restart arcturus-podman-api.service
systemctl --user enable arcturus-bus.service arcturus-registry.service
systemctl --user restart arcturus-bus.service arcturus-registry.service
if [[ -n "$BASE_DOMAIN" ]]; then
  systemctl --user enable arcturus-router.service
  systemctl --user restart arcturus-router.service
else
  systemctl --user disable --now arcturus-router.service >/dev/null 2>&1 || true
fi
systemctl --user enable "${deployer_units[@]}"
systemctl --user restart "${deployer_units[@]}"

if $CONFIGURE_FIREWALL; then
  firewall_family="$("$PYTHON_BIN" - "$RUNNER_CIDR" <<'PY'
import ipaddress, sys
print(f"ipv{ipaddress.ip_network(sys.argv[1], strict=False).version}")
PY
)"
  rule="rule family=$firewall_family source address=$RUNNER_CIDR port port=9090 protocol=tcp accept"
  sudo firewall-cmd --permanent --add-rich-rule="$rule"
  sudo firewall-cmd --reload
fi

echo "Arcturus $VERSION installed for $HOST_USER."
echo "API readiness: curl --fail http://127.0.0.1:9090/healthz"
echo "Create a CI token with: arcturusctl token create --database '$CONFIG_DIR/tokens.json' --service <service> --token-id <service>-ci --output '$CONFIG_DIR/<service>-ci.token'"
