"""Safety regression tests for the Pi migration control plane.

These tests use temporary files and mocked peers only. They never contact a Pi
or write outside their temporary directories.
"""
from __future__ import annotations

import copy
import contextlib
import hashlib
import io
import json
import lzma
import os
import stat
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pi_migrate as migrate


def inventory():
    return {
        "model": "Raspberry Pi 5 Model B Rev 1.0",
        "os": "PRETTY_NAME=\"Debian GNU/Linux 12 (bookworm)\"\nID=debian\n",
        "hostname": "arcturus-pi",
        "addresses": [{
            "ifname": "eth0", "address": "dc:a6:32:01:02:03",
            "addr_info": [{"family": "inet", "local": "192.168.68.58", "prefixlen": 24}],
        }],
        "routes": [{"dst": "default", "dev": "eth0", "gateway": "192.168.68.1"}],
        "target": {"device": "/dev/mmcblk0", "cid": "a" * 32, "size_bytes": 64 * 1024**3},
        "mounts": {"filesystems": [
            {"target": "/", "source": "/dev/mmcblk0p2"},
            {"target": "/boot/firmware", "source": "/dev/mmcblk0p1"},
        ]},
        "runtimes": {"docker": {"container": []}},
        "users": [
            {"name": "root", "uid": 0, "gid": 0},
            {"name": "aki", "uid": 1000, "gid": 1000},
        ],
        "subuid": "aki:100000:65536\n",
        "subgid": "aki:100000:65536\n",
    }


class CloudInitStatusFixturePeer:
    """Execute the actual remote acceptance program with mocked host APIs."""
    def __init__(self, document, returncode=2, *, existing_groups=None, memberships=None, events=None):
        self.document = document
        self.returncode = returncode
        self.existing_groups = set(existing_groups or ())
        self.memberships = memberships or {}
        self.events = events if events is not None else []
        self.commands = []

    def run(self, command, data=None, timeout=180):
        self.commands.append((command, data, timeout))
        if command == "sudo -n /usr/bin/python3 -":
            import grp
            import pwd
            if isinstance(self.document, str):
                stdout = self.document
            else:
                stdout = json.dumps(self.document)
            result = SimpleNamespace(returncode=self.returncode, stdout=stdout, stderr="")

            def get_group(name):
                if name not in self.existing_groups:
                    raise KeyError(name)
                return SimpleNamespace(gr_name=name, gr_gid=1000 if name == "aki" else 2000,
                                       gr_mem=self.memberships.get(name, []))

            stdout_buffer, stderr_buffer = io.StringIO(), io.StringIO()
            with (mock.patch("subprocess.run", return_value=result) as run_status,
                  mock.patch.object(grp, "getgrnam", side_effect=get_group),
                  mock.patch.object(pwd, "getpwnam", return_value=SimpleNamespace(pw_name="aki", pw_uid=1000, pw_gid=1000)),
                  contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer)):
                try:
                    exec(compile(data, "fixture-cloud-init-acceptance", "exec"), {})
                except BaseException:
                    raise migrate.MigrationError("fixture remote verifier rejected cloud-init status") from None
                self.events.append("status")
                self.status_invocation = run_status.call_args
            return stdout_buffer.getvalue().encode()
        if command == "test -f /var/lib/arcturus-migration-firstboot && sudo -n true":
            self.events.append("marker")
            return b""
        if command.startswith("sudo -n dnf -y install"):
            raise migrate.MigrationError("fixture stopped after acceptance persistence")
        return b""


class PiMigrateSafetyTests(unittest.TestCase):
    def _write_cloudflared_token_tar(self, path, *, token=b"credential", token_type=tarfile.REGTYPE,
                                     uid=0, gid=0, mode=0o600):
        with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for name in ("etc", "etc/cloudflared"):
                directory = tarfile.TarInfo(name)
                directory.type = tarfile.DIRTYPE
                directory.uid = directory.gid = 0
                directory.mode = 0o755
                archive.addfile(directory)
            member = tarfile.TarInfo("etc/cloudflared/token")
            member.uid, member.gid, member.mode = uid, gid, mode
            member.type = token_type
            if token_type == tarfile.REGTYPE:
                member.size = len(token)
                archive.addfile(member, io.BytesIO(token))
            else:
                member.linkname = token.decode()
                archive.addfile(member)

    def test_cloudflared_token_backup_requires_exact_protected_regular_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "rootfs.tar.gz"
            self._write_cloudflared_token_tar(source)
            migrate._verify_cloudflared_token_archive(source, "/etc/cloudflared/token")
            for kwargs, message in (({"token": b""}, "nonempty"),
                                    ({"uid": 1000}, "protected root-owned"),
                                    ({"gid": 1000}, "protected root-owned"),
                                    ({"mode": 0o640}, "protected root-owned"),
                                    ({"token_type": tarfile.SYMTYPE, "token": b"/tmp/token"}, "regular file")):
                with self.subTest(kwargs=kwargs):
                    self._write_cloudflared_token_tar(source, **kwargs)
                    with self.assertRaisesRegex(migrate.MigrationError, message):
                        migrate._verify_cloudflared_token_archive(source, "/etc/cloudflared/token")
            with self.assertRaisesRegex(migrate.MigrationError, "allowlist"):
                migrate._verify_cloudflared_token_archive(source, "/etc/cloudflared/other")

    def test_prepare_restore_selects_only_reviewed_cloudflared_token_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rootfs = directory / "rootfs.tar.gz"
            binary = b"static cloudflared fixture"
            token = b"root secret token"
            with tarfile.open(rootfs, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                for name in ("etc", "etc/cloudflared", "etc/systemd", "etc/systemd/system", "usr", "usr/bin"):
                    item = tarfile.TarInfo(name)
                    item.type = tarfile.DIRTYPE
                    item.uid = item.gid = 0
                    item.mode = 0o755
                    archive.addfile(item)
                for name, data, mode in (("etc/cloudflared/token", token, 0o600),
                                         ("usr/bin/cloudflared", binary, 0o755)):
                    item = tarfile.TarInfo(name)
                    item.uid = item.gid = 0
                    item.mode = mode
                    item.size = len(data)
                    archive.addfile(item, io.BytesIO(data))
            dependencies = {"cloudflared": {"description": "statically linked fixture",
                                              "file": {"path": "/usr/bin/cloudflared"},
                                              "sha256": hashlib.sha256(binary).hexdigest()}}
            source_unit = ("[Unit]\n[Service]\nExecStart=/usr/bin/cloudflared tunnel run "
                           "--token-file /etc/cloudflared/token\n")
            source_inventory = inventory()
            source_inventory["users"][1]["home"] = "/home/aki"
            source_inventory.update({
                "schemaVersion": 1,
                "dependencies": dependencies,
                "custom_units": {"cloudflared.service": {"content": source_unit, "active": "inactive", "enabled": "enabled"}},
                "cron": {}, "user_units": {"aki": ""}, "custom_user_units": {}, "user_linger": {},
                "system_units": "cloudflared.service enabled enabled\n", "active_units": "",
            })
            host_plan = migrate.host_adapter().build_plan(source_inventory, dependencies, verify_target=False)
            self.assertTrue(host_plan["ready"], host_plan["blockers"])
            (directory / "rootfs.tar.gz").write_bytes(rootfs.read_bytes())
            (directory / "inventory.json").write_text(json.dumps(source_inventory))
            (directory / "runtime-plan.json").write_text(json.dumps({"containers": []}))
            (directory / "host-plan.json").write_text(json.dumps(host_plan))
            state = {"phase": "backup-verified", "directory": str(directory), "user": "aki",
                     "source_runtime_units": [], "nonce": "a" * 32}
            with mock.patch.object(migrate, "check_backup_inputs"), \
                 mock.patch.object(migrate, "build_bundle", side_effect=lambda root, out, paths: {
                     "bundle": Path(out).name, "bundle_sha256": "a" * 64,
                     "selected_paths": list(paths), "deployment": "staged_only"}), \
                 mock.patch.object(migrate, "state_save"):
                migrate.prepare_restore(state)
            files_plan = json.loads((directory / "files-plan.json").read_text())
            self.assertEqual(files_plan["selected_paths"], ["/home/aki", "/root", "/opt", "/srv",
                                                              "/usr/local", "/etc/systemd/system/cloudflared.service",
                                                              "/etc/cloudflared/token"])
            self.assertEqual(state["restoration_acceptance"], "prepared")

    def _exec_cloudflared_program(self, program, *, fake_root=False, os_module=None):
        namespace = {}
        patches = []
        if fake_root:
            original_lstat = Path.lstat
            def root_lstat(path):
                info = original_lstat(path)
                return SimpleNamespace(st_mode=info.st_mode & ~0o022, st_uid=0)
            patches.append(mock.patch.object(Path, "lstat", root_lstat))
        if os_module is not None:
            patches.append(mock.patch.dict(sys.modules, {"os": os_module}))
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            exec(compile(program, "fixture-cloudflared-install", "exec"), namespace)
        return namespace

    def test_atomic_cloudflared_install_replaces_only_exact_known_symlink(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            bindir = root / "usr/local/bin"
            bindir.mkdir(parents=True)
            migration_root = root / "var/lib/arcturus-migration"
            migration_root.mkdir(parents=True, mode=0o700)
            os.chmod(migration_root, 0o700)
            destination = bindir / "cloudflared"
            destination.symlink_to("/usr/bin/cloudflared")
            source = root / "cloudflared"
            source.write_bytes(b"pinned-cloudflared-binary")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            token = "a" * 32
            temporary_path = str(destination) + ".arcturus-" + token + ".tmp"
            provenance = hashlib.sha256((token + "\0" + digest).encode()).hexdigest()[:20]
            with mock.patch.object(migrate, "CLOUDFLARED_PATH", str(destination)), \
                 mock.patch.object(migrate, "ARCTURUS_MIGRATION_ROOT", str(migration_root)):
                self._exec_cloudflared_program(migrate._cloudflared_atomic_program(
                    "prepare", destination=str(destination), migration_root=str(migration_root),
                    temporary=temporary_path, source_name=provenance), fake_root=True)
                Path(temporary_path).write_bytes(source.read_bytes())
                events = []
                observed_os = types.ModuleType("os")
                observed_os.__dict__.update(os.__dict__)
                def observed_fsync(fd):
                    mode = os.fstat(fd).st_mode
                    events.append(("fsync", "directory" if stat.S_ISDIR(mode) else "file"))
                    return os.fsync(fd)
                def root_fstat(fd):
                    info = os.fstat(fd)
                    return SimpleNamespace(st_mode=info.st_mode & ~0o022, st_uid=0)
                def observed_replace(src, dst):
                    events.append(("replace",))
                    return os.replace(src, dst)
                observed_os.fsync = observed_fsync
                observed_os.fstat = root_fstat
                observed_os.replace = observed_replace
                self._exec_cloudflared_program(migrate._cloudflared_atomic_program(
                    "commit", destination=str(destination), migration_root=str(migration_root),
                    temporary=temporary_path, digest=digest, source_name=provenance),
                    fake_root=True, os_module=observed_os)
            self.assertFalse(destination.is_symlink())
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            record = json.loads((migration_root / ("cloudflared-install-" + provenance + ".json")).read_text())
            self.assertEqual(record["state"], "known-source-symlink")
            self.assertEqual(record["target"], "/usr/bin/cloudflared")
            self.assertEqual(source.read_bytes(), b"pinned-cloudflared-binary")
            self.assertEqual(events, [("fsync", "file"), ("replace",), ("fsync", "directory")])

    def test_atomic_cloudflared_install_refuses_unknown_symlink_before_writes(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            bindir = root / "usr/local/bin"
            bindir.mkdir(parents=True)
            migration_root = root / "var/lib/arcturus-migration"
            migration_root.mkdir(parents=True, mode=0o700)
            os.chmod(migration_root, 0o700)
            destination = bindir / "cloudflared"
            destination.symlink_to("/unexpected/cloudflared")
            with mock.patch.object(migrate, "CLOUDFLARED_PATH", str(destination)), \
                 mock.patch.object(migrate, "ARCTURUS_MIGRATION_ROOT", str(migration_root)):
                with self.assertRaisesRegex(RuntimeError, "unexpected symlink"):
                    self._exec_cloudflared_program(migrate._cloudflared_atomic_program(
                        "prepare", destination=str(destination), migration_root=str(migration_root),
                        temporary=str(destination) + ".tmp", source_name="fixture"), fake_root=True)
            self.assertTrue(destination.is_symlink())
            self.assertEqual(os.readlink(destination), "/unexpected/cloudflared")
            self.assertEqual(list(migration_root.iterdir()), [])
            self.assertFalse((bindir / "cloudflared.tmp").exists())

    def test_finish_restore_refuses_wrong_phase_and_missing_frozen_result_before_peer(self):
        peer = mock.Mock()
        with mock.patch.object(migrate, "alma_peer", return_value=peer):
            with self.assertRaisesRegex(migrate.MigrationError, "only while"):
                migrate.finish_restore({"phase": "environment-restored"})
            peer.assert_not_called()
        state = {"phase": "environment-restoring", "restoration_acceptance": "verified",
                 "directory": "/tmp/nonexistent-resume-fixture", "restoration_artifacts": {}}
        with mock.patch.object(migrate, "alma_peer", return_value=peer):
            with self.assertRaisesRegex(migrate.MigrationError, "missing pinned plans"):
                migrate.finish_restore(state)
            peer.assert_not_called()

    def test_peer_command_quotes_known_hosts_path_and_uses_pinned_recovery_alias(self):
        state = {
            "key": "/tmp/pi key", "user": "aki", "host": "192.168.68.58",
            "host_key_alias": "arcturus-pi-wired", "known_hosts": "/tmp/keys dir/known_hosts",
        }
        normal = migrate.Peer(state).command("true")
        recovery = migrate.Peer(state, recovery=True).command("true")
        self.assertIn('UserKnownHostsFile="/tmp/keys dir/known_hosts"', normal)
        self.assertIn("GlobalKnownHostsFile=/dev/null", normal)
        self.assertIn("HostKeyAlias=arcturus-pi-wired", normal)
        self.assertEqual(normal[normal.index("-p") + 1], "22")
        self.assertEqual(normal[-2], "aki@192.168.68.58")
        self.assertEqual(recovery[recovery.index("-p") + 1], "2222")
        self.assertEqual(recovery[-2], "root@192.168.68.58")
        self.assertIn("HostKeyAlias=arcturus-pi-wired", recovery)

    def test_wired_identity_requires_pi_wired_address_with_default_route(self):
        inv = inventory()
        self.assertEqual(migrate.wired_identity(inv, "192.168.68.58"), {
            "interface": "eth0", "mac": "dc:a6:32:01:02:03", "address": "192.168.68.58",
            "prefix": 24, "gateway": "192.168.68.1",
        })
        for forbidden in ("tailscale0", "wlan0"):
            bad = copy.deepcopy(inv)
            bad["addresses"][0]["ifname"] = forbidden
            with self.subTest(interface=forbidden), self.assertRaises(migrate.MigrationError):
                migrate.wired_identity(bad, "192.168.68.58")
        bad_model = copy.deepcopy(inv)
        bad_model["model"] = "Raspberry Pi 4 Model B"
        with self.assertRaisesRegex(migrate.MigrationError, "not a Raspberry Pi 5"):
            migrate.wired_identity(bad_model, "192.168.68.58")
        with self.assertRaises(migrate.MigrationError):
            migrate.wired_identity(inv, "192.168.68.99")

    def test_validate_source_rejects_non_pi_non_debian_and_extra_persistent_device(self):
        valid = inventory()
        self.assertEqual(migrate.validate_source(valid, "192.168.68.58")["interface"], "eth0")
        cases = []
        non_pi = copy.deepcopy(valid)
        non_pi["model"] = "Raspberry Pi 4 Model B"
        cases.append(("non Pi", non_pi))
        non_debian = copy.deepcopy(valid)
        non_debian["os"] = "ID=ubuntu\n"
        cases.append(("non Debian", non_debian))
        extra_disk = copy.deepcopy(valid)
        extra_disk["mounts"]["filesystems"].append({"target": "/srv/data", "source": "/dev/sda1"})
        cases.append(("additional mounted device", extra_disk))
        wrong_root = copy.deepcopy(valid)
        wrong_root["mounts"]["filesystems"][0]["source"] = "/dev/sda2"
        cases.append(("unsupported root layout", wrong_root))
        for label, candidate in cases:
            with self.subTest(case=label), self.assertRaises(migrate.MigrationError):
                migrate.validate_source(candidate, "192.168.68.58")

    def test_runtime_signature_ignores_state_but_tracks_definition_and_network_changes(self):
        inv = inventory()
        container = {
            "Id": "container-1", "Image": "sha256:image",
            "Config": {"Cmd": ["app"], "Env": ["A=B"]},
            "HostConfig": {"Binds": ["/srv/app:/app"]},
            "Mounts": [{"Source": "/srv/app", "Destination": "/app"}],
            "State": {"Running": True, "Status": "running"},
            "NetworkSettings": {"Networks": {"bridge": {"Aliases": ["app"], "IPAMConfig": None}}},
        }
        inv["runtimes"]["docker"]["container"] = [container]
        baseline = migrate.runtime_signature(inv)
        state_only = copy.deepcopy(inv)
        state_only["runtimes"]["docker"]["container"][0]["State"] = {"Running": False, "Status": "exited"}
        self.assertEqual(migrate.runtime_signature(state_only), baseline)
        for change in (
            lambda c: c["Config"].update(Cmd=["different"]),
            lambda c: c["HostConfig"].update(Binds=["/other:/app"]),
            lambda c: c["NetworkSettings"]["Networks"]["bridge"].update(Aliases=["renamed"]),
        ):
            changed = copy.deepcopy(inv)
            change(changed["runtimes"]["docker"]["container"][0])
            self.assertNotEqual(migrate.runtime_signature(changed), baseline)

    def test_flash_rejects_wrong_cid_missing_backup_image_and_unverified_restore_before_peer_io(self):
        state = {"target": {"cid": "a" * 32}, "phase": "backup-verified", "image": {}}
        with mock.patch.object(migrate, "verify_backup") as verify, \
             mock.patch.object(migrate, "recovery_peer") as peer:
            with self.assertRaisesRegex(migrate.MigrationError, "--confirm-cid"):
                migrate.flash(state, SimpleNamespace(confirm_cid="b" * 32))
            verify.assert_not_called()
            peer.assert_not_called()

            with self.assertRaisesRegex(migrate.MigrationError, "environment restoration"):
                migrate.flash(state, SimpleNamespace(confirm_cid="a" * 32))
            verify.assert_not_called()
            peer.assert_not_called()

            state["restoration_acceptance"] = "verified"
            state.pop("image")
            with self.assertRaisesRegex(migrate.MigrationError, "verified offline backup"):
                migrate.flash(state, SimpleNamespace(confirm_cid="a" * 32))
            verify.assert_not_called()
            peer.assert_not_called()

    def test_flash_stops_when_local_signed_image_was_modified(self):
        raw_image = b"verified OS image contents"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            image_path = directory / "alma.raw.xz"
            image_path.write_bytes(lzma.compress(raw_image))
            recorded = migrate.image_metadata(image_path)
            image_path.write_bytes(lzma.compress(b"modified image contents"))
            state = {
                "target": {"cid": "a" * 32}, "phase": "backup-verified",
                "restoration_acceptance": "verified", "directory": str(directory),
                "image": dict(recorded, filename=image_path.name),
            }
            remote = mock.Mock()
            with mock.patch.object(migrate, "verify_backup", return_value={}), \
                 mock.patch.object(migrate, "recovery_peer", return_value=remote), \
                 mock.patch.object(migrate, "guard") as guard:
                with self.assertRaisesRegex(migrate.MigrationError, "image changed"):
                    migrate.flash(state, SimpleNamespace(confirm_cid="a" * 32))
            guard.assert_called_once_with(remote)
            remote.run.assert_not_called()
            remote.upload.assert_not_called()

    def test_image_metadata_checks_xz_content_size_and_detects_truncation(self):
        raw = b"abc" * 1000
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "image.xz"
            compressed = lzma.compress(raw)
            image.write_bytes(compressed)
            metadata = migrate.image_metadata(image)
            self.assertEqual(metadata, {
                "compressed_sha256": hashlib.sha256(compressed).hexdigest(),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "raw_bytes": len(raw),
            })
            image.write_bytes(lzma.compress(raw + b"x"))
            changed = migrate.image_metadata(image)
            self.assertNotEqual(changed["compressed_sha256"], metadata["compressed_sha256"])
            self.assertNotEqual(changed["raw_sha256"], metadata["raw_sha256"])
            self.assertEqual(changed["raw_bytes"], len(raw) + 1)
            image.write_bytes(compressed[:-8])
            with self.assertRaises((EOFError, lzma.LZMAError)):
                migrate.image_metadata(image)

    def test_cloud_seed_preserves_identity_subids_network_key_and_headless_settings(self):
        inv = inventory()
        inv["hostname"] = "pi-prod"
        inv["subuid"] = "aki:100000:65536\nservice:200000:65536\n"
        inv["subgid"] = "aki:100000:65536\nservice:200000:65536\n"
        state = {
            "user": "aki", "nonce": "f" * 32,
            "network": {"interface": "eth0", "mac": "dc:a6:32:01:02:03", "address": "192.168.68.58", "prefix": 24, "gateway": "192.168.68.1"},
        }
        public_key = 'ssh-ed25519 AAAATEST comment "quoted"'
        seed = migrate.cloud_seed(state, inv, public_key)
        user_data = seed["user-data"]
        self.assertIn('hostname: "pi-prod"', user_data)
        self.assertIn("    uid: 1000", user_data)
        self.assertIn('      - ' + json.dumps(public_key), user_data)
        self.assertIn('  - path: "/etc/subuid"', user_data)
        self.assertIn('content: ' + json.dumps(inv["subuid"]), user_data)
        self.assertIn('  - path: "/etc/subgid"', user_data)
        self.assertIn('content: ' + json.dumps(inv["subgid"]), user_data)
        self.assertIn("ssh_pwauth: false", user_data)
        self.assertIn("disable_root: true", user_data)
        self.assertIn("package_upgrade: false", user_data)
        self.assertIn("systemctl, enable, --now, sshd", user_data)
        self.assertIn("/var/lib/arcturus-migration-firstboot", user_data)
        self.assertIn('macaddress: "dc:a6:32:01:02:03"', seed["network-config"])
        self.assertIn("dhcp4: true", seed["network-config"])
        self.assertIn("instance-id: " + state["nonce"], seed["meta-data"])

    def test_cloud_seed_rejects_ambiguous_uid_gid_mapping(self):
        inv = inventory()
        inv["users"][1]["gid"] = 1001
        state = {"user": "aki", "nonce": "f" * 32, "network": {"mac": "dc:a6:32:01:02:03"}}
        with self.assertRaisesRegex(migrate.MigrationError, "UID/GID"):
            migrate.cloud_seed(state, inv, "ssh-ed25519 AAAATEST")


class CloudInitStatusAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source_inventory = inventory()
        self.source_inventory["groups"] = [
            {"name": name, "members": ["aki"]} for name in ("adm", "audio")
        ]
        (self.directory / "inventory.json").write_text(json.dumps(self.source_inventory))
        self.state = {"directory": str(self.directory), "user": "aki", "phase": "alma-boot-requested"}
        self.expected_groups = {"adm", "audio", "wheel"}
        self.existing_groups = set(self.expected_groups)
        self.memberships = {name: ["aki"] for name in self.expected_groups}

    def status_document(self, warnings=(), *, errors=None, stage_errors=None,
                        stage_warnings=None, severity="WARNING", status="done"):
        stage_warnings = list(warnings) if stage_warnings is None else list(stage_warnings)
        empty_stage = {"errors": [], "recoverable_errors": {}}
        stages = {name: copy.deepcopy(empty_stage)
                  for name in ("init", "init-local", "modules-config", "modules-final")}
        stages["init"]["recoverable_errors"] = {severity: stage_warnings} if stage_warnings else {}
        if stage_errors is not None:
            stages["modules-final"]["errors"] = stage_errors
        return {
            "status": status,
            "extended_status": "degraded done" if warnings else "done",
            "errors": [] if errors is None else errors,
            "recoverable_errors": {severity: list(warnings)} if warnings else {},
            **stages,
        }

    def peer(self, document=None, returncode=2, **kwargs):
        if document is None:
            document = self.status_document()
        memberships = kwargs.pop("memberships", self.memberships)
        existing = kwargs.pop("existing_groups", self.existing_groups)
        return CloudInitStatusFixturePeer(document, returncode,
                                         existing_groups=existing, memberships=memberships,
                                         **kwargs)

    def test_accepts_only_done_with_anchored_source_group_warning_and_membership(self):
        warnings = ["Skipping creation of existing group 'adm'",
                    "Skipping creation of existing group 'audio'"]
        document = self.status_document(warnings)
        peer = self.peer(document)
        result = migrate.verified_cloud_init_status(peer, self.state)
        self.assertEqual(result, {
            "status": "done", "returncode": 2, "known_warning_count": 2,
            "known_warning_groups": ["adm", "audio"],
        })
        self.assertEqual(peer.status_invocation.args[0], [
            "/usr/bin/cloud-init", "status", "--wait", "--format", "json"])
        self.assertTrue(peer.status_invocation.kwargs["capture_output"])
        self.assertTrue(peer.status_invocation.kwargs["text"])
        self.assertEqual(peer.status_invocation.kwargs["timeout"], 1800)
        self.assertNotIn("Skipping creation", result.__repr__())

    def test_accepts_clean_exit_zero(self):
        result = migrate.verified_cloud_init_status(self.peer(returncode=0), self.state)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["known_warning_count"], 0)
        self.assertEqual(result["known_warning_groups"], [])

    def test_rejects_unbounded_status_or_group_evidence(self):
        allowed = "Skipping creation of existing group 'adm'"
        cases = [
            ("other exit", self.status_document(), 3, self.existing_groups, self.memberships),
            ("invalid json", "not json", 2, self.existing_groups, self.memberships),
            ("not done", self.status_document([allowed], status="degraded"), 2, self.existing_groups, self.memberships),
            ("fatal aggregate error", self.status_document([allowed], errors=["fatal"]), 2, self.existing_groups, self.memberships),
            ("fatal stage error", self.status_document([allowed], stage_errors=["fatal"]), 2, self.existing_groups, self.memberships),
            ("unknown severity", self.status_document([allowed], severity="ERROR"), 2, self.existing_groups, self.memberships),
            ("stage warning absent from aggregate", self.status_document([], stage_warnings=[allowed]), 2, self.existing_groups, self.memberships),
            ("unrequested group", self.status_document(["Skipping creation of existing group 'backup'"]), 2, self.existing_groups, self.memberships),
            ("malformed message", self.status_document(["Skipping creation of existing group 'adm' extra"]), 2, self.existing_groups, self.memberships),
            ("warning with exit zero", self.status_document([allowed]), 0, self.existing_groups, self.memberships),
            ("code two without warning", self.status_document(), 2, self.existing_groups, self.memberships),
            ("missing actual group", self.status_document([allowed]), 2, {"adm", "wheel"}, self.memberships),
            ("missing membership", self.status_document([allowed]), 2, self.existing_groups,
             {"adm": [], "audio": ["aki"], "wheel": ["aki"]}),
        ]
        for label, document, returncode, groups, memberships in cases:
            with self.subTest(case=label), self.assertRaises(migrate.MigrationError):
                migrate.verified_cloud_init_status(
                    self.peer(document, returncode, existing_groups=groups, memberships=memberships), self.state)

    def test_rejects_missing_top_level_cloud_init_stage(self):
        document = self.status_document()
        document.pop("modules-config")
        with self.assertRaises(migrate.MigrationError):
            migrate.verified_cloud_init_status(self.peer(document, returncode=0), self.state)

    def test_wait_alma_saves_public_report_only_after_marker_and_boot_payload_checks(self):
        events = []
        warnings = ["Skipping creation of existing group 'adm'"]
        peer = self.peer(self.status_document(warnings), events=events)

        def save(state):
            self.assertIn("cloud_init_acceptance", state)
            events.append("save")

        with (mock.patch.object(migrate, "alma_peer", return_value=peer),
              mock.patch.object(migrate.boot_acceptance, "verify_boot_payload",
                                side_effect=lambda *args, **kwargs: events.append("boot-payload")),
              mock.patch.object(migrate, "state_save", side_effect=save),
              contextlib.redirect_stdout(io.StringIO())):
            migrate.wait_alma(self.state)
        self.assertEqual(events, ["status", "marker", "boot-payload", "save"])
        self.assertEqual(self.state["phase"], "alma-running")
        self.assertEqual(self.state["cloud_init_acceptance"]["known_warning_groups"], ["adm"])

    def test_restore_environment_rechecks_status_and_persists_after_boot_checks(self):
        events = []
        peer = self.peer(self.status_document(), returncode=0, events=events)

        def save(state):
            self.assertIn("cloud_init_acceptance", state)
            events.append("save")

        state = dict(self.state, restoration_acceptance="verified", restoration_artifacts={})
        with (mock.patch.object(migrate, "alma_peer", return_value=peer),
              mock.patch.object(migrate.boot_acceptance, "verify_boot_payload",
                                side_effect=lambda *args, **kwargs: events.append("boot-payload")),
              mock.patch.object(migrate, "state_save", side_effect=save),
              self.assertRaisesRegex(migrate.MigrationError, "fixture stopped")):
            migrate.restore_environment(state)
        self.assertEqual(events, ["status", "marker", "boot-payload", "save"])


if __name__ == "__main__":
    unittest.main()
