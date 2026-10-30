import ipaddress
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ikuai import LAN_UNBOUND, NetworkProfile, provision_database, select_network


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.addresses = [{
            "ifname": "ens3", "address": "52:54:00:12:34:56",
            "operstate": "UP", "addr_info": [{
                "family": "inet", "local": "203.0.113.10", "prefixlen": 24,
                "dynamic": False,
            }],
        }]
        self.routes = [{
            "dst": "default", "gateway": "203.0.113.1",
            "dev": "ens3", "protocol": "static", "metric": 100,
        }]

    def test_static_profile_uses_source_mac_and_gateway(self):
        got = select_network(self.addresses, self.routes, allow_test_ip=True)
        self.assertEqual(got, NetworkProfile("52:54:00:12:34:56", "static", "203.0.113.10", 24, "203.0.113.1"))

    def test_dhcp_profile_does_not_pin_lease(self):
        self.addresses[0]["addr_info"][0]["dynamic"] = True
        self.routes[0]["protocol"] = "dhcp"
        self.assertEqual(select_network(self.addresses, self.routes, allow_test_ip=True).mode, "dhcp")

    def test_off_subnet_gateway_is_rejected(self):
        self.routes[0]["gateway"] = "203.0.114.1"
        with self.assertRaisesRegex(ValueError, "gateway"):
            select_network(self.addresses, self.routes, allow_test_ip=True)

    def test_multiple_default_routes_are_rejected(self):
        self.routes.append(dict(self.routes[0]))
        with self.assertRaisesRegex(ValueError, "default route"):
            select_network(self.addresses, self.routes, allow_test_ip=True)

    def test_private_or_natted_address_is_rejected(self):
        self.addresses[0]["addr_info"][0]["local"] = "10.0.0.4"
        self.routes[0]["gateway"] = "10.0.0.1"
        with self.assertRaisesRegex(ValueError, "public"):
            select_network(self.addresses, self.routes)

    def test_explicit_cloud_nat_dhcp_uses_private_lease(self):
        self.addresses[0]["addr_info"][0].update(local="10.3.0.2", prefixlen=22)
        self.routes[0].update(gateway="10.3.0.1", protocol="dhcp")
        self.assertEqual(
            select_network(self.addresses, self.routes, allow_cloud_nat_dhcp=True),
            NetworkProfile("52:54:00:12:34:56", "dhcp", "10.3.0.2", 22, "10.3.0.1"),
        )

    def test_cloud_nat_rejects_static_private_network(self):
        self.addresses[0]["addr_info"][0].update(local="10.3.0.2", prefixlen=22)
        self.routes[0]["gateway"] = "10.3.0.1"
        with self.assertRaisesRegex(ValueError, "DHCP"):
            select_network(self.addresses, self.routes, allow_cloud_nat_dhcp=True)

    def test_cloud_nat_accepts_unlabelled_dhcp_route_with_verified_client(self):
        self.addresses[0]["addr_info"][0].update(local="10.3.0.2", prefixlen=22)
        self.routes[0].update(gateway="10.3.0.1", protocol=None)
        got = select_network(self.addresses, self.routes, allow_cloud_nat_dhcp=True,
                             dhcp_source_confirmed=True)
        self.assertEqual(got.mode, "dhcp")
        with self.assertRaisesRegex(ValueError, "DHCP"):
            select_network(self.addresses, self.routes, allow_cloud_nat_dhcp=True)

    def test_slash_32_is_rejected(self):
        self.addresses[0]["addr_info"][0]["prefixlen"] = 32
        with self.assertRaisesRegex(ValueError, "prefix"):
            select_network(self.addresses, self.routes, allow_test_ip=True)


class ConfigTests(unittest.TestCase):
    def test_only_expected_rows_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "config.db"
            source = Path("/tmp/ikuai-clean-config.db")
            if not source.exists():
                self.skipTest("clean ISO config fixture not extracted")
            db.write_bytes(source.read_bytes())
            profile = NetworkProfile("52:54:00:ab:cd:ef", "static", "203.0.113.10", 24, "203.0.113.1")
            provision_database(db, profile, "a" * 32, 443)
            con = sqlite3.connect(db)
            wan = con.execute("select bandif,internet,ip_mask,gateway,default_route from wan_config").fetchone()
            lan = con.execute("select bandif from lan_config").fetchone()[0]
            remote = con.execute("select open_wanweb,open_sshd,https_port,force_https from remote_control").fetchone()
            password = con.execute("select passwd from webuser where username='admin'").fetchone()[0]
            self.assertEqual(wan, (profile.mac, 0, "203.0.113.10/255.255.255.0", "203.0.113.1", 0))
            self.assertEqual(lan, LAN_UNBOUND)
            self.assertEqual(remote, (1, 0, 443, 1))
            self.assertEqual(password, "a" * 32)
            self.assertEqual(con.execute("pragma integrity_check").fetchone()[0], "ok")

    def test_dhcp_does_not_pin_ip_or_gateway(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "config.db"
            source = Path("/tmp/ikuai-clean-config.db")
            if not source.exists():
                self.skipTest("clean ISO config fixture not extracted")
            db.write_bytes(source.read_bytes())
            profile = NetworkProfile("52:54:00:ab:cd:ef", "dhcp", "203.0.113.10", 24, "203.0.113.1")
            provision_database(db, profile, "b" * 32, 443)
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("select internet,ip_mask,gateway from wan_config").fetchone(), (1, "", ""))


if __name__ == "__main__":
    unittest.main()
