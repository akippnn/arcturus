import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pi_migration_runtime as runtime


def fixture_inventory():
    containers = []
    for n, running in (("api", True), ("worker", True), ("scheduler", False)):
        cid = (n[0] * 64)
        containers.append({
            "Id": cid,
            "Name": "/" + n,
            "Config": {
                "Entrypoint": ["/usr/local/bin/service"],
                "Cmd": ["--mode", n],
                "WorkingDir": "/srv/app",
                "User": "1000:1000",
                "Env": ["TOKEN=secret-value", 'MESSAGE=quoted " value'],
            },
            "HostConfig": {
                "NetworkMode": "arcturus-net",
                "CgroupnsMode": "private",
                "Runtime": "runc",
                "PortBindings": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]},
                "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            },
            "NetworkSettings": {"Networks": {"arcturus-net": {
                "Aliases": [n, "service-alias"], "IPAddress": "172.29.0." + str(len(containers) + 2),
            }}},
            "Mounts": [
                {"Type": "bind", "Source": "/home/aki/data", "Destination": "/data", "RW": True},
                {"Type": "volume", "Name": "anon-vol", "Source": "/var/lib/docker/volumes/anon-vol/_data",
                 "Destination": "/srv/app/node_modules", "RW": True},
            ],
            "State": {"Running": running},
        })
    return {
        "runtimes": {
            "docker:root": {
                "container": containers,
                "network": [{
                    "Name": "arcturus-net", "Driver": "bridge", "Internal": False,
                    "EnableIPv6": False, "Options": {},
                    "IPAM": {"Config": [{"Subnet": "172.29.0.0/24", "Gateway": "172.29.0.1", "IPRange": "172.29.0.128/25"}]},
                }],
                "volume": [{"Name": "anon-vol", "Driver": "local", "Scope": "local",
                            "Mountpoint": "/var/lib/docker/volumes/anon-vol/_data"}],
            },
            "podman:root": {"container": [], "volume": [], "network": []},
            "podman:aki": {"container": [], "volume": [], "network": []},
        }
    }


class RuntimePlanTests(unittest.TestCase):
    def plan(self):
        inv = fixture_inventory()
        snapshots = {c["Id"]: "localhost/snapshot-" + c["Name"].lstrip("").strip("/") + ":migrate"
                     for c in inv["runtimes"]["docker:root"]["container"]}
        return inv, snapshots, runtime.build_plan(inv, snapshots)

    def test_preserves_commands_runtime_identity_and_lifecycle(self):
        _, _, plan = self.plan()
        self.assertEqual(len(plan["containers"]), 3)
        first = plan["containers"][0]
        self.assertEqual(first["desired_running"], True)
        self.assertIn("--workdir", first["command"])
        self.assertIn("--user", first["command"])
        self.assertIn("--entrypoint", first["command"])
        self.assertIn("private", first["command"])
        self.assertIn("--cgroupns", first["command"])
        self.assertEqual(first["command"][-2:], ["--mode", "api"])
        self.assertEqual(plan["containers"][2]["desired_running"], False)
        self.assertEqual(plan["containers"][0]["restart_policy"], "unless-stopped")
        self.assertEqual(plan["containers"][0]["unit"], "arcturus-migrated-api.service")

    def test_env_is_carried_outside_create_args_and_written_as_envfile_content(self):
        _, _, plan = self.plan()
        container = plan["containers"][0]
        self.assertIn({"key": "TOKEN", "value": "secret-value"}, container["environment"])
        self.assertIn({"key": "MESSAGE", "value": 'quoted " value'}, container["environment"])
        self.assertNotIn("secret-value", " ".join(container["command"]))
        self.assertEqual(runtime._env_file_text([(x["key"], x["value"]) for x in container["environment"]]),
                         'TOKEN=secret-value\nMESSAGE=quoted " value\n')
        with self.assertRaisesRegex(runtime.RuntimeMigrationError, "line breaks"):
            runtime._docker_env({"Env": ["TOKEN=one\ntwo"]})

    def test_maps_network_alias_ipam_and_volume_archive(self):
        _, _, plan = self.plan()
        self.assertEqual(plan["networks"], [{"name": "arcturus-net", "source_name": "arcturus-net", "subnet": "172.29.0.0/24",
                                            "gateway": "172.29.0.1", "ip_range": "172.29.0.128/25"}])
        self.assertEqual(plan["volumes"][0]["name"], "anon-vol")
        self.assertEqual(plan["volumes"][0]["source_path"], "/var/lib/docker/volumes/anon-vol/_data")
        self.assertEqual(plan["volumes"][0]["archive"], runtime.volume_archive_name("anon-vol"))
        cmd = plan["containers"][0]["command"]
        self.assertIn("--network-alias", cmd)
        self.assertIn("--ip", cmd)
        self.assertIn("127.0.0.1:18080:8080/tcp", cmd)
        self.assertIn("/home/aki/data:/data:rw,Z", cmd)

    def test_missing_snapshot_fails_without_exposing_environment(self):
        inventory = fixture_inventory()
        with self.assertRaisesRegex(runtime.RuntimeMigrationError, "no valid committed image snapshot") as raised:
            runtime.build_plan(inventory, {})
        self.assertNotIn("secret-value", str(raised.exception))

    def test_rejects_unsupported_runtime_settings(self):
        inventory = fixture_inventory()
        inventory["runtimes"]["docker:root"]["container"][0]["HostConfig"]["Privileged"] = True
        with self.assertRaisesRegex(runtime.RuntimeMigrationError, "privileged container"):
            runtime.build_plan(inventory, {c["Id"]: "snapshot:one" for c in inventory["runtimes"]["docker:root"]["container"]})
        inventory = fixture_inventory()
        inventory["runtimes"]["docker:root"]["container"][0]["NetworkSettings"]["Networks"]["other"] = {}
        with self.assertRaisesRegex(runtime.RuntimeMigrationError, "multiple networks"):
            runtime.build_plan(inventory, {c["Id"]: "snapshot:one" for c in inventory["runtimes"]["docker:root"]["container"]})

        inventory = fixture_inventory()
        inventory["runtimes"]["docker:root"]["container"][0]["HostConfig"]["CgroupnsMode"] = "host"
        with self.assertRaisesRegex(runtime.RuntimeMigrationError, "CgroupnsMode"):
            runtime.build_plan(inventory, {c["Id"]: "snapshot:one" for c in inventory["runtimes"]["docker:root"]["container"]})

        inventory = fixture_inventory()
        inventory["runtimes"]["docker:root"]["container"][0]["HostConfig"]["Runtime"] = "kata"
        with self.assertRaisesRegex(runtime.RuntimeMigrationError, "unsupported Docker runtime"):
            runtime.build_plan(inventory, {c["Id"]: "snapshot:one" for c in inventory["runtimes"]["docker:root"]["container"]})

    def test_rejects_populated_rootful_runtime_and_bind_traversal(self):
        inventory = fixture_inventory()
        inventory["runtimes"]["podman:root"]["container"] = [{"Id": "existing"}]
        with self.assertRaisesRegex(runtime.RuntimeMigrationError, "populated rootful Podman"):
            runtime.build_plan(inventory, {c["Id"]: "snapshot:one" for c in inventory["runtimes"]["docker:root"]["container"]})
        self.assertFalse(runtime._under_home("/home/aki/../../etc/passwd"))
        self.assertFalse(runtime._under_home("/home/aki/./data"))

    def test_rejects_populated_rootless_runtime_and_unit_injection(self):
        inventory = fixture_inventory()
        inventory["runtimes"]["podman:aki"]["container"] = [{"Id": "x"}]
        with self.assertRaisesRegex(runtime.RuntimeMigrationError, "rootless"):
            runtime.build_plan(inventory, {c["Id"]: "snapshot:one" for c in inventory["runtimes"]["docker:root"]["container"]})
        with self.assertRaisesRegex(runtime.RuntimeMigrationError, "unsafe container name"):
            runtime._service({"name": "x.service\nExecStart=/tmp/pwn", "unit": "bad", "restart_policy": "always"})

    def test_volume_archive_hash_is_deterministic_and_does_not_embed_name(self):
        self.assertEqual(runtime.volume_archive_name("node_modules"), runtime.volume_archive_name("node_modules"))
        self.assertNotIn("node_modules", runtime.volume_archive_name("node_modules"))

    def test_docker_default_bridge_gets_a_nonconflicting_podman_name(self):
        inventory = fixture_inventory()
        docker = inventory["runtimes"]["docker:root"]
        docker["network"][0]["Name"] = "bridge"
        docker["network"][0]["Options"] = {"com.docker.network.bridge.default_bridge": "true"}
        for container in docker["container"]:
            old = container["NetworkSettings"]["Networks"].pop("arcturus-net")
            container["NetworkSettings"]["Networks"]["bridge"] = old
            container["HostConfig"]["NetworkMode"] = "bridge"
        plan = runtime.build_plan(inventory, {c["Id"]: "snapshot:" + c["Name"].strip("/") for c in docker["container"]})
        self.assertEqual(plan["networks"][0]["name"], "arcturus-docker-bridge")
        self.assertEqual(plan["networks"][0]["source_name"], "bridge")
        self.assertIn("arcturus-docker-bridge", plan["containers"][0]["command"])


class RuntimeArchiveTests(unittest.TestCase):
    def make_archive(self, directory, entries):
        path = Path(directory) / "volume.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, kind, target in entries:
                member = tarfile.TarInfo(name)
                member.uid = 999
                member.gid = 999
                if kind == "file":
                    data = b"volume data"
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
                elif kind == "dir":
                    member.type = tarfile.DIRTYPE
                    archive.addfile(member)
                elif kind == "symlink":
                    member.type = tarfile.SYMTYPE
                    member.linkname = target
                    archive.addfile(member)
                elif kind == "hardlink":
                    member.type = tarfile.LNKTYPE
                    member.linkname = target
                    archive.addfile(member)
                elif kind == "char":
                    member.type = tarfile.CHRTYPE
                    member.devmajor = 1
                    member.devminor = 3
                    archive.addfile(member)
        return path

    def test_volume_validator_accepts_regular_files_directories_and_internal_links(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_archive(directory, [
                ("./dir/", "dir", ""),
                ("./dir/data", "file", ""),
                ("dir/copy", "hardlink", "dir/data"),
                ("current", "symlink", "dir/data"),
            ])
            self.assertEqual(runtime._validate_volume_archive(Path(directory), path.name), path)

    def test_volume_validator_rejects_traversal_links_duplicates_and_devices(self):
        cases = [
            ([ ("../outside", "file", "") ], "traversal"),
            ([ ("escape", "symlink", "../../outside") ], "escaping link"),
            ([ ("target", "file", ""), ("alias", "hardlink", "/target") ], "unsafe link"),
            ([ ("first", "hardlink", "second"), ("second", "hardlink", "first") ], "cyclic hardlink"),
            ([ ("./same", "file", ""), ("same", "file", "") ], "duplicate"),
            ([ ("pivot", "symlink", "inside"), ("pivot/child", "file", "") ], "ancestor"),
            ([ ("device", "char", "") ], "special or unsupported"),
        ]
        for entries, message in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as directory:
                path = self.make_archive(directory, entries)
                with self.assertRaisesRegex(runtime.RuntimeMigrationError, message):
                    runtime._validate_volume_archive(Path(directory), path.name)

    def test_volume_validator_checks_gzip_crc(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_archive(directory, [("data", "file", "")])
            contents = bytearray(path.read_bytes())
            contents[-8] ^= 0x01
            path.write_bytes(contents)
            with self.assertRaisesRegex(runtime.RuntimeMigrationError, "valid gzip tar"):
                runtime._validate_volume_archive(Path(directory), path.name)

    def test_volume_validator_rejects_a_second_concatenated_tar(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as second_directory:
            first = self.make_archive(directory, [("safe", "file", "")])
            second = self.make_archive(second_directory, [("../outside", "file", "")])
            first.write_bytes(first.read_bytes() + second.read_bytes())
            with self.assertRaisesRegex(runtime.RuntimeMigrationError, "after its tar end marker"):
                runtime._validate_volume_archive(Path(directory), first.name)

    def test_volume_mountpoint_must_be_canonical_and_not_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            volume_root = Path(directory).resolve() / "volumes"
            data = volume_root / "named" / "_data"
            data.mkdir(parents=True)
            with mock.patch.object(runtime, "_PODMAN_VOLUME_ROOT", volume_root):
                self.assertEqual(runtime._validated_volume_mountpoint(str(data), "named"), data)
                alias = volume_root / "alias"
                alias.symlink_to(data.parent, target_is_directory=True)
                with self.assertRaisesRegex(runtime.RuntimeMigrationError, "invalid volume mountpoint"):
                    runtime._validated_volume_mountpoint(str(alias / "_data"), "named")
                data.rmdir()
                data.symlink_to(Path(directory), target_is_directory=True)
                with self.assertRaisesRegex(runtime.RuntimeMigrationError, "symlink"):
                    runtime._validated_volume_mountpoint(str(data), "named")

    def test_consume_images_removes_only_after_successful_podman_load(self):
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / "backup"
            backup.mkdir()
            filename = "image-" + hashlib.sha256(b"localhost/x:tag").hexdigest()[:16] + ".tar.gz"
            image_archive = backup / filename
            image_archive.write_bytes(b"opaque docker archive")
            unrelated = backup / "keep.txt"
            unrelated.write_text("keep")
            env_dir = Path(directory) / "env"
            container = {
                "name": "x", "unit": "arcturus-migrated-x.service", "image": "localhost/x:tag",
                "image_archive": filename, "desired_running": False, "restart_policy": "no",
                "command": ["podman", "create", "--name", "x", "--pull=never", "--cgroupns", "private",
                            "--env-file", f"{env_dir}/x.env", "--restart", "no", "localhost/x:tag"],
                "environment": [],
            }
            plan = {"schemaVersion": 1, "runtime": "podman:root", "containers": [container],
                    "networks": [], "volumes": [], "image_exports": {"localhost/x:tag": filename}}
            plan_path = Path(directory) / "plan.json"
            plan_path.write_text(json.dumps(plan))
            units = Path(directory) / "units"
            units.mkdir()

            def run_failure_on_load(args, *, check=True, **kwargs):
                if args[:2] == ["/usr/bin/podman", "load"]:
                    raise runtime.RuntimeMigrationError("simulated load failure")
                return type("Result", (), {"returncode": 1 if args[-2:] in (["exists", "x"], ["exists", "localhost/x:tag"]) else 0,
                                            "stdout": "", "stderr": ""})()

            def run_success(args, *, check=True, **kwargs):
                code = 1 if args in (["/usr/bin/podman", "container", "exists", "x"],
                                     ["/usr/bin/podman", "image", "exists", "localhost/x:tag"]) else 0
                return type("Result", (), {"returncode": code, "stdout": "", "stderr": ""})()

            path_is_file = Path.is_file
            def is_file(candidate):
                return True if candidate == Path("/usr/bin/podman") else path_is_file(candidate)

            with (mock.patch.object(runtime.os, "geteuid", return_value=0),
                  mock.patch.object(runtime, "_os_id", return_value="almalinux"),
                  mock.patch.object(runtime.shutil, "which", side_effect=lambda tool: "/usr/bin/" + tool),
                  mock.patch.object(runtime.os, "access", return_value=True),
                  mock.patch.object(Path, "is_file", is_file),
                  mock.patch.object(runtime, "_UNIT_DIR", units),
                  mock.patch.object(runtime, "_ENV_DIR", env_dir),
                  mock.patch.object(runtime, "_run", side_effect=run_failure_on_load)):
                with self.assertRaisesRegex(runtime.RuntimeMigrationError, "simulated load failure"):
                    runtime.apply_plan(plan_path, backup, consume_images=True)
            self.assertTrue(image_archive.exists())

            with (mock.patch.object(runtime.os, "geteuid", return_value=0),
                  mock.patch.object(runtime, "_os_id", return_value="almalinux"),
                  mock.patch.object(runtime.shutil, "which", side_effect=lambda tool: "/usr/bin/" + tool),
                  mock.patch.object(runtime.os, "access", return_value=True),
                  mock.patch.object(Path, "is_file", is_file),
                  mock.patch.object(runtime, "_UNIT_DIR", units),
                  mock.patch.object(runtime, "_ENV_DIR", env_dir),
                  mock.patch.object(runtime, "_run", side_effect=run_success)):
                runtime.apply_plan(plan_path, backup, consume_images=True)
            self.assertFalse(image_archive.exists())
            self.assertEqual(unrelated.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
