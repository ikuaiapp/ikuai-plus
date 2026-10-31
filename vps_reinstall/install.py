"""Preflight a Linux VPS, then stage a verified iKuai rescue reinstall.

Preflight is read-only. The explicit --execute path stages a rescue boot on the
source disk; the second stage rewrites the disk only after image verification.
"""

import argparse
import getpass
import hashlib
import ipaddress
import json
import os
import re
import secrets
import signal
import stat
import string
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from ikuai import select_network
from stage_rescue import HERE, stage


def command_json(*args):
    return json.loads(subprocess.check_output(args, text=True))


def walk_blockdev(nodes):
    for node in nodes:
        yield node
        yield from walk_blockdev(node.get("children") or [])


def select_disk(blocks, boot_mode):
    nodes = list(walk_blockdev(blocks["blockdevices"]))
    roots = [n for n in nodes if "/" in (n.get("mountpoints") or [])]
    if len(roots) != 1 or roots[0].get("type") != "part":
        raise ValueError("root filesystem must be on one plain disk partition")
    by_name = {n["name"]: n for n in nodes}
    root = roots[0]
    parent = by_name.get(root.get("pkname"))
    if not parent or parent.get("type") != "disk":
        raise ValueError("LVM, RAID, and nested boot disks are not supported")
    if boot_mode == "uefi":
        esp = [n for n in nodes if "/boot/efi" in (n.get("mountpoints") or [])]
        if len(esp) != 1 or esp[0].get("pkname") != parent["name"]:
            raise ValueError("UEFI system partition is not on the system disk")
    size = int(parent["size"])
    if size < 8 * 1024**3 or size > 2 * 1024**4:
        raise ValueError("system disk must be 8 GiB through 2 TiB")
    if int(parent["log-sec"]) != 512:
        raise ValueError("only 512-byte logical sectors are supported")
    path = parent["path"]
    if not re.fullmatch(r"/dev/[a-zA-Z0-9_-]+", path):
        raise ValueError("unexpected system disk path")
    return path, size


def tencent_metadata(key):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://169.254.0.23/latest/meta-data/{key}", timeout=3) as response:
        if response.geturl() != f"http://169.254.0.23/latest/meta-data/{key}":
            raise ValueError("Tencent metadata redirected")
        value = response.read(257)
    if len(value) > 256:
        raise ValueError("Tencent metadata value is too long")
    return value.decode("ascii").strip()


def external_ipv4():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("https://api.ipify.org?format=json", timeout=10) as response:
        return str(ipaddress.IPv4Address(json.loads(response.read(256))["ip"]))


def oracle_metadata_vnics():
    url = "http://169.254.169.254/opc/v2/vnics/"
    request = urllib.request.Request(url, headers={"Authorization": "Bearer Oracle"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=3) as response:
        if response.geturl() != url:
            raise ValueError("Oracle VNIC metadata redirected")
        payload = response.read(65537)
    if len(payload) > 65536:
        raise ValueError("Oracle VNIC metadata is too large")
    vnics = json.loads(payload)
    if not isinstance(vnics, list):
        raise ValueError("Oracle VNIC metadata is not a list")
    return vnics


def verify_oracle_nat(network, expected_public_ip):
    expected = ipaddress.IPv4Address(expected_public_ip)
    if not expected.is_global or network["mode"] != "dhcp":
        raise ValueError("Oracle NAT requires a public IP and DHCP WAN")
    if external_ipv4() != str(expected):
        raise ValueError("Oracle public egress does not match the expected public IP")
    vnics = oracle_metadata_vnics()
    if len(vnics) != 1 or not isinstance(vnics[0], dict):
        raise ValueError("Oracle VNIC metadata must identify one primary interface")
    vnic = vnics[0]
    expected_subnet = str(ipaddress.IPv4Network(
        f"{network['address']}/{network['prefix']}", strict=False))
    if (str(vnic.get("macAddr", "")).lower() != network["mac"] or
            vnic.get("privateIp") != network["address"] or
            vnic.get("subnetCidrBlock") != expected_subnet or
            vnic.get("virtualRouterIp") != network["gateway"]):
        raise ValueError("Oracle VNIC metadata does not match the default route NIC")
    return str(expected)


def has_rescue_memory(total_bytes):
    # A nominal 1 GB VPS often exposes about 968 MiB after firmware reservation.
    return total_bytes >= 896 * 1024**2


def verify_tencent_nat(network, expected_public_ip):
    expected = ipaddress.IPv4Address(expected_public_ip)
    if not expected.is_global or network["mode"] != "dhcp":
        raise ValueError("Tencent NAT requires a public IP and DHCP WAN")
    public = tencent_metadata("public-ipv4")
    if public != str(expected) or external_ipv4() != str(expected):
        raise ValueError("Tencent public IP metadata or egress does not match expected public IP")
    if tencent_metadata("local-ipv4") != network["address"]:
        raise ValueError("Tencent private IP metadata does not match the default route NIC")
    macs = tencent_metadata("network/interfaces/macs/").splitlines()
    if network["mac"] + "/" not in macs:
        raise ValueError("Tencent NIC MAC metadata does not match the default route NIC")
    return public


def confirmed_ifupdown_dhcp(dev, config=None,
                            run_dir=Path("/run"), proc_dir=Path("/proc")):
    if not isinstance(dev, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", dev):
        return False
    try:
        if config is None:
            configs = [Path("/etc/network/interfaces")]
            configs.extend(sorted(Path("/etc/network/interfaces.d").glob("*")))
        elif isinstance(config, (str, Path)):
            configs = [Path(config)]
        else:
            configs = [Path(item) for item in config]
        matches = [parts for path in configs if path.is_file()
                   for line in path.read_text().splitlines()
                   if (parts := line.split())[:2] == ["iface", dev]]
        if matches != [["iface", dev, "inet", "dhcp"]]:
            return False
        pid = int((run_dir / f"dhclient.{dev}.pid").read_text().strip())
        if pid <= 1:
            return False
        args = [arg for arg in (proc_dir / str(pid) / "cmdline").read_bytes().split(b"\0") if arg]
        return bool(args and Path(os.fsdecode(args[0])).name == "dhclient" and
                    args[-1] == os.fsencode(dev))
    except (OSError, UnicodeError, ValueError):
        return False


def preflight(*, allow_test_ip=False, tencent_nat_public_ip=None,
              oracle_nat_public_ip=None):
    if os.uname().machine != "x86_64" or sys.platform != "linux":
        raise ValueError("only x86-64 Linux sources are supported")
    if not has_rescue_memory(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")):
        raise ValueError("rescue stage requires at least 896 MiB of usable physical RAM")
    if tencent_nat_public_ip and oracle_nat_public_ip:
        raise ValueError("select only one cloud NAT provider")
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        raise ValueError("run on the VPS host, not inside a container")
    mode = "uefi" if Path("/sys/firmware/efi").exists() else "bios"
    addresses = command_json("ip", "-j", "addr", "show")
    routes = command_json("ip", "-j", "-4", "route", "show", "default")
    route_dev = next((route.get("dev") for route in routes if route.get("dst") == "default"), None)
    network = select_network(addresses, routes, allow_test_ip=allow_test_ip,
                             allow_cloud_nat_dhcp=bool(tencent_nat_public_ip or oracle_nat_public_ip),
                             dhcp_source_confirmed=(bool(tencent_nat_public_ip or oracle_nat_public_ip) and
                                                    confirmed_ifupdown_dhcp(route_dev)))
    public_ip = None
    if tencent_nat_public_ip:
        public_ip = verify_tencent_nat(vars(network), tencent_nat_public_ip)
    elif oracle_nat_public_ip:
        public_ip = verify_oracle_nat(vars(network), oracle_nat_public_ip)
    elif not allow_test_ip:
        public_ip = external_ipv4()
        if public_ip != network.address:
            raise ValueError("external IPv4 differs from the NIC address; NAT or proxy is unsupported")
    dev = next(route["dev"] for route in routes if route.get("dst") == "default")
    if not Path("/sys/class/net", dev, "device").exists():
        raise ValueError("default route is not on a physical or paravirtual NIC")
    blocks = command_json("lsblk", "-b", "-J", "-o",
                          "NAME,TYPE,PATH,PKNAME,SIZE,LOG-SEC,MOUNTPOINTS")
    disk, size = select_disk(blocks, mode)
    if not stat.S_ISBLK(os.stat(disk).st_mode):
        raise ValueError("selected system disk is not a block device")
    return {"boot_mode": mode, "disk": disk, "disk_bytes": size,
            "nic": dev, "network": vars(network), "public_ip": public_ip,
            "provider": ("tencent-nat" if tencent_nat_public_ip else
                         "oracle-nat" if oracle_nat_public_ip else "direct")}


def load_manifest(url, expected_sha256):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ValueError("manifest must be served over HTTPS by GitHub Releases")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("manifest SHA-256 is required")
    with urllib.request.urlopen(url, timeout=30) as response:
        if urllib.parse.urlparse(response.geturl()).scheme != "https":
            raise ValueError("manifest redirected away from HTTPS")
        payload = response.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024 or hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError("manifest failed SHA-256 or size validation")
    manifest = json.loads(payload)
    if manifest.get("format") != "ikuai-cloud-v1":
        raise ValueError("unknown image manifest format")
    return manifest


def image_from_manifest(manifest_url, manifest, mode):
    entry = manifest["images"][mode]
    name = entry["filename"]
    if not re.fullmatch(r"ikuai-3\.7\.16-(?:bios|uefi)\.raw\.gz", name):
        raise ValueError("unexpected image asset name")
    if mode not in name or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
        raise ValueError("image manifest mode or compressed digest mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", entry["raw_sha256"]):
        raise ValueError("raw image digest is missing")
    base = manifest_url.rsplit("/", 1)[0]
    if not re.fullmatch(r"https://github\.com/[^/]+/[^/]+/releases/download/[^/]+", base):
        raise ValueError("unexpected GitHub Release path")
    return base + "/" + name, entry


def timed_getpass(prompt):
    def timeout(_signum, _frame):
        raise TimeoutError("password input timed out")

    old_handler = signal.signal(signal.SIGALRM, timeout)
    try:
        signal.alarm(30)
        return getpass.getpass(prompt)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def generate_password():
    alphabet = string.ascii_letters + string.digits
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(10))
        if any(char.isalpha() for char in password) and any(char.isdigit() for char in password):
            return password


def require_web_password():
    try:
        first = timed_getpass("New iKuai Web admin and rescue SSH password (10 chars; 30s timeout): ")
        second = timed_getpass("Repeat password (30s timeout): ")
    except TimeoutError:
        password = generate_password()
        print(f"No password entered within 30 seconds; generated Web admin and rescue SSH password: {password}", flush=True)
        return password
    if (first != second or len(first) != 10 or not first.isascii() or
            not first.isprintable() or not any(char.isalpha() for char in first) or
            not any(char.isdigit() for char in first)):
        raise ValueError("passwords must match and contain exactly 10 visible ASCII characters, including letters and digits")
    return first


def execute(preview, image_url, image, password):
    if os.geteuid() != 0:
        raise PermissionError("--execute requires root")
    rescue_password = password
    web_md5 = hashlib.md5(password.encode()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="ikuai-vps-stage-") as directory:
        root = Path(directory)
        vendor = stage(root / "vendor")
        profile = root / "profile.json"
        payload = {"disk_basename": Path(preview["disk"]).name,
                   "boot_mode": preview["boot_mode"],
                   "raw_sha256": image["raw_sha256"],
                   "network": preview["network"],
                   "web_password_md5": web_md5}
        profile.write_text(json.dumps(payload))
        profile.chmod(0o600)
        env = os.environ.copy()
        env.update(IKUAI_VENDOR_DIR=str(vendor), IKUAI_PROFILE=str(profile),
                   IKUAI_APPLY=str(HERE / "apply_profile.py"),
                   IKUAI_LIB=str(HERE / "ikuai.py"),
                   IKUAI_LOGVIEWER=str(HERE / "rescue_logviewer.html"),
                   IKUAI_PUBLIC_IP=preview.get("public_ip") or "",
                   IKUAI_DISK_BASENAME=payload["disk_basename"],
                   IKUAI_BOOT_MODE=payload["boot_mode"],
                   IKUAI_COMPRESSED_SHA256=image["sha256"],
                   IKUAI_RESCUE_PASSWORD=rescue_password)
        print("Rescue SSH user: root; password: same as the new iKuai Web admin password", flush=True)
        print("Rescue will reboot once, download and verify the image, write the selected disk, "
              "configure WAN/Web, then reboot. A failure leaves rescue SSH active.", flush=True)
        subprocess.run(["bash", str(vendor / "reinstall.sh"), "dd", "--img", image_url,
                        "--target-disk", preview["disk"], "--web-port", "443", "--hold", "2"],
                       env=env, check=True)
        subprocess.run(["reboot"], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-url", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--execute", action="store_true",
                        help="confirm and stage an irreversible system-disk rewrite")
    parser.add_argument("--tencent-nat-public-ip",
                        help="opt in to Tencent Cloud DHCP/NAT after metadata and public IP checks")
    parser.add_argument("--oracle-nat-public-ip",
                        help="opt in to Oracle Cloud DHCP/NAT after VNIC metadata and public egress checks")
    args = parser.parse_args()
    preview = preflight(tencent_nat_public_ip=args.tencent_nat_public_ip,
                        oracle_nat_public_ip=args.oracle_nat_public_ip)
    manifest = load_manifest(args.manifest_url, args.manifest_sha256)
    image_url, image = image_from_manifest(args.manifest_url, manifest, preview["boot_mode"])
    print(json.dumps({"preflight": preview, "image": image_url,
                      "compressed_sha256": image["sha256"]}, indent=2, ensure_ascii=False))
    print("Confirm the cloud security group allows inbound TCP 443 to this VPS.")
    if not args.execute:
        print("Preflight only: no reboot or disk write. Use --execute to continue.")
        return
    password = require_web_password()
    answer = input(f"Type ERASE {preview['disk']} to destroy that disk and stage rescue: ")
    if answer != f"ERASE {preview['disk']}":
        raise ValueError("confirmation did not match; no changes made")
    execute(preview, image_url, image, password)


if __name__ == "__main__":
    main()
