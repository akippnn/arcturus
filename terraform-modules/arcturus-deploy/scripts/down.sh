#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${1:?Usage: $0 <app_name> [stacks_base_dir]}"
STACKS_BASE="${2:-${STACKS_BASE_DIR:-/srv/arcturus/stacks}}"
STACK_DIR="$STACKS_BASE/$APP_NAME"

echo "--- Destroying stack: $APP_NAME ---"

# Check and execute pre-down hook
if [ -f "$STACK_DIR/scripts/pre-down.sh" ]; then
  echo "--- Executing pre-down hook ---"
  chmod +x "$STACK_DIR/scripts/pre-down.sh"
  bash "$STACK_DIR/scripts/pre-down.sh"
fi

# 1. docker compose down
echo "[1/3] docker compose down..."
(cd "$STACK_DIR" && docker compose down --remove-orphans) || true

# 2. Aggressive cleanup of any remaining containers
echo "[2/3] Cleaning up lingering containers matching '$APP_NAME'..."
for container in $(docker ps -a --filter "name=$APP_NAME" --format "{{.Names}}" 2>/dev/null); do
    docker stop -t 2 "$container" 2>/dev/null || true
    docker rm -f "$container" 2>/dev/null || true
done

# 3. Remove compose file and protection marker
echo "[3/3] Cleaning up files..."
rm -f "$STACK_DIR/.dockge-protect" 2>/dev/null || true

echo "--- $APP_NAME destroyed ---"
