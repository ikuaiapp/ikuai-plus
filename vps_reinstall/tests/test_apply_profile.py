import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apply_profile import apply_profile, check_layout, disk_size, ensure_partition_device, provision_identity


class ApplyProfileTests(unittest.TestCase):
    def test_rescue_populates_partition_device_after_table_reread(self):
        with patch("apply_profile.Path.is_block_device", side_effect=[False, True]), \
             patch("apply_profile.run") as run:
            ensure_partition_device("/dev/vda6")
            run.assert_called_once_with("mdev", "-s")

    def test_missing_partition_device_stops_before_resize(self):
        with patch("apply_profile.Path.is_block_device", return_value=False), \
             patch("apply_profile.run") as run:
            with self.assertRaisesRegex(RuntimeError, "partition device"):
                ensure_partition_device("/dev/vda6")
            run.assert_called_once_with("mdev", "-s")

    def test_bad_image_digest_does_not_modify_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            disk = Path(tmp) / "disk.raw"
            disk.write_bytes(b"unchanged" + bytes(1015))
            profile = Path(tmp) / "profile.json"
            profile.write_text(json.dumps({"disk_basename": "disk.raw",
                                           "raw_sha256": "0" * 64}))
            before = disk.read_bytes()
            with patch("apply_profile.IMAGE_BYTES", 1024):
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    apply_profile(str(disk), str(profile))
            self.assertEqual(disk.read_bytes(), before)

    def test_rescue_creates_unique_identity_after_verified_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            disk = Path(tmp) / "disk.raw"
            with disk.open("wb") as handle:
                handle.seek(104448 * 512)
                handle.write(b"www.ikuai8.com\0\0" + bytes(16) + b"\1\0\0\0")
            provision_identity(str(disk))
            with disk.open("rb") as handle:
                handle.seek(104448 * 512 + 16)
                first = handle.read(16)
            self.assertNotEqual(first, bytes(16))
            with self.assertRaises(ValueError):
                provision_identity(str(disk))

    def test_bios_image_profile_and_growth(self):
        source = Path("/tmp/ikuai-release/ikuai-3.7.16-bios.raw")
        if not source.exists():
            self.skipTest("BIOS image build not available")
        with tempfile.TemporaryDirectory(prefix="ikuai-apply-test-") as directory:
            disk = Path(directory) / "test.raw"
            subprocess.run(["cp", "--reflink=auto", "--sparse=always", source, disk], check=True)
            disk.open("r+b").close()
            with disk.open("r+b") as handle:
                handle.truncate(9 * 1024**3)
            with source.open("rb") as handle:
                source_hash = hashlib.file_digest(handle, "sha256").hexdigest()
            profile = {
                "disk_basename": disk.name,
                "raw_sha256": source_hash,
                "network": {"mac": "52:54:00:aa:bb:cc", "mode": "static",
                            "address": "192.168.9.10", "prefix": 24, "gateway": "192.168.9.1"},
                "web_password_md5": "a" * 32,
            }
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(profile))
            result = apply_profile(str(disk), str(path))
            self.assertEqual(result["sectors"], 9 * 1024**3 // 512)
            table = json.loads(subprocess.check_output(["/sbin/sfdisk", "-J", disk]))["partitiontable"]
            self.assertEqual(table["partitions"][5]["size"], result["sectors"] - 8_609_792)
            part = Path(directory) / "config.ext4"
            db = Path(directory) / "config.db"
            subprocess.run(["dd", f"if={disk}", f"of={part}", "bs=512", "skip=114688",
                            "count=102400", "status=none"], check=True)
            subprocess.run(["/sbin/debugfs", "-R", f"dump /ikuai/config.db {db}", str(part)], check=True)
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT bandif, internet, gateway FROM wan_config WHERE name='wan1'").fetchone(),
                             ("52:54:00:aa:bb:cc", 0, "192.168.9.1"))
            self.assertEqual(con.execute("SELECT open_wanweb,open_sshd FROM remote_control WHERE id=1").fetchone(), (1, 0))
            self.assertEqual(con.execute("SELECT passwd FROM webuser WHERE username='admin'").fetchone()[0], "a" * 32)
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            con.close()


if __name__ == "__main__":
    unittest.main()
