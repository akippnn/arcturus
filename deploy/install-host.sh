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
  --legacy-v1-mode MODE       enforce (default) or audit for temporary unverified v1 routing
  --allow-legacy-nginx-extras Temporarily permit deprecated v1 nginxExtras (unsafe compatibility)
  --allow-legacy-mutable-main  Temporarily allow old /deploy apply requests without an immutable SHA
  --disallow-legacy-mutable-main Re-enable immutable-SHA enforcement for old /deploy requests
  --oci-registry-image IMAGE  Digest-pinned, supported Distribution v3 image; enables OCI storage
  --oci-registry-port PORT    Loopback OCI port (default: 9443)
  --oci-registry-storage DIR  Persistent OCI storage (default: ~/.local/share/arcturus-registry)
  --enable-oci-auth           Install Rust auth and configure registry token verification
  --disable-oci-auth          Keep local registry but disable Rust token authorization
  --oci-registry-host HOST    Advertised private HTTPS registry hostname (*.ts.net)
  --oci-tailscale-service SVC Dedicated Tailscale Service name (svc:<name>)
  --disable-oci-registry      Disable and remove the local OCI data-plane unit
  --configure-firewall        Add a source-scoped firewalld rule for port 9090
  --validate-only             Validate inputs and prerequisites without writing
  --dry-run                   Print the resolved installation without writing
  --force-config              Replace managed env files after making backups
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
LEGACY_V1_MODE=""
ALLOW_LEGACY_NGINX_EXTRAS=false
ALLOW_LEGACY_MUTABLE_MAIN=false
LEGACY_V1_MODE_SET=false
ALLOW_LEGACY_NGINX_EXTRAS_SET=false
ALLOW_LEGACY_MUTABLE_MAIN_SET=false
OCI_REGISTRY_IMAGE=""
OCI_REGISTRY_PORT="9443"
OCI_REGISTRY_PORT_SET=false
OCI_REGISTRY_STORAGE=""
OCI_AUTH_SET=false
OCI_AUTH_ENABLED=false
OCI_REGISTRY_HOST=""
OCI_TAILSCALE_SERVICE=""
OCI_TAILSCALE_SERVICE_TO_CLEAR=""
OCI_MAX_LAYER_BYTES=536870912
OCI_MAX_ARTIFACT_BYTES=805306368
OCI_MIN_FREE_BYTES=$((OCI_MAX_ARTIFACT_BYTES * 2))
DISABLE_OCI_REGISTRY=false
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
    --legacy-v1-mode) LEGACY_V1_MODE="$2"; LEGACY_V1_MODE_SET=true; shift 2 ;;
    --allow-legacy-nginx-extras) ALLOW_LEGACY_NGINX_EXTRAS=true; ALLOW_LEGACY_NGINX_EXTRAS_SET=true; shift ;;
    --allow-legacy-mutable-main) ALLOW_LEGACY_MUTABLE_MAIN=true; ALLOW_LEGACY_MUTABLE_MAIN_SET=true; shift ;;
    --disallow-legacy-mutable-main) ALLOW_LEGACY_MUTABLE_MAIN=false; ALLOW_LEGACY_MUTABLE_MAIN_SET=true; shift ;;
    --oci-registry-image) OCI_REGISTRY_IMAGE="$2"; shift 2 ;;
    --oci-registry-port) OCI_REGISTRY_PORT="$2"; OCI_REGISTRY_PORT_SET=true; shift 2 ;;
    --oci-registry-storage) OCI_REGISTRY_STORAGE="$2"; shift 2 ;;
    --enable-oci-auth) OCI_AUTH_SET=true; OCI_AUTH_ENABLED=true; shift ;;
    --disable-oci-auth) OCI_AUTH_SET=true; OCI_AUTH_ENABLED=false; shift ;;
    --oci-registry-host) OCI_REGISTRY_HOST="$2"; shift 2 ;;
    --oci-tailscale-service) OCI_TAILSCALE_SERVICE="$2"; shift 2 ;;
    --disable-oci-registry) DISABLE_OCI_REGISTRY=true; shift ;;
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
for command in bash "$PYTHON_BIN" node podman systemctl systemd-analyze sha256sum getent curl df sed; do
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
if command -v systemd >/dev/null 2>&1 && [[ "$(systemd --version | awk 'NR==1 {print $2}')" -lt 252 ]]; then
  errors+=("systemd 252 or newer is required")
fi
QUADLET_GENERATOR="${ARCTURUS_QUADLET_GENERATOR:-/usr/lib/systemd/system-generators/podman-system-generator}"
[[ -x "$QUADLET_GENERATOR" ]] || errors+=("Podman Quadlet generator is missing: $QUADLET_GENERATOR")
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
    arcturus-router.service arcturusd.service render-oci-registry-quadlet.sh \
    configure-oci-tailnet-ingress.sh arcturus-oci-publish.sh; do
    [[ -f "$SOURCE_DIR/$file" ]] || errors+=("source artifact is missing: $SOURCE_DIR/$file")
  done
  source_root_check="$(cd "$SOURCE_DIR/.." 2>/dev/null && pwd || true)"
  for module in bus registry router; do
    [[ -f "$source_root_check/modules/$module/dist/index.js" ]] || \
      errors+=("compiled module is missing: modules/$module/dist/index.js")
  done
fi
[[ "$CONTAINER_CLI" == podman ]] || errors+=("--container-cli must be podman")
[[ "$LEGACY_V1_MODE" =~ ^(enforce|audit)$ || -z "$LEGACY_V1_MODE" ]] || errors+=("--legacy-v1-mode must be enforce or audit")
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
existing_oci_config="$HOST_HOME/.config/arcturus/oci-registry.env"
read_existing_value() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 0
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}

install_managed_env() {
  local rendered="$1" destination="$2" merged backup
  if [[ ! -f "$destination" || "$FORCE_CONFIG" == true ]]; then
    if [[ -f "$destination" ]]; then
      backup="$destination.backup.$(date -u +%Y%m%dT%H%M%SZ)"
      cp -a "$destination" "$backup"
    fi
    install -m 0600 "$rendered" "$destination"
    return
  fi

  merged="$(mktemp)"
  "$PYTHON_BIN" - "$destination" "$rendered" "$merged" <<'PY'
from pathlib import Path
import re
import sys

existing_path, rendered_path, merged_path = map(Path, sys.argv[1:])
assignment = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
rendered_lines = rendered_path.read_text(encoding="utf-8").splitlines()
managed: dict[str, str] = {}
managed_order: list[str] = []
for line in rendered_lines:
    match = assignment.match(line)
    if not match:
        continue
    key = match.group(1)
    if key not in managed:
        managed_order.append(key)
    managed[key] = line

output: list[str] = []
emitted: set[str] = set()
for line in existing_path.read_text(encoding="utf-8").splitlines():
    match = assignment.match(line)
    if not match or match.group(1) not in managed:
        output.append(line)
        continue
    key = match.group(1)
    if key not in emitted:
        output.append(managed[key])
        emitted.add(key)
for key in managed_order:
    if key not in emitted:
        output.append(managed[key])
merged_path.write_text("\n".join(output) + "\n", encoding="utf-8")
PY
  backup="$destination.backup.$(date -u +%Y%m%dT%H%M%SZ)"
  cp -a "$destination" "$backup"
  install -m 0600 "$merged" "$destination"
  rm -f "$merged"
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
if ! $LEGACY_V1_MODE_SET; then
  LEGACY_V1_MODE="$(read_existing_value "$existing_platform_config" ARCTURUS_LEGACY_V1_MODE)"
  # Native v1 manifests are provenance-stamped by the registry, so normal
  # upgrades can remain fail-closed. Audit mode is an explicit emergency bridge.
  [[ -n "$LEGACY_V1_MODE" ]] || LEGACY_V1_MODE=enforce
fi
[[ "$LEGACY_V1_MODE" =~ ^(enforce|audit)$ ]] || {
  echo "ARCTURUS_LEGACY_V1_MODE must be enforce or audit" >&2
  exit 2
}
if ! $ALLOW_LEGACY_NGINX_EXTRAS_SET; then
  existing_legacy_extras="$(read_existing_value "$existing_platform_config" ARCTURUS_ALLOW_LEGACY_NGINX_EXTRAS)"
  [[ "$existing_legacy_extras" == 1 ]] && ALLOW_LEGACY_NGINX_EXTRAS=true
fi
if ! $ALLOW_LEGACY_MUTABLE_MAIN_SET; then
  existing_mutable_main="$(read_existing_value "$existing_deployer_config" ARCTURUS_LEGACY_ALLOW_MUTABLE_MAIN)"
  [[ "$existing_mutable_main" == 1 ]] && ALLOW_LEGACY_MUTABLE_MAIN=true
fi
existing_oci_service="$(read_existing_value "$existing_oci_config" ARCTURUS_OCI_TAILSCALE_SERVICE)"
if $DISABLE_OCI_REGISTRY; then
  OCI_TAILSCALE_SERVICE_TO_CLEAR="$existing_oci_service"
else
  [[ -n "$OCI_REGISTRY_IMAGE" ]] || OCI_REGISTRY_IMAGE="$(read_existing_value "$existing_oci_config" ARCTURUS_OCI_REGISTRY_IMAGE)"
  if ! $OCI_REGISTRY_PORT_SET; then
    existing_oci_port="$(read_existing_value "$existing_oci_config" ARCTURUS_OCI_REGISTRY_PORT)"
    [[ -z "$existing_oci_port" ]] || OCI_REGISTRY_PORT="$existing_oci_port"
  fi
  [[ -n "$OCI_REGISTRY_STORAGE" ]] || OCI_REGISTRY_STORAGE="$(read_existing_value "$existing_oci_config" ARCTURUS_OCI_REGISTRY_STORAGE)"
  [[ -n "$OCI_REGISTRY_HOST" ]] || OCI_REGISTRY_HOST="$(read_existing_value "$existing_oci_config" ARCTURUS_OCI_REGISTRY_HOST)"
  [[ -n "$OCI_TAILSCALE_SERVICE" ]] || OCI_TAILSCALE_SERVICE="$existing_oci_service"
  if ! $OCI_AUTH_SET; then
    existing_oci_auth="$(read_existing_value "$existing_oci_config" ARCTURUS_OCI_AUTH_ENABLED)"
    case "$existing_oci_auth" in
      true) OCI_AUTH_ENABLED=true ;;
      false|"") OCI_AUTH_ENABLED=false ;;
      *)
        echo "Existing ARCTURUS_OCI_AUTH_ENABLED must be true or false" >&2
        exit 2
        ;;
    esac
  fi
  if $OCI_AUTH_SET && [[ "$OCI_AUTH_ENABLED" != true ]]; then
    # Explicitly disabling authorization also removes remote write ingress. Keep
    # the old Service name long enough to drain and clear it after the local
    # registry has been returned to its authenticated-disabled, read-only state.
    OCI_TAILSCALE_SERVICE_TO_CLEAR="$existing_oci_service"
    OCI_REGISTRY_HOST=""
    OCI_TAILSCALE_SERVICE=""
  elif [[ -n "$existing_oci_service" && "$existing_oci_service" != "$OCI_TAILSCALE_SERVICE" ]]; then
    # A Service rename must not leave the old private ingress advertised.
    OCI_TAILSCALE_SERVICE_TO_CLEAR="$existing_oci_service"
  fi
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
if [[ -n "$OCI_REGISTRY_IMAGE" && -z "$OCI_REGISTRY_STORAGE" ]]; then
  OCI_REGISTRY_STORAGE="$HOST_HOME/.local/share/arcturus-registry"
fi

oci_errors=()
if [[ -n "$OCI_REGISTRY_IMAGE" && ! "$OCI_REGISTRY_IMAGE" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}$ ]]; then
  oci_errors+=("--oci-registry-image must be pinned as repository@sha256:digest")
fi
if [[ ! "$OCI_REGISTRY_PORT" =~ ^[0-9]+$ ]] || ((OCI_REGISTRY_PORT < 1024 || OCI_REGISTRY_PORT > 65535)); then
  oci_errors+=("--oci-registry-port must be between 1024 and 65535")
elif [[ "$OCI_REGISTRY_PORT" == 9090 ]]; then
  oci_errors+=("--oci-registry-port must not conflict with the deployer API port 9090")
fi
if [[ -n "$OCI_REGISTRY_STORAGE" && ! "$OCI_REGISTRY_STORAGE" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
  oci_errors+=("--oci-registry-storage must be an absolute path without whitespace")
fi
if $DISABLE_OCI_REGISTRY && { [[ -n "$OCI_REGISTRY_IMAGE" ]] || $OCI_REGISTRY_PORT_SET || [[ -n "$OCI_REGISTRY_STORAGE" ]] || $OCI_AUTH_SET || [[ -n "$OCI_REGISTRY_HOST" ]] || [[ -n "$OCI_TAILSCALE_SERVICE" ]]; }; then
  oci_errors+=("--disable-oci-registry cannot be combined with OCI registry configuration options")
fi
if [[ -z "$OCI_REGISTRY_IMAGE" ]] && ! $DISABLE_OCI_REGISTRY && { $OCI_REGISTRY_PORT_SET || [[ -n "$OCI_REGISTRY_STORAGE" ]] || $OCI_AUTH_ENABLED || [[ -n "$OCI_REGISTRY_HOST" ]] || [[ -n "$OCI_TAILSCALE_SERVICE" ]]; }; then
  oci_errors+=("OCI registry port, storage, authorization, or tailnet ingress requires --oci-registry-image or an existing OCI configuration")
fi
if $OCI_AUTH_ENABLED && [[ "$OCI_REGISTRY_PORT" == 9190 ]]; then
  oci_errors+=("--oci-registry-port must not conflict with the Rust authorization port 9190")
fi
if $OCI_AUTH_ENABLED && [[ -n "$SOURCE_DIR" && ! -x "$SOURCE_DIR/arcturusd" ]]; then
  oci_errors+=("--enable-oci-auth requires a compiled executable at $SOURCE_DIR/arcturusd")
fi
if [[ -n "$OCI_REGISTRY_HOST" && ! "$OCI_REGISTRY_HOST" =~ ^[a-z0-9][a-z0-9.-]*\.ts\.net$ ]]; then
  oci_errors+=("--oci-registry-host must be a full lowercase Tailscale hostname ending in .ts.net")
fi
if [[ -n "$OCI_TAILSCALE_SERVICE" && ! "$OCI_TAILSCALE_SERVICE" =~ ^svc:[a-z0-9][a-z0-9-]{0,62}$ ]]; then
  oci_errors+=("--oci-tailscale-service must use svc:<lowercase-name> format")
fi
if [[ -n "$OCI_REGISTRY_HOST" && -z "$OCI_TAILSCALE_SERVICE" ]] || [[ -z "$OCI_REGISTRY_HOST" && -n "$OCI_TAILSCALE_SERVICE" ]]; then
  oci_errors+=("--oci-registry-host and --oci-tailscale-service must be configured together")
fi
if [[ -n "$OCI_TAILSCALE_SERVICE" && "$OCI_AUTH_ENABLED" != true ]]; then
  oci_errors+=("Tailscale OCI ingress requires --enable-oci-auth")
fi
if [[ -n "$OCI_TAILSCALE_SERVICE" ]] && ! command -v tailscale >/dev/null 2>&1; then
  oci_errors+=("missing prerequisite for OCI tailnet ingress: tailscale")
fi
if ((${#oci_errors[@]})); then
  printf 'Arcturus OCI registry validation failed\n' >&2
  printf '  - %s\n' "${oci_errors[@]}" >&2
  exit 2
fi
OCI_WRITABLE_ENABLED=false
if $OCI_AUTH_ENABLED && [[ -n "$OCI_REGISTRY_HOST" && -n "$OCI_TAILSCALE_SERVICE" ]]; then
  OCI_WRITABLE_ENABLED=true
fi

STATE_DIR="$HOST_HOME/.local/share/arcturus-deployer"
CONFIG_DIR="$HOST_HOME/.config/arcturus"
UNIT_DIR="$HOST_HOME/.config/systemd/user"
QUADLET_DIR="$HOST_HOME/.config/containers/systemd/arcturus"
BIN_DIR="$HOST_HOME/.local/bin"
RUNTIME_DIR="/run/user/$HOST_UID/arcturus"
CONFIG_FILE="$CONFIG_DIR/deployer.env"
PLATFORM_CONFIG_FILE="$CONFIG_DIR/platform.env"
OCI_CONFIG_FILE="$CONFIG_DIR/oci-registry.env"
OCI_RUNTIME_ENV_FILE="$CONFIG_DIR/oci-registry-runtime.env"
OCI_QUADLET_FILE="$QUADLET_DIR/arcturus-oci-registry.container"
ARCTURUSD_CONFIG_FILE="$CONFIG_DIR/arcturusd.env"
ARCTURUSD_UNIT_FILE="$UNIT_DIR/arcturusd.service"
OCI_SIGNING_KEY_FILE="$CONFIG_DIR/oci-signing.seed"
OCI_AUTH_STATE_DIR="$HOST_HOME/.local/share/arcturus-oci-auth"
OCI_JWKS_FILE="$OCI_AUTH_STATE_DIR/jwks.json"
OCI_AUTH_DB="$OCI_AUTH_STATE_DIR/grants.sqlite3"

if [[ -z "$VERSION" && -n "$SOURCE_DIR" ]]; then
  source_root="$(cd "$SOURCE_DIR/.." && pwd)"
  VERSION="local-$(
    {
      find "$SOURCE_DIR" -maxdepth 1 -type f -print0
      for module in bus registry router; do
        find "$source_root/modules/$module/dist" -type f -print0 2>/dev/null || true
        [[ ! -f "$source_root/modules/$module/package.json" ]]           || printf '%s\0' "$source_root/modules/$module/package.json"
      done
    } | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12
  )"
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
  OCI data plane:    ${OCI_REGISTRY_IMAGE:-disabled}${OCI_REGISTRY_IMAGE:+ on 127.0.0.1:$OCI_REGISTRY_PORT}
  OCI authorization: $([[ "$OCI_AUTH_ENABLED" == true ]] && echo enabled || echo disabled)
  OCI private HTTPS: ${OCI_REGISTRY_HOST:-disabled}${OCI_TAILSCALE_SERVICE:+ via $OCI_TAILSCALE_SERVICE}
  OCI write ingress: $([[ "$OCI_WRITABLE_ENABLED" == true ]] && echo enabled-after-validation || echo read-only)
  OCI storage:       ${OCI_REGISTRY_STORAGE:-not configured}
  state:             $STATE_DIR
  allowed bind roots: $(IFS=,; echo "${ALLOWED_BIND_ROOTS[*]}")
EOF

$VALIDATE_ONLY && exit 0
$DRY_RUN && exit 0

if [[ -n "$OCI_REGISTRY_IMAGE" ]]; then
  # Resolve the immutable infrastructure image before changing the installed release.
  podman image exists "$OCI_REGISTRY_IMAGE" || podman pull "$OCI_REGISTRY_IMAGE" >/dev/null
fi

umask 077
mkdir -p "$STATE_DIR/releases" "$STATE_DIR/active-manifests" "$CONFIG_DIR" "$UNIT_DIR" "$QUADLET_DIR" "$BIN_DIR" "$RUNTIME_DIR"
staging="$(mktemp -d "$STATE_DIR/releases/.install.XXXXXX")"
container_id=""
rendered_config=""
oci_unlock_guard_armed=false

set_oci_registry_read_only() {
  local value="$1" temporary
  [[ -f "$OCI_RUNTIME_ENV_FILE" ]] || return 1
  temporary="$(mktemp "${OCI_RUNTIME_ENV_FILE}.tmp.XXXXXX")"
  awk -v value="$value" '
    BEGIN { replaced = 0 }
    /^REGISTRY_STORAGE_MAINTENANCE_READONLY_ENABLED=/ {
      if (!replaced) {
        print "REGISTRY_STORAGE_MAINTENANCE_READONLY_ENABLED=" value
        replaced = 1
      }
      next
    }
    { print }
    END {
      if (!replaced) {
        print "REGISTRY_STORAGE_MAINTENANCE_READONLY_ENABLED=" value
      }
    }
  ' "$OCI_RUNTIME_ENV_FILE" >"$temporary"
  chmod 0600 "$temporary"
  mv -f "$temporary" "$OCI_RUNTIME_ENV_FILE"
  grep -qx "REGISTRY_STORAGE_MAINTENANCE_READONLY_ENABLED=$value"     "$OCI_RUNTIME_ENV_FILE"
}

cleanup() {
  local status=$?
  trap - EXIT
  if [[ "$oci_unlock_guard_armed" == true ]]; then
    set_oci_registry_read_only true >/dev/null 2>&1 || true
    systemctl --user restart arcturus-oci-registry.service >/dev/null 2>&1 || true
  fi
  [[ -z "$container_id" ]] || podman rm "$container_id" >/dev/null 2>&1 || true
  [[ ! -d "$staging" ]] || rm -rf "$staging"
  [[ -z "$rendered_config" ]] || rm -f "$rendered_config"
  exit "$status"
}
trap cleanup EXIT

if [[ -n "$SOURCE_DIR" ]]; then
  for file in app.py image_policy_app.py release.py arcturusctl.py requirements.txt \
    arcturus-deployer@.service arcturus-podman-api.service arcturus-bus.service \
    arcturus-registry.service arcturus-router.service arcturusd.service arcturusctl \
    render-oci-registry-quadlet.sh configure-oci-tailnet-ingress.sh arcturus-oci-publish.sh; do
    install -m 0644 "$SOURCE_DIR/$file" "$staging/$file"
  done
  chmod 0755 "$staging/arcturusctl" "$staging/arcturusctl.py" \
    "$staging/render-oci-registry-quadlet.sh" "$staging/configure-oci-tailnet-ingress.sh" \
    "$staging/arcturus-oci-publish.sh"
  if [[ -x "$SOURCE_DIR/arcturusd" ]]; then
    install -m 0755 "$SOURCE_DIR/arcturusd" "$staging/arcturusd"
  fi
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
ARCTURUS_OCI_REGISTRY_URL=${OCI_REGISTRY_IMAGE:+http://127.0.0.1:$OCI_REGISTRY_PORT}
ARCTURUS_OCI_RECEIPT_DB=$([[ "$OCI_WRITABLE_ENABLED" == true ]] && printf '%s' "$OCI_AUTH_DB")
ARCTURUS_OCI_REGISTRY_HOST=$([[ "$OCI_WRITABLE_ENABLED" == true ]] && printf '%s' "$OCI_REGISTRY_HOST")
ARCTURUS_LEGACY_ALLOW_MUTABLE_MAIN=$($ALLOW_LEGACY_MUTABLE_MAIN && printf 1 || printf 0)
EOF
install_managed_env "$rendered_config" "$CONFIG_FILE"

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
ARCTURUS_LEGACY_V1_MODE=${LEGACY_V1_MODE:-enforce}
ARCTURUS_ALLOW_LEGACY_NGINX_EXTRAS=$($ALLOW_LEGACY_NGINX_EXTRAS && printf 1 || printf 0)
ARCTURUS_OCI_REGISTRY_URL=${OCI_REGISTRY_IMAGE:+http://127.0.0.1:$OCI_REGISTRY_PORT}
EOF
install_managed_env "$rendered_platform" "$PLATFORM_CONFIG_FILE"
rm -f "$rendered_platform"

if [[ -n "$OCI_REGISTRY_IMAGE" ]]; then
  mkdir -p "$OCI_REGISTRY_STORAGE"
  chmod 0700 "$OCI_REGISTRY_STORAGE"
  rendered_oci="$(mktemp)"
  cat >"$rendered_oci" <<EOF
ARCTURUS_OCI_REGISTRY_IMAGE=$OCI_REGISTRY_IMAGE
ARCTURUS_OCI_REGISTRY_PORT=$OCI_REGISTRY_PORT
ARCTURUS_OCI_REGISTRY_STORAGE=$OCI_REGISTRY_STORAGE
ARCTURUS_OCI_REGISTRY_URL=http://127.0.0.1:$OCI_REGISTRY_PORT
ARCTURUS_OCI_AUTH_ENABLED=$OCI_AUTH_ENABLED
ARCTURUS_OCI_WRITABLE_ENABLED=$OCI_WRITABLE_ENABLED
ARCTURUS_OCI_REGISTRY_HOST=$OCI_REGISTRY_HOST
ARCTURUS_OCI_TAILSCALE_SERVICE=$OCI_TAILSCALE_SERVICE
EOF
  install_managed_env "$rendered_oci" "$OCI_CONFIG_FILE"
  rm -f "$rendered_oci"
  oci_http_secret="$(read_existing_value "$OCI_RUNTIME_ENV_FILE" REGISTRY_HTTP_SECRET)"
  [[ -n "$oci_http_secret" ]] || oci_http_secret="$($PYTHON_BIN -c 'import secrets; print(secrets.token_urlsafe(48))')"
  if $OCI_AUTH_ENABLED; then
    mkdir -p "$OCI_AUTH_STATE_DIR"
    chmod 0700 "$OCI_AUTH_STATE_DIR"
    [[ -x "$release_path/arcturusd" ]] || {
      echo "Installed release is missing the arcturusd binary required for OCI authorization" >&2
      exit 2
    }
    if [[ ! -f "$OCI_SIGNING_KEY_FILE" ]]; then
      "$PYTHON_BIN" - "$OCI_SIGNING_KEY_FILE" <<'PY'
import base64, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
tmp = path.with_suffix(path.suffix + '.new')
tmp.write_text(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('=') + '\n', encoding='utf-8')
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
    fi
    chmod 0600 "$OCI_SIGNING_KEY_FILE"
    rendered_arcturusd="$(mktemp)"
    cat >"$rendered_arcturusd" <<EOF
ARCTURUSD_LISTEN=127.0.0.1:9190
ARCTURUSD_UPLOAD_AUTH_ENABLED=true
ARCTURUSD_STATE_DB=$OCI_AUTH_DB
RUNNER_TOKENS_FILE=$CONFIG_DIR/tokens.json
ARCTURUS_OCI_SIGNING_KEY=$OCI_SIGNING_KEY_FILE
ARCTURUS_OCI_JWKS_FILE=$OCI_JWKS_FILE
ARCTURUS_OCI_REGISTRY=${OCI_REGISTRY_HOST:-127.0.0.1:$OCI_REGISTRY_PORT}
ARCTURUS_OCI_REGISTRY_INTERNAL=http://127.0.0.1:$OCI_REGISTRY_PORT
ARCTURUS_OCI_EXPECTED_OS=linux
ARCTURUS_OCI_MAX_LAYER_BYTES=$OCI_MAX_LAYER_BYTES
ARCTURUS_OCI_MAX_ARTIFACT_BYTES=$OCI_MAX_ARTIFACT_BYTES
ARCTURUS_OCI_MAX_CONCURRENT_VERIFICATIONS=2
ARCTURUS_OCI_TOKEN_ISSUER=arcturusd
ARCTURUS_OCI_TOKEN_SERVICE=arcturus-oci
ARCTURUS_OCI_UPLOAD_TTL_SECONDS=600
EOF
    install -m 0600 "$rendered_arcturusd" "$ARCTURUSD_CONFIG_FILE"
    rm -f "$rendered_arcturusd"
  else
    rm -f "$ARCTURUSD_CONFIG_FILE"
  fi

  rendered_oci_runtime="$(mktemp)"
  cat >"$rendered_oci_runtime" <<EOF
REGISTRY_HTTP_ADDR=0.0.0.0:5000
REGISTRY_HTTP_SECRET=$oci_http_secret
REGISTRY_LOG_LEVEL=info
REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY=/var/lib/registry
REGISTRY_STORAGE_DELETE_ENABLED=false
REGISTRY_STORAGE_MAINTENANCE_UPLOADPURGING_ENABLED=true
REGISTRY_STORAGE_MAINTENANCE_UPLOADPURGING_AGE=168h
REGISTRY_STORAGE_MAINTENANCE_UPLOADPURGING_INTERVAL=24h
REGISTRY_STORAGE_MAINTENANCE_UPLOADPURGING_DRYRUN=false
REGISTRY_STORAGE_MAINTENANCE_READONLY_ENABLED=true
EOF
  if $OCI_AUTH_ENABLED; then
    cat >>"$rendered_oci_runtime" <<EOF
REGISTRY_AUTH=token
REGISTRY_AUTH_TOKEN_REALM=$([[ "$OCI_WRITABLE_ENABLED" == true ]] && printf 'https://%s/auth/token' "$OCI_REGISTRY_HOST" || printf 'http://127.0.0.1:9190/auth/token')
REGISTRY_AUTH_TOKEN_SERVICE=arcturus-oci
REGISTRY_AUTH_TOKEN_ISSUER=arcturusd
REGISTRY_AUTH_TOKEN_JWKS=/etc/distribution/oci-jwks.json
REGISTRY_AUTH_TOKEN_SIGNINGALGORITHMS_0=EdDSA
EOF
  fi
  install -m 0600 "$rendered_oci_runtime" "$OCI_RUNTIME_ENV_FILE"
  rm -f "$rendered_oci_runtime"
  oci_quadlet_stage="$(mktemp -d)"
  "$release_path/render-oci-registry-quadlet.sh" \
    "$OCI_REGISTRY_IMAGE" "$OCI_REGISTRY_PORT" "$OCI_REGISTRY_STORAGE" \
    "$OCI_RUNTIME_ENV_FILE" "$([[ "$OCI_AUTH_ENABLED" == true ]] && printf '%s' "$OCI_JWKS_FILE")" \
    >"$oci_quadlet_stage/arcturus-oci-registry.container"
  QUADLET_UNIT_DIRS="$oci_quadlet_stage" "$QUADLET_GENERATOR" --user --dryrun >/dev/null
  install -m 0644 "$oci_quadlet_stage/arcturus-oci-registry.container" "$OCI_QUADLET_FILE"
  rm -rf "$oci_quadlet_stage"
else
  rm -f "$OCI_CONFIG_FILE" "$OCI_RUNTIME_ENV_FILE" "$ARCTURUSD_CONFIG_FILE"
fi

install -m 0644 "$release_path/arcturus-deployer@.service" "$UNIT_DIR/arcturus-deployer@.service"
install -m 0644 "$release_path/arcturus-podman-api.service" "$UNIT_DIR/arcturus-podman-api.service"
install -m 0644 "$release_path/arcturus-bus.service" "$UNIT_DIR/arcturus-bus.service"
install -m 0644 "$release_path/arcturus-registry.service" "$UNIT_DIR/arcturus-registry.service"
install -m 0644 "$release_path/arcturus-router.service" "$UNIT_DIR/arcturus-router.service"
install -m 0644 "$release_path/arcturusd.service" "$ARCTURUSD_UNIT_FILE"
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
if [[ -n "$OCI_REGISTRY_IMAGE" ]]; then
  if $OCI_AUTH_ENABLED; then
    systemctl --user enable arcturusd.service
    systemctl --user restart arcturusd.service
    auth_ready=false
    for _ in {1..30}; do
      if curl --fail --silent "http://127.0.0.1:9190/healthz" >/dev/null 2>&1 \
        && [[ -s "$OCI_JWKS_FILE" ]]; then
        auth_ready=true
        break
      fi
      sleep 1
    done
    $auth_ready || {
      systemctl --user status arcturusd.service --no-pager -l >&2 || true
      echo "Arcturus Rust OCI authorization did not become ready" >&2
      exit 1
    }
  else
    systemctl --user disable --now arcturusd.service >/dev/null 2>&1 || true
  fi
  systemctl --user restart arcturus-oci-registry.service
  registry_ready=false
  for _ in {1..30}; do
    if $OCI_AUTH_ENABLED; then
      registry_headers="$(mktemp)"
      registry_status="$(curl --silent --output /dev/null --dump-header "$registry_headers" \
        --write-out '%{http_code}' "http://127.0.0.1:$OCI_REGISTRY_PORT/v2/" || true)"
      if [[ "$registry_status" == 401 ]] \
        && grep -Eqi '^Www-Authenticate: Bearer .*service="?arcturus-oci"?' "$registry_headers"; then
        registry_ready=true
      fi
      rm -f "$registry_headers"
      $registry_ready && break
    elif curl --fail --silent "http://127.0.0.1:$OCI_REGISTRY_PORT/v2/" >/dev/null 2>&1; then
      registry_ready=true
      break
    fi
    sleep 1
  done
  $registry_ready || {
    systemctl --user status arcturus-oci-registry.service --no-pager -l >&2 || true
    echo "Arcturus OCI registry did not become ready on loopback port $OCI_REGISTRY_PORT" >&2
    exit 1
  }
  if $OCI_WRITABLE_ENABLED; then
    "$release_path/configure-oci-tailnet-ingress.sh" \
      "$OCI_TAILSCALE_SERVICE" "$OCI_REGISTRY_HOST" "$OCI_REGISTRY_PORT"
    available_bytes="$(df -PB1 "$OCI_REGISTRY_STORAGE" | awk 'NR==2 {print $4}')"
    if [[ ! "$available_bytes" =~ ^[0-9]+$ ]] || ((available_bytes < OCI_MIN_FREE_BYTES)); then
      echo "OCI registry needs at least $OCI_MIN_FREE_BYTES bytes free before write ingress is enabled (available: ${available_bytes:-unknown})" >&2
      exit 1
    fi

    # Only a verified private HTTPS route and sufficient local headroom may
    # unlock Distribution writes. Arm the EXIT guard before changing the file;
    # normal failure or interruption restores read-only mode and restarts the
    # registry before the installer exits.
    oci_unlock_guard_armed=true
    if ! set_oci_registry_read_only false; then
      echo "failed to unlock OCI registry writes atomically" >&2
      exit 1
    fi
    if ! systemctl --user restart arcturus-oci-registry.service || \
       ! "$release_path/configure-oci-tailnet-ingress.sh" \
          "$OCI_TAILSCALE_SERVICE" "$OCI_REGISTRY_HOST" "$OCI_REGISTRY_PORT"; then
      echo "OCI write ingress validation failed; registry will be returned to read-only mode" >&2
      exit 1
    fi
  fi
else
  systemctl --user stop arcturus-oci-registry.service >/dev/null 2>&1 || true
  systemctl --user disable --now arcturusd.service >/dev/null 2>&1 || true
  rm -f "$OCI_QUADLET_FILE"
  systemctl --user daemon-reload
fi
if [[ -n "$OCI_TAILSCALE_SERVICE_TO_CLEAR" && "$OCI_TAILSCALE_SERVICE_TO_CLEAR" != "$OCI_TAILSCALE_SERVICE" ]]; then
  if command -v tailscale >/dev/null 2>&1; then
    # Stop advertising before removing all endpoint mappings. These commands
    # are best-effort so a previously cleared Service does not make registry
    # removal or an ingress rename non-idempotent.
    tailscale serve drain "$OCI_TAILSCALE_SERVICE_TO_CLEAR" >/dev/null 2>&1 || true
    tailscale serve clear "$OCI_TAILSCALE_SERVICE_TO_CLEAR" >/dev/null 2>&1 || true
  else
    echo "Warning: Tailscale Service $OCI_TAILSCALE_SERVICE_TO_CLEAR could not be cleared because the tailscale CLI is unavailable" >&2
  fi
fi
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


# The write-unlock rollback guard remains armed through every installation
# step, including router/deployer restart and optional firewall changes. Only a
# fully successful installation may leave Distribution writable.
oci_unlock_guard_armed=false

echo "Arcturus $VERSION installed for $HOST_USER."
echo "API readiness: curl --fail http://127.0.0.1:9090/healthz"
if [[ -n "$OCI_REGISTRY_IMAGE" ]]; then
  if $OCI_AUTH_ENABLED; then
    if $OCI_WRITABLE_ENABLED; then
      echo "OCI readiness: expect HTTP 401 and the HTTPS token realm from https://$OCI_REGISTRY_HOST/v2/"
    else
      echo "OCI readiness: local authenticated registry remains read-only at http://127.0.0.1:$OCI_REGISTRY_PORT/v2/"
    fi
  else
    echo "OCI readiness: curl --fail http://127.0.0.1:$OCI_REGISTRY_PORT/v2/"
  fi
fi
echo "Create a CI token with: arcturusctl token create --database '$CONFIG_DIR/tokens.json' --service <service> --token-id <service>-ci --output '$CONFIG_DIR/<service>-ci.token'"
