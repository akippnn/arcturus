#!/usr/bin/env python3
"""Run as root on the source Pi; stdout is sensitive inventory, never a log."""
import grp
import json
import os
import pathlib
import pwd
import subprocess
import sys


def run(args, json_output=False):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=90)
        if p.returncode:
            return {"error": p.stderr.strip(), "status": p.returncode}
        return json.loads(p.stdout) if json_output else p.stdout
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return {"error": str(e)}


def runtime(engine, prefix):
    result = {}
    for kind in ("container", "image", "volume", "network"):
        ids = run(prefix + [engine, kind, "ls", "-aq" if kind in ("container", "image") else "-q"])
        if isinstance(ids, dict):
            result[kind] = ids
        elif ids.strip():
            result[kind] = run(prefix + [engine, kind, "inspect"] + ids.split(), True)
        else:
            result[kind] = []
    result["info"] = run(prefix + [engine, "info", "--format", "json"], True)
    return result


if os.geteuid() != 0:
    sys.exit("inventory must run as root")
users = [u for u in pwd.getpwall() if u.pw_uid == 0 or 1000 <= u.pw_uid < 65534]
result = {
    "schemaVersion": 1,
    "hostname": run(["hostname"]),
    "model": pathlib.Path("/proc/device-tree/model").read_text().strip("\x00"),
    "kernel": run(["uname", "-r"]),
    "os": pathlib.Path("/etc/os-release").read_text(),
    "addresses": run(["ip", "-j", "address"], True),
    "routes": run(["ip", "-j", "route"], True),
    "blockdevices": run(["lsblk", "-J", "-b", "-o", "NAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINTS,UUID,LABEL"], True),
    "mounts": run(["findmnt", "-J", "-o", "TARGET,SOURCE,FSTYPE,OPTIONS,MAJ:MIN"], True),
    "users": [{"name": u.pw_name, "uid": u.pw_uid, "gid": u.pw_gid, "home": u.pw_dir, "shell": u.pw_shell} for u in users],
    "system_users": [{"name": u.pw_name, "uid": u.pw_uid, "gid": u.pw_gid, "home": u.pw_dir, "shell": u.pw_shell} for u in pwd.getpwall()],
    "groups": [{"name": g.gr_name, "gid": g.gr_gid, "members": g.gr_mem} for g in grp.getgrall()],
    "subuid": pathlib.Path("/etc/subuid").read_text(),
    "subgid": pathlib.Path("/etc/subgid").read_text(),
    "system_units": run(["systemctl", "list-unit-files", "--no-pager", "--no-legend"]),
    "active_units": run(["systemctl", "list-units", "--all", "--no-pager", "--no-legend"]),
    "packages": run(["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\n"]),
    "runtimes": {},
    "user_units": {},
    "custom_user_units": {},
    "user_linger": {},
    "ssh_host_public_key": pathlib.Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text(),
    "custom_units": {},
    "cron": {},
    "dependencies": {},
}
homekit_python = pathlib.Path("/opt/homekit-wol/venv/bin/python3")
if homekit_python.exists():
    freeze = run([str(homekit_python), "-m", "pip", "freeze"])
    result["dependencies"]["homekit_requirements"] = freeze.splitlines() if isinstance(freeze, str) else freeze
cloudflared = pathlib.Path("/usr/bin/cloudflared")
if cloudflared.is_file():
    result["dependencies"]["cloudflared"] = {"file": str(cloudflared), "description": run(["file", str(cloudflared)]), "sha256": run(["sha256sum", str(cloudflared)]).split()[0]}
for path in pathlib.Path("/etc/systemd/system").glob("*"):
    if path.suffix not in (".service", ".timer", ".socket", ".path"):
        continue
    if path.is_file() and not path.is_symlink():
        result["custom_units"][path.name] = {"content": path.read_text(),
            "active": run(["systemctl", "is-active", path.name]),
            "enabled": run(["systemctl", "is-enabled", path.name])}
for u in users:
    result["cron"][u.pw_name] = run(["crontab", "-u", u.pw_name, "-l"])
for engine in ("docker", "podman"):
    result["runtimes"][engine + ":root"] = runtime(engine, [])
for u in users:
    if u.pw_uid == 0:
        continue
    prefix = ["runuser", "-u", u.pw_name, "--", "env", "HOME=" + u.pw_dir, "XDG_RUNTIME_DIR=/run/user/" + str(u.pw_uid)]
    result["runtimes"]["podman:" + u.pw_name] = runtime("podman", prefix)
    result["user_units"][u.pw_name] = run(prefix + ["systemctl", "--user", "list-unit-files", "--no-pager", "--no-legend"])
    result["user_linger"][u.pw_name] = pathlib.Path("/var/lib/systemd/linger", u.pw_name).exists()
    result["custom_user_units"][u.pw_name] = {}
    for path in pathlib.Path(u.pw_dir, ".config/systemd/user").glob("*"):
        if path.suffix in (".service", ".timer", ".socket", ".path") and path.is_file() and not path.is_symlink():
            result["custom_user_units"][u.pw_name][path.name] = {"content": path.read_text(), "active": run(prefix + ["systemctl", "--user", "is-active", path.name]), "enabled": run(prefix + ["systemctl", "--user", "is-enabled", path.name])}
cid = pathlib.Path("/sys/block/mmcblk0/device/cid")
result["target"] = {"device": "/dev/mmcblk0", "cid": cid.read_text().strip(), "size_bytes": int(pathlib.Path("/sys/block/mmcblk0/size").read_text()) * 512}
json.dump(result, sys.stdout, indent=2)
