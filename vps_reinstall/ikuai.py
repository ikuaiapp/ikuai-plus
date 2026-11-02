"""Strict network selection and iKuai 3.7.16 first-boot configuration."""

import ipaddress
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

LAN_UNBOUND = ",".join(["00:00:00:00:00:00"] * 8)


@dataclass(frozen=True)
class NetworkProfile:
    mac: str
    mode: str
    address: str
    prefix: int
    gateway: str


def select_network(addresses, routes, *, allow_test_ip=False, allow_cloud_nat_dhcp=False,
                   allow_cloud_nat_static=False, dhcp_source_confirmed=False):
    defaults = [route for route in routes if route.get("dst") == "default"]
    if len(defaults) != 1:
        raise ValueError("expected exactly one IPv4 default route")
    route = defaults[0]
    candidates = [item for item in addresses if item.get("ifname") == route.get("dev")]
    if len(candidates) != 1:
        raise ValueError("default route interface is ambiguous")
    interface = candidates[0]
    ipv4 = [item for item in interface.get("addr_info", []) if item.get("family") == "inet"]
    if len(ipv4) != 1:
        raise ValueError("expected one IPv4 address on the default route interface")
    address = ipv4[0]
    prefix = int(address["prefixlen"])
    if prefix < 1 or prefix >= 31:
        raise ValueError("unsupported IPv4 prefix; /31 and /32 require provider-specific routing")
    local = ipaddress.IPv4Address(address["local"])
    cloud_nat = allow_cloud_nat_dhcp or allow_cloud_nat_static
    if cloud_nat:
        if not local.is_private:
            raise ValueError("cloud NAT requires a private IPv4 address")
    elif not allow_test_ip and not local.is_global:
        raise ValueError("source IPv4 is not public; a 1:1 NAT/cloud route needs manual support")
    gateway = ipaddress.IPv4Address(route["gateway"])
    subnet = ipaddress.IPv4Network(f"{local}/{prefix}", strict=False)
    if gateway not in subnet:
        raise ValueError("gateway is outside the source IPv4 subnet")
    mac = interface.get("address", "").lower()
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        raise ValueError("invalid physical MAC")
    octets = bytes.fromhex(mac.replace(":", ""))
    if octets == b"\0" * 6 or octets[0] & 1:
        raise ValueError("invalid unicast MAC")
    dynamic = bool(address.get("dynamic")) or "dynamic" in address.get("flags", [])
    dhcp_route = route.get("protocol") == "dhcp"
    if cloud_nat:
        if dhcp_route or dhcp_source_confirmed:
            if not allow_cloud_nat_dhcp or route.get("protocol") not in (None, "dhcp"):
                raise ValueError("cloud NAT requires a verified DHCP source")
            mode = "dhcp"
        else:
            if not allow_cloud_nat_static:
                raise ValueError("cloud NAT requires a verified DHCP source")
            if dynamic or route.get("protocol") not in (None, "static"):
                raise ValueError("cloud NAT static source network state is ambiguous")
            mode = "static"
    else:
        if dynamic != dhcp_route:
            raise ValueError("DHCP/static source network state is ambiguous")
        mode = "dhcp" if dhcp_route else "static"
    return NetworkProfile(mac, mode, str(local), prefix, str(gateway))


def provision_database(path, profile, password_md5, https_port=443):
    if not re.fullmatch(r"[0-9a-f]{32}", password_md5):
        raise ValueError("expected an MD5 password digest, not a plaintext password")
    if not 1 <= https_port <= 65535:
        raise ValueError("invalid HTTPS port")
    if profile.mode not in ("dhcp", "static"):
        raise ValueError("unsupported WAN access mode")
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    mask = str(ipaddress.IPv4Network(f"0.0.0.0/{profile.prefix}").netmask)
    ip_mask = f"{profile.address}/{mask}" if profile.mode == "static" else ""
    gateway = profile.gateway if profile.mode == "static" else ""
    con = sqlite3.connect(path)
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("source config.db failed integrity check")
        with con:
            updates = (
                ("UPDATE wan_config SET bandif=?,internet=?,ip_mask=?,gateway=?,default_route=0 WHERE name='wan1'",
                 (profile.mac, 1 if profile.mode == "dhcp" else 0, ip_mask, gateway)),
                ("UPDATE lan_config SET bandif=?,bandflag='' WHERE name='lan1'", (LAN_UNBOUND,)),
                ("UPDATE remote_control SET open_wanweb=1,open_sshd=0,force_https=1,https_port=? WHERE id=1",
                 (https_port,)),
                ("UPDATE webuser SET passwd=? WHERE username='admin' AND enabled='yes'", (password_md5,)),
            )
            for statement, args in updates:
                if con.execute(statement, args).rowcount != 1:
                    raise ValueError(f"unexpected config row count: {statement.split()[1]}")
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("provisioned config.db failed integrity check")
    finally:
        con.close()
