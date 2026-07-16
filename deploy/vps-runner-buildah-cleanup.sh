#!/usr/bin/env bash
set -uo pipefail

RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
LOCK="$RUNTIME_DIR/vps-runner-buildah-cleanup.lock"
LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/vps-maintenance"
LOG_FILE="$LOG_DIR/runner-buildah-cleanup.log"
MIN_BYTES="${RUNNER_BUILDAH_CLEANUP_MIN_BYTES:-536870912}"
failures=0

mkdir -p "$RUNTIME_DIR" "$LOG_DIR"
exec 9>"$LOCK"
flock -n 9 || exit 0
exec >>"$LOG_FILE" 2>&1

log() {
    printf '%s [%s] %s\n' "$(date -Is)" "$1" "$2"
}

if ! [[ "$MIN_BYTES" =~ ^[0-9]+$ ]]; then
    log ERROR "RUNNER_BUILDAH_CLEANUP_MIN_BYTES must be an integer."
    exit 2
fi

if ! podman info >/dev/null 2>&1; then
    log ERROR "Podman is unavailable; runner cleanup was not attempted."
    exit 1
fi

mapfile -t runners < <(
    podman ps --filter label=u128.arcturus.runner=true --format '{{.Names}}' 2>/dev/null | sort
)

if ((${#runners[@]} == 0)); then
    log INFO "No running Arcturus runners found."
    exit 0
fi

for runner in "${runners[@]}"; do
    bytes="$(
        podman exec "$runner" sh -c \
            "du -sk /var/lib/containers/storage 2>/dev/null | awk '{ print \$1 * 1024 }'" \
            2>/dev/null || true
    )"
    if ! [[ "$bytes" =~ ^[0-9]+$ ]]; then
        log WARN "Could not measure nested Buildah storage in $runner; skipping."
        failures=$((failures + 1))
        continue
    fi
    if (( bytes < MIN_BYTES )); then
        log INFO "Skipping $runner: nested Buildah storage is ${bytes} bytes."
        continue
    fi

    log WARN "Cleaning ${bytes} bytes from idle runner $runner."
    if podman exec "$runner" sh -ceu '
        if ps -eo ppid= | awk '"'"'$1 == 1 { found=1 } END { exit(found ? 0 : 1) }'"'"'; then
            echo "runner job is active" >&2
            exit 75
        fi
        buildah rm --all
        buildah prune --all --force
    '; then
        log INFO "Cleaned the default nested Buildah store in $runner."
    else
        status=$?
        if (( status == 75 )); then
            log INFO "Skipping $runner because a job is active."
        else
            log ERROR "Nested Buildah cleanup failed in $runner with status $status."
            failures=$((failures + 1))
        fi
    fi
done

exit "$failures"
