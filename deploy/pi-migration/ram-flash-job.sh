#!/bin/bash
# Detach the prepared-image writer from the SSH session. Never relaunch a job.
set -euo pipefail
export PATH=/bin:/usr/bin:/sbin:/usr/sbin
test "$#" = 6
nonce=$1
[[ "$nonce" =~ ^[0-9a-f]{32}$ ]]
[[ "$2" =~ ^[0-9a-f]{32}$ ]]
[[ "$3" =~ ^[0-9]+$ ]]
[[ "$4" =~ ^[0-9a-f]{64}$ ]]
[[ "$5" =~ ^[0-9a-f]{64}$ ]]
[[ "$6" =~ ^[0-9]+$ ]]
/bin/busybox --list | /bin/busybox grep -x setsid >/dev/null
umask 077
job=/run/alma-flash-job-$nonce
mkdir -m 700 "$job" # Existing or uncertain jobs require observation, never retry.
cat > "$job/worker.sh" <<'WORKER'
#!/bin/bash
export PATH=/bin:/usr/bin:/sbin:/usr/sbin
trap '' HUP
job=$1
shift
printf '%s\n' "$$" > "$job/pid"
/recovery/ram-flash.sh "$@"
result=$?
printf '%s\n' "$result" > "$job/exit.partial"
mv "$job/exit.partial" "$job/exit"
exit "$result"
WORKER
chmod 700 "$job/worker.sh"
/bin/busybox setsid /bin/bash "$job/worker.sh" "$job" "$@" </dev/null >"$job/log" 2>&1 &
printf 'Detached flash job launched.\n'
