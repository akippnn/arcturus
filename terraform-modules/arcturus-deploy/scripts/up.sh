#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${1:?Usage: $0 <app_name> [stacks_base_dir]}"
STACKS_BASE="${2:-${STACKS_BASE_DIR:-/srv/arcturus/stacks}}"
STACK_DIR="$STACKS_BASE/$APP_NAME"
COMPOSE_FILE="$STACK_DIR/compose.yaml"

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "ERROR: Compose file not found at $COMPOSE_FILE"
  exit 1
fi

echo "--- Deploying stack: $APP_NAME ---"

echo "[1/2] Pulling latest image layers..."
(cd "$STACK_DIR" && docker compose pull)

echo "[2/2] Re-creating containers via docker compose..."
(cd "$STACK_DIR" && docker compose up -d --remove-orphans) && {
  echo "--- $APP_NAME deployed successfully ---"

  # Check and execute post-up hook
  if [ -f "$STACK_DIR/scripts/post-up.sh" ]; then
    echo "--- Executing post-up hook ---"
    chmod +x "$STACK_DIR/scripts/post-up.sh"
    bash "$STACK_DIR/scripts/post-up.sh"
  fi
  exit 0
}

echo "ERROR: docker compose failed for $APP_NAME"
exit 1
