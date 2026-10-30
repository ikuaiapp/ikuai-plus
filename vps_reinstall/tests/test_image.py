import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_image import cloud_account_script, patch_bootstrap, rebuild_config_partition, sanitize_database, sanitize_identity_partition, split_layout


class ImageTests(unittest.TestCase):
    def test_source_market_account_setup_does_not_create_fixed_ssh_login(self):
        script = cloud_account_script()
        self.assertNotIn(b"iksshd", script)
        self.assertNotIn(b"sanjin", script)
        self.assertIn(b"exit 0", script)

    def test_bootstrap_does_not_require_fixed_ssh_account(self):
        original = (b'if [ "$(readlink "$PAGE")" = /tmp/ikpkg/pgstore/html ] &&\n'
                    b'       [ "$(readlink "$API")" = /tmp/ikpkg/pgstore/script/service.sh ] &&\n'
                    b"       grep -q '^iksshd:' /etc/passwd; then\n")
        patched = patch_bootstrap(original)
        self.assertNotIn(b"iksshd", patched)
        self.assertIn(b"/tmp/ikpkg/pgstore/script/service.sh", patched)

    def test_split_layout_keeps_log_at_four_gib_and_data_after_it(self):
        layout = split_layout(16_777_216)
        self.assertEqual(layout[5], (219_136, 8_388_608))
        self.assertEqual(layout[6], (8_609_792, 8_167_424))

    def test_split_layout_rejects_small_disk(self):
        with self.assertRaises(ValueError):
            split_layout(10_000_000)

    def test_public_image_removes_installer_identity(self):
        source = Path("/tmp/ikuai-bios-stock-p2.img")
        if not source.exists():
            self.skipTest("stock ISO identity fixture not extracted")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "identity.img"
            target.write_bytes(source.read_bytes())
            sanitize_identity_partition(target)
            data = target.read_bytes()
            self.assertEqual(data[:16], b"www.ikuai8.com\0\0")
            self.assertEqual(data[16:32], bytes(16))
            self.assertEqual(data[32:36], b"\1\0\0\0")
            self.assertFalse(any(data[36:]))

    def test_public_template_has_no_active_web_or_fixed_admin_password(self):
        source = Path("/tmp/ikuai-clean-config.db")
        if not source.exists():
            self.skipTest("clean ISO config fixture not extracted")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "config.db"
            db.write_bytes(source.read_bytes())
            sanitize_database(db)
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("select bandif from lan_config").fetchone()[0], "")
            self.assertEqual(con.execute("select bandif from wan_config").fetchone()[0], "")
            self.assertEqual(con.execute("select open_wanweb,open_sshd from remote_control").fetchone(), (0, 0))
            self.assertNotEqual(con.execute("select passwd from webuser").fetchone()[0], "21232f297a57a5a743894a0e4a801fc3")
            self.assertEqual(con.execute("pragma integrity_check").fetchone()[0], "ok")

    def test_config_partition_is_rebuilt_without_source_free_blocks(self):
        source = Path("/tmp/ikuai-bios-stock-p3.ext4")
        if not source.exists():
            self.skipTest("stock ISO config partition fixture not extracted")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.ext4"
            rebuild_config_partition(source, target, Path(tmp))
            import subprocess
            result = subprocess.run(["/sbin/e2fsck", "-fn", str(target)], capture_output=True)
            self.assertIn(result.returncode, (0, 1), result.stderr.decode())
            self.assertNotIn(b"52:54:00:12:34:56", target.read_bytes())
            listing = subprocess.run(["/sbin/debugfs", "-R", "ls -l /ikuai", str(target)],
                                     capture_output=True, text=True, check=True).stdout
            for name in ("config.db", "wpa_ppsk.db", "wecom.db", "camera.db"):
                self.assertIn(name, listing)


if __name__ == "__main__":
    unittest.main()
