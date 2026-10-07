import argparse
import io
import json
import plistlib
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import provision_rpi_sd as provision


class ProvisionRpiSdTests(unittest.TestCase):
    def test_disk_parser_accepts_only_safe_external_whole_media(self):
        payload = plistlib.dumps(
            {
                "DeviceNode": "/dev/disk5",
                "DeviceIdentifier": "disk5",
                "TotalSize": 31_914_983_424,
                "WholeDisk": True,
                "Internal": False,
                "VirtualOrPhysical": "Physical",
                "Writable": True,
                "RemovableMediaOrExternalDevice": True,
                "MediaName": "SD Reader",
                "BusProtocol": "USB",
            }
        )
        disk = provision.parse_disk_plist(payload)
        self.assertEqual(disk.device, "/dev/disk5")
        self.assertEqual(disk.raw_device, "/dev/rdisk5")

        unsafe = plistlib.loads(payload)
        unsafe["Internal"] = True
        with self.assertRaisesRegex(provision.ProvisionError, "refusing unsafe"):
            provision.parse_disk_plist(plistlib.dumps(unsafe))

    def test_network_context_is_derived_from_dhcp_lease(self):
        context = provision.parse_dhcp_packet(
            "en0",
            """ciaddr = 192.168.68.60
yiaddr = 192.168.68.60
subnet_mask (ip): 255.255.252.0
router (ip_mult): {192.168.68.1}
domain_name_server (ip_mult): {192.168.1.1, 192.168.68.1}
""",
        )
        self.assertEqual(context.network, "192.168.68.0/22")
        self.assertEqual(context.gateway, "192.168.68.1")
        self.assertEqual(context.dns, ("192.168.1.1", "192.168.68.1"))

    def test_network_context_falls_back_to_static_interface_configuration(self):
        context = provision.parse_static_network(
            "en7",
            "   gateway: 10.40.0.1\n   interface: en7\n",
            "\tinet 10.40.2.15 netmask 0xfffffc00 broadcast 10.40.3.255\n",
            "  nameserver[0] : 10.40.0.53\n  nameserver[1] : 2001:db8::53\n",
        )
        self.assertEqual(context.address, "10.40.2.15")
        self.assertEqual(context.network, "10.40.0.0/22")
        self.assertEqual(context.gateway, "10.40.0.1")
        self.assertEqual(context.dns, ("10.40.0.53",))

    def test_candidate_picker_excludes_current_gateway_occupied_and_dhcp_pool(self):
        context = provision.NetworkContext(
            "en0", "10.20.30.10", "10.20.30.0/24", "10.20.30.1", ("10.20.30.1",)
        )
        values = provision.candidate_addresses(
            context,
            {provision.ipaddress.ip_address("10.20.30.254")},
            excluded_range=(
                provision.ipaddress.ip_address("10.20.30.200"),
                provision.ipaddress.ip_address("10.20.30.253"),
            ),
            limit=2,
            probe=False,
        )
        self.assertEqual(values, ["10.20.30.199", "10.20.30.198"])

    def test_static_address_must_be_inside_detected_network_and_not_reserved(self):
        context = provision.NetworkContext(
            "en0", "192.0.2.10", "192.0.2.0/24", "192.0.2.1", ("192.0.2.1",)
        )
        self.assertEqual(provision.validate_static_address("192.0.2.50", context), "192.0.2.50/24")
        with self.assertRaises(provision.ProvisionError):
            provision.validate_static_address("198.51.100.50", context)
        with self.assertRaises(provision.ProvisionError):
            provision.validate_static_address("192.0.2.1", context)
        with self.assertRaisesRegex(provision.ProvisionError, "does not match"):
            provision.validate_static_address("192.0.2.50/25", context)

    def test_supported_alma_majors_come_from_compatibility_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "COMPATIBILITY.json"
            path.write_text(
                json.dumps({"features": ["almalinux-9.8-and-10.2-hosts"]}), encoding="utf-8"
            )
            self.assertEqual(provision.supported_alma_majors(path), [10, 9])

    def test_image_index_filters_desktop_and_describes_partition_scheme(self):
        html = """
<a href="AlmaLinux-10-RaspberryPi-gpt-10.2-20260520.aarch64.raw.xz">server</a>
<a href="AlmaLinux-10-RaspberryPi-GNOME-gpt-10.2.aarch64.raw.xz">desktop</a>
<a href="AlmaLinux-10-RaspberryPi-mbr-10.2.aarch64.raw.xz">mbr</a>
"""
        choices = provision.parse_image_index("https://repo.example/10/images/", html, 10)
        self.assertEqual({choice.scheme for choice in choices}, {"gpt", "mbr"})
        self.assertTrue(all("GNOME" not in choice.url for choice in choices))

    def test_checksum_parser_supports_official_formats(self):
        filename = "AlmaLinux-RaspberryPi.aarch64.raw.xz"
        digest = "a" * 64
        self.assertEqual(
            provision.checksum_from_manifest(f"SHA256 ({filename}) = {digest}\n", filename), digest
        )
        self.assertEqual(provision.checksum_from_manifest(f"{digest}  {filename}\n", filename), digest)

    def test_local_raw_image_requires_and_verifies_explicit_checksum(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "AlmaLinux-10-RaspberryPi-gpt-test.aarch64.raw"
            image.write_bytes(b"verified raw image")
            digest = provision.sha256_file(image)
            self.assertEqual(provision.verify_local_image(image, digest), image.resolve())
            with self.assertRaisesRegex(provision.ProvisionError, "required"):
                provision.verify_local_image(image, None)
            with self.assertRaisesRegex(provision.ProvisionError, "checksum mismatch"):
                provision.verify_local_image(image, "0" * 64)

    def test_local_image_size_does_not_invoke_xz(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "image.raw"
            image.write_bytes(b"12345")
            with patch.object(provision, "xz_uncompressed_size") as xz_size:
                self.assertEqual(provision.image_uncompressed_size(image), 5)
            xz_size.assert_not_called()

    def test_cloud_init_pre_authorizes_key_without_password_login(self):
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeButWellFormedForTest operator@example"
        content = provision.cloud_init_user_data("edge-a", key)
        self.assertTrue(content.startswith("#cloud-config\n"))
        self.assertIn(key, content)
        self.assertIn('"ssh_pwauth": false', content)
        self.assertNotIn("password", content)
        self.assertIn("findmnt -nr -S LABEL=CIDATA", content)
        self.assertNotIn("/boot/arcturus-firstboot.sh", content)

    def test_firstboot_uses_runtime_inputs_and_seed_relative_tokens(self):
        bundle = f"registry.example/arcturus@sha256:{'b' * 64}"
        content = provision.render_firstboot(
            host_user="svcuser",
            worker_id="edge-a",
            control_plane_url="https://fleet.example:9190",
            bundle=bundle,
            bundle_delivery="staged",
            allowed_bind_roots=["/srv/apps"],
            service_tokens=["game-server"],
            registry_auth=True,
            layout_args=[
                "--config-root", "/srv/arcturus/config",
                "--data-root", "/srv/arcturus/data",
                "--cache-root", "/srv/arcturus/cache",
            ],
            config_root="/srv/arcturus/config",
        )
        self.assertIn("--source-dir /var/lib/arcturus-firstboot/payload/arcturus/deploy", content)
        self.assertNotIn(f"--bundle {bundle}", content)
        self.assertIn("--worker-id edge-a", content)
        self.assertIn('"$seed/.arcturus-lifecycle-game-server.token"', content)
        self.assertIn("--allowed-bind-root /srv/apps", content)
        self.assertIn("--config-root /srv/arcturus/config", content)
        self.assertIn("--data-root /srv/arcturus/data", content)
        self.assertIn('target_config_root=/srv/arcturus/config', content)
        self.assertIn('install -m 0755 "$seed/arcturus_paths.py"', content)
        self.assertIn(".arcturus-registry-auth.json", content)
        self.assertIn('host_home="$(getent passwd', content)
        self.assertNotIn("/home/svcuser", content)
        self.assertNotIn("seed=/boot", content)
        self.assertIn("dnf -q module list nodejs:22", content)
        self.assertIn("Node.js 22 or newer is unavailable", content)

        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "firstboot.sh"
            script.write_text(content, encoding="utf-8")
            result = provision.subprocess.run(
                ["bash", "-n", str(script)], text=True, capture_output=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_firstboot_installs_tailscale_without_enabling_tailscale_ssh_by_default(self):
        content = provision.render_firstboot(
            host_user="svcuser",
            worker_id="edge-a",
            control_plane_url="https://fleet.example:9190",
            bundle=f"registry.example/arcturus@sha256:{'b' * 64}",
            bundle_delivery="first-boot-pull",
            allowed_bind_roots=[],
            service_tokens=[],
            registry_auth=False,
            tailscale=True,
        )
        self.assertIn("rhel_major=\"$(rpm -E '%{rhel}')\"", content)
        self.assertIn("https://pkgs.tailscale.com/stable/rhel/$rhel_major/tailscale.repo", content)
        self.assertNotIn("stable/rhel/10/tailscale.repo", content)
        self.assertIn('tailscale up --auth-key="file:$tailscale_auth_key" --hostname=edge-a', content)
        self.assertNotIn("--hostname=edge-a --ssh", content)
        self.assertIn('rm -f "$seed/.arcturus-worker-token"', content)
        self.assertIn('"$seed/.tailscale-auth-key"', content)
        self.assertNotIn("tskey-", content)

        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "firstboot.sh"
            script.write_text(content, encoding="utf-8")
            result = provision.subprocess.run(
                ["bash", "-n", str(script)], text=True, capture_output=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_tailscale_ssh_is_an_explicit_opt_in(self):
        content = provision.render_firstboot(
            host_user="svcuser",
            worker_id="edge-a",
            control_plane_url="https://fleet.example:9190",
            bundle=f"registry.example/arcturus@sha256:{'b' * 64}",
            bundle_delivery="first-boot-pull",
            allowed_bind_roots=[],
            service_tokens=[],
            registry_auth=False,
            tailscale=True,
            tailscale_ssh=True,
        )
        self.assertIn("--hostname=edge-a --ssh", content)

    def test_raw_flash_does_not_start_xz(self):
        disk = provision.DiskCandidate("/dev/disk9", "/dev/rdisk9", 32_000_000_000, "SD", "USB")
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "image.raw"
            image.write_bytes(b"raw")
            completed = provision.subprocess.CompletedProcess([], 0, "", "")
            with (
                patch.object(provision, "run", return_value=completed),
                patch.object(provision.subprocess, "run", return_value=completed) as process_run,
                patch.object(provision.subprocess, "Popen") as popen,
            ):
                provision.flash_image(image, disk)
            popen.assert_not_called()
            self.assertEqual(process_run.call_args.args[0][:2], ["sudo", "dd"])

    def test_bundle_is_staged_from_the_arm64_oci_payload(self):
        bundle = f"registry.example/arcturus@sha256:{'b' * 64}"
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            if command[:2] == ["podman", "create"]:
                return provision.subprocess.CompletedProcess(command, 0, "container-id\n", "")
            if command[:2] == ["podman", "cp"]:
                payload = Path(command[-1]) / "deploy"
                payload.mkdir(parents=True)
                (payload / "install-host.sh").write_text("#!/bin/bash\n", encoding="utf-8")
                (payload / "arcturus-agent").write_text("agent\n", encoding="utf-8")
            return provision.subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            provision, "run", side_effect=fake_run
        ):
            archive = provision.stage_arcturus_bundle(bundle, None, Path(temporary), "podman")
            self.assertTrue(archive.is_file())
        self.assertIn(["podman", "pull", "--platform", "linux/arm64", bundle], commands)
        self.assertIn(["podman", "create", "--platform", "linux/arm64", bundle], commands)
        self.assertTrue(any(command[:2] == ["podman", "rm"] for command in commands))

    def test_plan_only_never_flashes_or_mounts(self):
        disk = provision.DiskCandidate("/dev/disk9", "/dev/rdisk9", 32_000_000_000, "Test SD", "USB")
        context = provision.NetworkContext(
            "en0", "192.0.2.10", "192.0.2.0/24", "192.0.2.1", ("192.0.2.1",)
        )
        image = provision.ImageChoice("https://repo.example/image.raw.xz", 10, "gpt")
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeButWellFormedForTest operator@example"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = root / "worker.token"
            public = root / "id.pub"
            worker.write_text("worker-secret\n", encoding="utf-8")
            public.write_text(key + "\n", encoding="utf-8")
            argv = [
                "--non-interactive",
                "--device", "/dev/disk9",
                "--static-ip", "192.0.2.50/24",
                "--target-interface", "eth-test",
                "--hostname", "edge-a",
                "--ssh-public-key", str(public),
                "--host-user", "svcuser",
                "--image-url", image.url,
                "--image-sha256", "a" * 64,
                "--arcturus-bundle", f"registry.example/arcturus@sha256:{'b' * 64}",
                "--worker-id", "edge-a",
                "--control-plane-url", "https://fleet.example:9190",
                "--worker-token-file", str(worker),
            ]
            output = io.StringIO()
            with (
                patch.object(provision.sys, "platform", "darwin"),
                patch.object(provision, "require_commands"),
                patch.object(provision, "pick_disk", return_value=disk),
                patch.object(provision, "discover_macos_network", return_value=context),
                patch.object(provision, "address_responds", return_value=False),
                patch.object(provision, "run") as command,
                patch.object(provision, "pick_image", return_value=image),
                patch.object(provision, "flash_image") as flash,
                redirect_stdout(output),
            ):
                command.return_value.stdout = ""
                self.assertEqual(provision.main(argv), 0)
            flash.assert_not_called()
            self.assertIn("Plan only", output.getvalue())

    def test_plan_only_accepts_verified_local_raw_without_downloading(self):
        disk = provision.DiskCandidate("/dev/disk9", "/dev/rdisk9", 32_000_000_000, "Test SD", "USB")
        context = provision.NetworkContext(
            "en0", "192.0.2.10", "192.0.2.0/24", "192.0.2.1", ("192.0.2.1",)
        )
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeButWellFormedForTest operator@example"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = root / "worker.token"
            public = root / "id.pub"
            image = root / "AlmaLinux-10-RaspberryPi-gpt-test.aarch64.raw"
            worker.write_text("worker-secret\n", encoding="utf-8")
            public.write_text(key + "\n", encoding="utf-8")
            image.write_bytes(b"raw image")
            argv = [
                "--non-interactive",
                "--device", "/dev/disk9",
                "--static-ip", "192.0.2.50/24",
                "--target-interface", "eth-test",
                "--hostname", "edge-a",
                "--ssh-public-key", str(public),
                "--host-user", "svcuser",
                "--image-file", str(image),
                "--image-sha256", provision.sha256_file(image),
                "--arcturus-bundle", f"registry.example/arcturus@sha256:{'b' * 64}",
                "--worker-id", "edge-a",
                "--control-plane-url", "https://fleet.example:9190",
                "--worker-token-file", str(worker),
            ]
            output = io.StringIO()
            with (
                patch.object(provision.sys, "platform", "darwin"),
                patch.object(provision, "require_commands"),
                patch.object(provision, "pick_disk", return_value=disk),
                patch.object(provision, "discover_macos_network", return_value=context),
                patch.object(provision, "address_responds", return_value=False),
                patch.object(provision, "run") as command,
                patch.object(provision, "pick_image") as pick_image,
                patch.object(provision, "download_verified") as download,
                patch.object(provision, "flash_image") as flash,
                redirect_stdout(output),
            ):
                command.return_value.stdout = ""
                self.assertEqual(provision.main(argv), 0)
            pick_image.assert_not_called()
            download.assert_not_called()
            flash.assert_not_called()
            self.assertIn(str(image.resolve()), output.getvalue())


if __name__ == "__main__":
    unittest.main()
