import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from install import (confirmed_ifupdown_dhcp, execute, has_rescue_memory,
                     image_from_manifest, preflight, require_web_password,
                     select_disk, verify_oracle_nat, verify_tencent_nat)
from stage_rescue import patch_reinstall, patch_trans, stage


def block_fixture():
    return {"blockdevices": [{"name": "vda", "type": "disk", "path": "/dev/vda",
                              "pkname": None, "size": 10 * 1024**3, "log-sec": 512,
                              "mountpoints": [None], "children": [
                                  {"name": "vda1", "type": "part", "path": "/dev/vda1",
                                   "pkname": "vda", "size": 1024**3, "mountpoints": ["/"]},
                              ]}]}


class InstallerTests(unittest.TestCase):
    def test_nominal_one_gib_vps_memory_passes_rescue_floor(self):
        self.assertTrue(has_rescue_memory(991288 * 1024))
        self.assertFalse(has_rescue_memory(800 * 1024**2))

    def test_oracle_nat_requires_exact_vnic_and_egress_match(self):
        network = {"mac": "02:00:17:00:b1:5a", "mode": "dhcp",
                   "address": "10.0.0.148", "prefix": 24, "gateway": "10.0.0.1"}
        vnic = {"macAddr": "02:00:17:00:B1:5A", "privateIp": "10.0.0.148",
                "subnetCidrBlock": "10.0.0.0/24", "virtualRouterIp": "10.0.0.1"}
        with patch("install.oracle_metadata_vnics", return_value=[vnic]), \
             patch("install.external_ipv4", return_value="132.145.90.54"):
            self.assertEqual(verify_oracle_nat(network, "132.145.90.54"), "132.145.90.54")
            with self.assertRaisesRegex(ValueError, "egress"):
                verify_oracle_nat(network, "132.145.90.55")
            with self.assertRaisesRegex(ValueError, "DHCP"):
                verify_oracle_nat({**network, "mode": "static"}, "132.145.90.54")
            with patch("install.oracle_metadata_vnics", return_value=[{**vnic, "macAddr": "02:00:17:00:b1:5b"}]):
                with self.assertRaisesRegex(ValueError, "VNIC"):
                    verify_oracle_nat(network, "132.145.90.54")

    def test_oracle_dhcp_from_main_interfaces_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            main = root / "interfaces"
            extra = root / "50-cloud-init"
            main.write_text("auto ens3\niface ens3 inet dhcp\n")
            extra.write_text("# empty\n")
            (root / "dhclient.ens3.pid").write_text("513\n")
            proc = root / "proc"
            (proc / "513").mkdir(parents=True)
            (proc / "513" / "cmdline").write_bytes(b"/sbin/dhclient\0-4\0ens3\0")
            self.assertTrue(confirmed_ifupdown_dhcp("ens3", (main, extra), root, proc))
            extra.write_text("iface ens3 inet static\n")
            self.assertFalse(confirmed_ifupdown_dhcp("ens3", (main, extra), root, proc))

    def test_rescue_messages_describe_ikuai_password_and_erase_stage(self):
        source = (Path(__file__).resolve().parents[1] / "vendor/reinstall.sh").read_text()
        patched = patch_reinstall(source)
        self.assertIn("救援 SSH：root，端口 22", patched)
        self.assertIn("账号 admin，密码与救援 SSH 相同", patched)
        self.assertIn("新系统 SSH 默认关闭", patched)
        self.assertIn("此阶段不再提供取消命令", patched)
        self.assertIn('IKUAI_PUBLIC_IP:-IP', patched)
        self.assertNotIn("镜像的用户名/密码/公钥不会被修改。", patched)

    def test_dhcp_client_and_cloud_init_must_agree_on_interface(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "50-cloud-init"
            config.write_text("auto eth0\niface eth0 inet dhcp\n")
            (root / "dhclient.eth0.pid").write_text("513\n")
            proc = root / "proc"
            (proc / "513").mkdir(parents=True)
            (proc / "513" / "cmdline").write_bytes(
                b"/sbin/dhclient\0-4\0-pf\0/run/dhclient.eth0.pid\0eth0\0")
            self.assertTrue(confirmed_ifupdown_dhcp("eth0", config, root, proc))
            self.assertFalse(confirmed_ifupdown_dhcp("eth1", config, root, proc))
            config.write_text("iface eth0 inet static\n")
            self.assertFalse(confirmed_ifupdown_dhcp("eth0", config, root, proc))

    def test_short_password_is_reused_for_rescue_and_web(self):
        with patch("install.timed_getpass", side_effect=["Abcde12345", "Abcde12345"]):
            password = require_web_password()
        self.assertEqual(password, "Abcde12345")
        with patch("install.timed_getpass", side_effect=["short", "short"]):
            with self.assertRaisesRegex(ValueError, "10"):
                require_web_password()

        preview = {"disk": "/dev/vda", "boot_mode": "bios", "network": {"mode": "dhcp"}}
        image = {"raw_sha256": "a" * 64, "sha256": "b" * 64}
        captured = {}

        def record_run(*args, **kwargs):
            if args[0][0] == "bash":
                captured["rescue"] = kwargs["env"]["IKUAI_RESCUE_PASSWORD"]
                captured["profile"] = json.loads(Path(kwargs["env"]["IKUAI_PROFILE"]).read_text())

        with patch("install.os.geteuid", return_value=0), \
             patch("install.stage", side_effect=lambda path: path), \
             patch("install.subprocess.run", side_effect=record_run):
            execute(preview, "https://example.invalid/image.gz", image, password)
        self.assertEqual(captured["rescue"], password)
        self.assertEqual(captured["profile"]["web_password_md5"],
                         hashlib.md5(password.encode()).hexdigest())
        self.assertNotIn(password, json.dumps(captured["profile"]))

    def test_password_prompt_timeout_generates_visible_ten_character_password(self):
        for replies in ([TimeoutError()], ["Abcde12345", TimeoutError()]):
            with self.subTest(replies=replies), \
                 patch("install.timed_getpass", side_effect=replies), \
                 patch("install.generate_password", return_value="Qr7Stu8Vw9"), \
                 patch("builtins.print") as output:
                password = require_web_password()
                self.assertEqual(password, "Qr7Stu8Vw9")
                self.assertIn("Qr7Stu8Vw9", str(output.call_args_list))

    def test_tencent_nat_metadata_must_match_network_and_expected_public_ip(self):
        network = {"mac": "52:54:00:12:34:56", "mode": "dhcp",
                   "address": "10.3.0.2", "prefix": 22, "gateway": "10.3.0.1"}
        metadata = {"local-ipv4": "10.3.0.2", "public-ipv4": "8.8.8.8",
                    "network/interfaces/macs/": "52:54:00:12:34:56/"}
        with patch("install.tencent_metadata", side_effect=lambda key: metadata[key]), \
             patch("install.external_ipv4", return_value="8.8.8.8"):
            self.assertEqual(verify_tencent_nat(network, "8.8.8.8"), "8.8.8.8")
            with self.assertRaisesRegex(ValueError, "public IP"):
                verify_tencent_nat(network, "8.8.4.4")
            metadata["local-ipv4"] = "10.3.0.3"
            with self.assertRaisesRegex(ValueError, "private IP"):
                verify_tencent_nat(network, "8.8.8.8")

    def test_preflight_reads_link_address_alongside_ipv4(self):
        addresses = [{"ifname": "ens3", "address": "52:54:00:12:34:56",
                      "addr_info": [{"family": "inet", "local": "10.0.2.15",
                                     "prefixlen": 24, "dynamic": True}]}]
        routes = [{"dst": "default", "dev": "ens3", "gateway": "10.0.2.2",
                   "protocol": "dhcp"}]

        def command(*args):
            if args == ("ip", "-j", "addr", "show"):
                return addresses
            if args == ("ip", "-j", "-4", "route", "show", "default"):
                return routes
            raise AssertionError(f"unexpected command: {args}")

        with patch("install.command_json", side_effect=command):
            with self.assertRaisesRegex(ValueError, "physical or paravirtual NIC"):
                preflight(allow_test_ip=True)

    def test_plain_boot_disk_selection(self):
        self.assertEqual(select_disk(block_fixture(), "bios"), ("/dev/vda", 10 * 1024**3))

    def test_uefi_requires_esp_on_same_disk(self):
        with self.assertRaisesRegex(ValueError, "UEFI"):
            select_disk(block_fixture(), "uefi")

    def test_lvm_root_rejected(self):
        data = block_fixture()
        data["blockdevices"][0]["children"][0]["type"] = "lvm"
        with self.assertRaisesRegex(ValueError, "plain disk"):
            select_disk(data, "bios")

    def test_release_asset_is_exact_mode_and_name(self):
        manifest = {"images": {"bios": {"filename": "ikuai-3.7.16-bios.raw.gz",
                                         "sha256": "a" * 64, "raw_sha256": "b" * 64}}}
        url, entry = image_from_manifest(
            "https://github.com/user/project/releases/download/v1/manifest.json", manifest, "bios")
        self.assertEqual(url, "https://github.com/user/project/releases/download/v1/ikuai-3.7.16-bios.raw.gz")
        manifest["images"]["bios"]["filename"] = "../../bad.raw.gz"
        with self.assertRaises(ValueError):
            image_from_manifest("https://github.com/user/project/releases/download/v1/manifest.json", manifest, "bios")

    def test_patched_rescue_never_streams_unverified_image(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = stage(Path(temp) / "vendor")
            trans = (directory / "trans.sh").read_text()
            reinstall = (directory / "reinstall.sh").read_text()
            self.assertNotIn("wget $img -O- | pipe_extract >/dev/$xda", trans)
            self.assertLess(trans.index("sha256sum -c /configs/ikuai-image.sha256"),
                            trans.index("gzip -dc /tmp/ikuai.raw.gz | dd"))
            self.assertIn("target disk changed", trans)
            self.assertIn("boot mode changed", trans)
            self.assertIn("python3 /configs/apply_profile.py", trans)
            self.assertIn('password="${IKUAI_RESCUE_PASSWORD:?missing rescue password}"', reinstall)
            self.assertIn('confhome_cn="file://$IKUAI_VENDOR_DIR"', reinstall)
            self.assertNotIn("https://cnb.cool/bin456789/reinstall/-/git/raw/main", reinstall)
            self.assertIn("17fbd3efd16f2e7cb1b945f06423b37399ba7d5ac8cdd3665e7f13f363500e85", reinstall)
            self.assertIn("41d55a3fd650c99f5fa2a9dfed1eff3b441186831fd4f8d2223f717d8a28217d", reinstall)
            self.assertIn("gzip -dc /tmp/ikuai.raw.gz | dd of=/dev/$xda bs=4M conv=fsync status=none", trans)
            self.assertIn('if [ -f /configs/ikuai-profile.json ]; then printf "%s\\n" /dev/console; return; fi', trans)
            self.assertIn('IKUAI_BOOT_MODE" = uefi', reinstall)
            self.assertIn('e44db656df17e33697bb8a177354a6b2ac19309bc593ff88a222ccf88fef2a57', reinstall)
            self.assertIn('pinned iKuai UEFI boot entry', trans)
            self.assertIn('blockdev --rereadpt "/dev/$xda"', trans)
            self.assertIn('mount -o loop,ro,offset=1048576', trans)
            self.assertIn('test -f /tmp/ikuai-efi-check/EFI/BOOT/BOOTX64.EFI', trans)
            self.assertIn('efibootmgr --create --disk "/dev/$xda" --part 1', trans)
            self.assertIn('read -r -p \'[y/N]: \'', reinstall)

    def test_rescue_web_viewer_is_staged_and_prints_public_address(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = stage(Path(temp) / "vendor")
            trans = (directory / "trans.sh").read_text()
            reinstall = (directory / "reinstall.sh").read_text()
            page = (Path(__file__).resolve().parents[1] / "rescue_logviewer.html").read_text()
            self.assertIn("new WebSocket", page)
            self.assertIn('cp /configs/rescue_logviewer.html "/tmp/web$web_path"', trans)
            self.assertNotIn('if [ -f /configs/ikuai-profile.json ]; then return; fi\n    total_ram=', trans)
            self.assertIn('IKUAI_PUBLIC_IP:-IP', reinstall)
            self.assertIn('cp "$IKUAI_LOGVIEWER" $initrd_dir/configs/rescue_logviewer.html', reinstall)

        preview = {"disk": "/dev/vda", "boot_mode": "bios", "public_ip": "8.8.8.8",
                   "network": {"mode": "dhcp"}}
        image = {"raw_sha256": "a" * 64, "sha256": "b" * 64}
        captured = {}

        def record_run(*args, **kwargs):
            if args[0][0] == "bash":
                captured["command"] = args[0]
                captured["environment"] = kwargs["env"]

        with patch("install.os.geteuid", return_value=0), \
             patch("install.stage", side_effect=lambda path: path), \
             patch("install.subprocess.run", side_effect=record_run):
            execute(preview, "https://example.invalid/image.gz", image, "Abcde12345")
        self.assertEqual(captured["environment"]["IKUAI_PUBLIC_IP"], "8.8.8.8")
        self.assertIn("--web-port", captured["command"])
        self.assertEqual(captured["command"][captured["command"].index("--web-port") + 1], "443")

    def test_execute_reboots_only_after_successful_staging(self):
        preview = {"disk": "/dev/vda", "boot_mode": "bios", "network": {"mode": "dhcp"}}
        image = {"raw_sha256": "a" * 64, "sha256": "b" * 64}
        with patch("install.os.geteuid", return_value=0), \
             patch("install.stage", side_effect=lambda path: path), \
             patch("install.subprocess.run") as run:
            execute(preview, "https://example.invalid/image.gz", image, "c" * 32)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.args[0], ["reboot"])
            self.assertTrue(run.call_args.kwargs["check"])

        with patch("install.os.geteuid", return_value=0), \
             patch("install.stage", side_effect=lambda path: path), \
             patch("install.subprocess.run", side_effect=RuntimeError("stage failed")) as run:
            with self.assertRaisesRegex(RuntimeError, "stage failed"):
                execute(preview, "https://example.invalid/image.gz", image, "c" * 32)
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
