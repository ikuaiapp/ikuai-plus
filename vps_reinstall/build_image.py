"""Build sanitized iKuai disk images from an ISO-installed test disk."""

import hashlib
import argparse
import gzip
import io
import json
import lzma
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local_market_build"))
from build_firmware_3716 import ext_read, ext_replace, parse_bin, validate_xz_compatibility
from build_native_pgstore import entry, openssl, package_wrapper, write_tar
from v1_rootfs import decode, encode


MIN_SECTORS = 16_777_216
LOG_START = 219_136
LOG_SECTORS = 8_388_608
DATA_START = LOG_START + LOG_SECTORS + 2_048
IDENTITY_START = 104448
IDENTITY_SECTORS = 10240
CONFIG_FILES = (
    "/ikuai/config.db", "/ikuai/wpa_ppsk.db", "/ikuai/wecom.db",
    "/ikuai/camera.db", "/ikuai/reset_default.ip",
    "/data/libproto.gz", "/data/build_date", "/data/libproto.md5sum",
)


def split_layout(total_sectors):
    if total_sectors < MIN_SECTORS or total_sectors > 2**32:
        raise ValueError("only 8 GiB through 2 TiB MBR templates are supported")
    return {5: (LOG_START, LOG_SECTORS), 6: (DATA_START, total_sectors - DATA_START)}


def cloud_account_script():
    return b"#!/bin/sh\n# Public image: no shared SSH account or password.\nexit 0\n"


def patch_bootstrap(original):
    old = b" &&\n       grep -q '^iksshd:' /etc/passwd; then"
    if original.count(old) != 1:
        raise ValueError("source market bootstrap account guard changed")
    return original.replace(old, b"; then")


def sanitize_database(path):
    con = sqlite3.connect(path)
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("source config.db is corrupt")
        code = con.execute("SELECT code FROM register WHERE id=1").fetchone()
        if code != ("",):
            raise ValueError("source image contains cloud binding")
        con.execute("PRAGMA secure_delete=ON")
        with con:
            updates = (
                ("UPDATE lan_config SET bandif='',bandflag='' WHERE name='lan1'", ()),
                ("UPDATE wan_config SET bandif='',internet=1,ip_mask='',gateway='',default_route=0 WHERE name='wan1'", ()),
                ("UPDATE remote_control SET open_wanweb=0,open_sshd=0,sshd_passwd='',force_https=1,https_port=443 WHERE id=1", ()),
                ("UPDATE webuser SET passwd=? WHERE username='admin' AND enabled='yes'",
                 (hashlib.md5(secrets.token_bytes(32)).hexdigest(),)),
            )
            for statement, args in updates:
                if con.execute(statement, args).rowcount != 1:
                    raise ValueError(f"unexpected template row count: {statement.split()[1]}")
        con.execute("VACUUM")
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("sanitized config.db is corrupt")
    finally:
        con.close()


def call(*args, input_data=None, ok=(0,)):
    result = subprocess.run(args, input=input_data, capture_output=True)
    if result.returncode not in ok:
        raise RuntimeError(f"{' '.join(map(str, args))}: {result.stderr.decode(errors='replace')}")
    return result.stdout


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rewrite_market_package(encrypted):
    plaintext = openssl(encrypted, "-d", "-aes-128-cbc", "-md", "md5", "-pass", "pass:ikuaiwbd")
    marker = plaintext.find(b"\x1f\x8b\x08")
    if marker < 0 or not plaintext[:marker].startswith(b"#!/bin/bash\n"):
        raise ValueError("unexpected source market package wrapper")
    wrapper = plaintext[:marker]
    if wrapper != package_wrapper():
        raise ValueError("source market package wrapper changed")
    files = {}
    modes = {}
    with tarfile.open(fileobj=io.BytesIO(plaintext[marker:]), mode="r:gz") as archive:
        for member in archive:
            if not member.isfile() or member.name in files or member.name.startswith("/") or ".." in Path(member.name).parts:
                raise ValueError("unexpected source market tar entry")
            files[member.name] = archive.extractfile(member).read()
            modes[member.name] = member.mode
    if b"iksshd" not in files.get("account_setup.sh", b""):
        raise ValueError("fixed SSH account setup was not found")
    files["account_setup.sh"] = cloud_account_script()
    files["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(body).hexdigest()}  {name}\n"
        for name, body in sorted(files.items()) if name != "SHA256SUMS"
    ).encode("ascii")
    inner = write_tar([entry(name, body, modes[name]) for name, body in sorted(files.items())])
    output = openssl(wrapper + inner, "-aes-128-cbc", "-md", "md5", "-salt", "-pass", "pass:ikuaiwbd")
    if b"iksshd" in openssl(output, "-d", "-aes-128-cbc", "-md", "md5", "-pass", "pass:ikuaiwbd"):
        raise ValueError("fixed SSH account survived market rewrite")
    return output


def cloud_boot_from_bin(upgrade_bin, directory):
    data = Path(upgrade_bin).read_bytes()
    _, header, payload = parse_bin(data)
    if header.get("version") != "3.7.16" or header.get("sysbit") != "x64":
        raise ValueError("expected the inspected full-source 3.7.16 upgrade BIN")
    boot = directory / "boot.ext2"
    boot.write_bytes(gzip.decompress(payload))
    stock_encoded = ext_read(boot, "/boot/rootfs")
    stock_xz = decode(stock_encoded)
    rootfs = directory / "rootfs.ext4"
    rootfs.write_bytes(lzma.decompress(stock_xz))
    root = "/usr/ikuai/local-market"
    bootstrap = patch_bootstrap(ext_read(rootfs, root + "/bootstrap.sh"))
    packages = ext_read(rootfs, root + "/packages.tar.gz")
    with tarfile.open(fileobj=io.BytesIO(packages), mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) != 1 or members[0].name != "pgstore.bin.pkg":
            raise ValueError("unexpected market package list")
        package = archive.extractfile(members[0]).read()
    rewritten = write_tar([entry("pgstore.bin.pkg", rewrite_market_package(package))])
    files = {"bootstrap.sh": bootstrap, "packages.tar.gz": rewritten}
    files["install.sh"] = ext_read(rootfs, root + "/install.sh")
    files["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(body).hexdigest()}  {name}\n"
        for name, body in sorted(files.items())
    ).encode("ascii")
    for name, body in files.items():
        source = directory / ("new-" + name)
        source.write_bytes(body)
        ext_replace(rootfs, root + "/" + name, source, 0o755 if name.endswith(".sh") else 0o644)
    call("/sbin/e2fsck", "-fn", str(rootfs), ok=(0, 1))
    compressed = lzma.compress(
        rootfs.read_bytes(), format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC32,
        filters=[{"id": lzma.FILTER_LZMA2, "dict_size": 8 * 1024 * 1024}],
    )
    validate_xz_compatibility(stock_xz, compressed)
    encoded = directory / "rootfs.enc"
    encoded.write_bytes(encode(compressed))
    ext_replace(boot, "/boot/rootfs", encoded, 0o644)
    call("/sbin/e2fsck", "-fn", str(boot), ok=(0, 1))
    return boot, encoded


def partition_table(image):
    data = json.loads(call("/sbin/sfdisk", "-J", str(image)))
    if data["partitiontable"]["label"] != "dos":
        raise ValueError("only the official MBR layout is supported")
    partitions = data["partitiontable"]["partitions"]
    expected = [(2048, 102400), (104448, 10240), (114688, 102400),
                (217088, 16560128), (219136, 16558080)]
    if [(part["start"], part["size"]) for part in partitions] != expected:
        raise ValueError("stock ISO partition layout changed")
    return data["partitiontable"]["id"]


def extract_partition(image, output, start, sectors):
    call("dd", f"if={image}", f"of={output}", "bs=512", f"skip={start}",
         f"count={sectors}", "conv=sparse", "status=none")


def write_partition(image, source, start):
    call("dd", f"if={source}", f"of={image}", "bs=512", f"seek={start}",
         "conv=notrunc,sparse", "status=none")


def sanitize_identity_partition(path):
    data = bytearray(Path(path).read_bytes())
    if (len(data) != IDENTITY_SECTORS * 512 or
            data[:16] != b"www.ikuai8.com\0\0" or
            data[16:32] == bytes(16) or
            data[32:36] != b"\1\0\0\0" or any(data[36:])):
        raise ValueError("unexpected source identity partition format")
    data[16:32] = bytes(16)
    Path(path).write_bytes(data)


def rebuild_config_partition(source, target, work):
    extracted = work / "config-files"
    extracted.mkdir()
    for name in CONFIG_FILES:
        destination = extracted / Path(name).name
        call("/sbin/debugfs", "-R", f"dump {name} {destination}", str(source))
        if not destination.is_file():
            raise ValueError(f"required config file missing: {name}")
    sanitize_database(extracted / "config.db")
    for name in ("wpa_ppsk.db", "wecom.db", "camera.db"):
        con = sqlite3.connect(extracted / name)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError(f"invalid auxiliary database: {name}")
            con.execute("VACUUM")
        finally:
            con.close()
    target.touch()
    os.truncate(target, 102400 * 512)
    call("/sbin/mkfs.ext4", "-F", "-m", "0", "-b", "1024", "-I", "128", str(target))
    for directory in ("/ikuai", "/bak", "/data"):
        call("/sbin/debugfs", "-w", "-R", f"mkdir {directory}", str(target))
    for name in CONFIG_FILES:
        call("/sbin/debugfs", "-w", "-R",
             f"write {extracted / Path(name).name} {name}", str(target))
    call("/sbin/e2fsck", "-fn", str(target), ok=(0, 1))


def build_disk(stock, upgrade_bin, mode, output):
    stock = Path(stock)
    output = Path(output)
    if output.exists() or stock.resolve() == output.resolve():
        raise ValueError("output must be a new file")
    if mode not in ("bios", "uefi") or stock.stat().st_size != 8 * 1024**3:
        raise ValueError("expected an 8 GiB BIOS or UEFI ISO-installed raw disk")
    disk_id = partition_table(stock)
    with tempfile.TemporaryDirectory(prefix="ikuai-cloud-build-") as temp:
        work = Path(temp)
        boot, encoded = cloud_boot_from_bin(upgrade_bin, work)
        call("cp", "--reflink=auto", "--sparse=always", str(stock), str(output))
        identity = work / "identity.img"
        extract_partition(output, identity, IDENTITY_START, IDENTITY_SECTORS)
        sanitize_identity_partition(identity)
        write_partition(output, identity, IDENTITY_START)
        boot_partition = work / "boot-part.img"
        if mode == "bios":
            write_partition(output, boot, 2048)
        else:
            extract_partition(output, boot_partition, 2048, 102400)
            if b"mkfs.fat" not in boot_partition.read_bytes()[:128]:
                raise ValueError("UEFI template does not have a FAT boot partition")
            call("mdel", "-i", str(boot_partition), "::/boot/rootfs")
            call("mcopy", "-i", str(boot_partition), str(encoded), "::/boot/rootfs")
            write_partition(output, boot_partition, 2048)
        source_config = work / "source-config.ext4"
        extract_partition(output, source_config, 114688, 102400)
        config_partition = work / "config.ext4"
        rebuild_config_partition(source_config, config_partition, work)
        write_partition(output, config_partition, 114688)
        log = work / "log.ext4"
        log.touch()
        os.truncate(log, LOG_SECTORS * 512)
        call("/sbin/mkfs.ext4", "-F", "-m", "0", str(log))
        call("fallocate", "-p", "-o", str(LOG_START * 512),
             "-l", str((MIN_SECTORS - LOG_START) * 512), str(output))
        layout = split_layout(MIN_SECTORS)
        table = (f"label: dos\nlabel-id: {disk_id}\nunit: sectors\n\n"
                 "start=2048,size=102400,type=83\n"
                 "start=104448,size=10240,type=83\n"
                 "start=114688,size=102400,type=83\n"
                 "start=217088,size=16560128,type=5\n"
                 f"start={layout[5][0]},size={layout[5][1]},type=83\n"
                 f"start={layout[6][0]},size={layout[6][1]},type=83\n")
        call("/sbin/sfdisk", "--force", str(output), input_data=table.encode())
        write_partition(output, log, layout[5][0])
        data = work / "data.ext4"
        data.touch()
        os.truncate(data, layout[6][1] * 512)
        call("/sbin/mkfs.ext4", "-F", "-m", "0", str(data))
        write_partition(output, data, layout[6][0])
        if len(json.loads(call("/sbin/sfdisk", "-J", str(output)))["partitiontable"]["partitions"]) != 6:
            raise ValueError("data partition was not created")
    return sha256(output)


def compress_image(raw, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError("compressed image already exists")
    with open(raw, "rb") as source, open(destination, "wb") as target:
        with gzip.GzipFile(fileobj=target, mode="wb", filename="", mtime=0, compresslevel=6) as compressed:
            shutil.copyfileobj(source, compressed, length=4 * 1024 * 1024)
    if destination.stat().st_size >= 2 * 1024**3:
        raise ValueError("GitHub Releases asset must be smaller than 2 GiB")
    return sha256(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-raw", required=True)
    parser.add_argument("--source-bin", required=True)
    parser.add_argument("--mode", choices=("bios", "uefi"), required=True)
    parser.add_argument("--output-prefix", required=True)
    args = parser.parse_args()
    raw = Path(args.output_prefix + ".raw")
    compressed = Path(args.output_prefix + ".raw.gz")
    raw_hash = build_disk(args.stock_raw, args.source_bin, args.mode, raw)
    compressed_hash = compress_image(raw, compressed)
    print(json.dumps({"raw": str(raw), "raw_sha256": raw_hash,
                      "compressed": str(compressed), "compressed_sha256": compressed_hash}, indent=2))
