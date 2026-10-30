import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from make_manifest import create_manifest


class ManifestTests(unittest.TestCase):
    def test_round_trip_and_identity_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefixes = {}
            for mode in ("bios", "uefi"):
                prefix = root / f"ikuai-3.7.16-{mode}"
                prefixes[mode] = prefix
                body = bytearray(1024)
                body[0] = 1 if mode == "bios" else 2
                Path(str(prefix) + ".raw").write_bytes(body)
                with gzip.open(str(prefix) + ".raw.gz", "wb") as target:
                    target.write(body)
            output = root / "manifest.json"
            with patch("make_manifest.RAW_SIZE", 1024), patch("make_manifest.IDENTITY_OFFSET", 16):
                digest = create_manifest(prefixes["bios"], prefixes["uefi"], output)
                self.assertEqual(len(digest), 64)
                manifest = json.loads(output.read_text())
                self.assertEqual(manifest["format"], "ikuai-cloud-v1")
                self.assertNotEqual(manifest["images"]["bios"]["raw_sha256"],
                                    manifest["images"]["uefi"]["raw_sha256"])
                with self.assertRaises(ValueError):
                    create_manifest(prefixes["bios"], prefixes["uefi"], output)
                body[16] = 1
                Path(str(prefixes["uefi"]) + ".raw").write_bytes(body)
                with self.assertRaises(ValueError):
                    create_manifest(prefixes["bios"], prefixes["uefi"], root / "bad.json")


if __name__ == "__main__":
    unittest.main()
