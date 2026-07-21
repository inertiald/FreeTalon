"""Tests for freetalon.mesh.recon — network topology reconnaissance."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from freetalon.mesh.recon import (
    Neighbor,
    NetworkTopology,
    discover_topology,
    parse_lldp_json,
    parse_lldp_keyvalue,
)

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

# Representative lldpctl -f json output with two neighbours:
#   eth0 → DAC link (QSFP mention in port description)
#   eth1 → standard Ethernet link
_JSON_FIXTURE = json.dumps(
    {
        "lldp": {
            "interface": {
                "eth0": {
                    "chassis": {
                        "aa:bb:cc:dd:ee:ff": {
                            "name": [{"value": "spine-switch-1"}],
                            "descr": [{"value": "Arista Networks EOS"}],
                        }
                    },
                    "port": {
                        "id": {"value": "Ethernet1"},
                        "descr": "QSFP DAC 40GbE direct-attach",
                    },
                },
                "eth1": {
                    "chassis": {
                        "11:22:33:44:55:66": {
                            "name": [{"value": "leaf-switch-2"}],
                            "descr": [{"value": "Cisco IOS"}],
                        }
                    },
                    "port": {
                        "id": {"value": "GigabitEthernet0/1"},
                        "descr": "1000BASE-T copper uplink",
                    },
                },
            }
        }
    }
)

# Representative lldpctl -f keyvalue output with two neighbours:
_KEYVALUE_FIXTURE = """\
lldp.eth0.chassis.mac=aa:bb:cc:dd:ee:ff
lldp.eth0.chassis.name=spine-switch-1
lldp.eth0.chassis.descr=Arista Networks EOS
lldp.eth0.port.ifname=Ethernet1
lldp.eth0.port.descr=SFP DAC twinax
lldp.eth1.chassis.mac=11:22:33:44:55:66
lldp.eth1.chassis.name=leaf-switch-2
lldp.eth1.chassis.descr=Cisco IOS
lldp.eth1.port.ifname=GigabitEthernet0/1
lldp.eth1.port.descr=1000BASE-T copper uplink
"""


# ---------------------------------------------------------------------------
# parse_lldp_json tests
# ---------------------------------------------------------------------------


class ParseLldpJsonTests(unittest.TestCase):
    """Tests for parse_lldp_json with captured fixture strings."""

    def _topology(self) -> NetworkTopology:
        return parse_lldp_json(_JSON_FIXTURE)

    def test_returns_network_topology(self) -> None:
        result = self._topology()
        self.assertIsInstance(result, NetworkTopology)

    def test_parses_correct_neighbor_count(self) -> None:
        result = self._topology()
        self.assertEqual(len(result.neighbors), 2)

    def test_parses_eth0_local_interface(self) -> None:
        topo = self._topology()
        eth0 = next(n for n in topo.neighbors if n.local_interface == "eth0")
        self.assertEqual(eth0.local_interface, "eth0")

    def test_parses_eth0_remote_chassis_id(self) -> None:
        topo = self._topology()
        eth0 = next(n for n in topo.neighbors if n.local_interface == "eth0")
        self.assertEqual(eth0.remote_chassis_id, "aa:bb:cc:dd:ee:ff")

    def test_parses_eth0_remote_port_id(self) -> None:
        topo = self._topology()
        eth0 = next(n for n in topo.neighbors if n.local_interface == "eth0")
        self.assertEqual(eth0.remote_port_id, "Ethernet1")

    def test_parses_eth0_remote_system_name(self) -> None:
        topo = self._topology()
        eth0 = next(n for n in topo.neighbors if n.local_interface == "eth0")
        self.assertEqual(eth0.remote_system_name, "spine-switch-1")

    def test_parses_eth1_fields(self) -> None:
        topo = self._topology()
        eth1 = next(n for n in topo.neighbors if n.local_interface == "eth1")
        self.assertEqual(eth1.remote_chassis_id, "11:22:33:44:55:66")
        self.assertEqual(eth1.remote_system_name, "leaf-switch-2")
        self.assertEqual(eth1.remote_port_id, "GigabitEthernet0/1")

    def test_returns_empty_topology_on_invalid_json(self) -> None:
        result = parse_lldp_json("not valid json }{")
        self.assertIsInstance(result, NetworkTopology)
        self.assertEqual(len(result.neighbors), 0)

    def test_returns_empty_topology_on_empty_string(self) -> None:
        result = parse_lldp_json("")
        self.assertEqual(len(result.neighbors), 0)


# ---------------------------------------------------------------------------
# Link-type classification tests
# ---------------------------------------------------------------------------


class LinkTypeClassificationTests(unittest.TestCase):
    """Tests for DAC vs Ethernet link classification."""

    def test_dac_classified_for_qsfp_in_json(self) -> None:
        topo = parse_lldp_json(_JSON_FIXTURE)
        eth0 = next(n for n in topo.neighbors if n.local_interface == "eth0")
        self.assertEqual(eth0.link_type, "dac")

    def test_ethernet_classified_for_copper_in_json(self) -> None:
        topo = parse_lldp_json(_JSON_FIXTURE)
        eth1 = next(n for n in topo.neighbors if n.local_interface == "eth1")
        self.assertEqual(eth1.link_type, "ethernet")

    def test_dac_classified_from_keyvalue_sfp(self) -> None:
        topo = parse_lldp_keyvalue(_KEYVALUE_FIXTURE)
        eth0 = next(n for n in topo.neighbors if n.local_interface == "eth0")
        self.assertEqual(eth0.link_type, "dac")

    def test_ethernet_classified_from_keyvalue_copper(self) -> None:
        topo = parse_lldp_keyvalue(_KEYVALUE_FIXTURE)
        eth1 = next(n for n in topo.neighbors if n.local_interface == "eth1")
        self.assertEqual(eth1.link_type, "ethernet")

    def test_unknown_link_type_when_no_hint(self) -> None:
        minimal_json = json.dumps(
            {
                "lldp": {
                    "interface": {
                        "eth2": {
                            "chassis": {"cc:dd:ee:ff:00:11": {"name": [{"value": "peer"}]}},
                            "port": {"id": {"value": "p1"}, "descr": ""},
                        }
                    }
                }
            }
        )
        topo = parse_lldp_json(minimal_json)
        self.assertEqual(len(topo.neighbors), 1)
        self.assertEqual(topo.neighbors[0].link_type, "unknown")


# ---------------------------------------------------------------------------
# parse_lldp_keyvalue tests
# ---------------------------------------------------------------------------


class ParseLldpKeyvalueTests(unittest.TestCase):
    """Tests for parse_lldp_keyvalue with captured fixture strings."""

    def _topology(self) -> NetworkTopology:
        return parse_lldp_keyvalue(_KEYVALUE_FIXTURE)

    def test_returns_network_topology(self) -> None:
        self.assertIsInstance(self._topology(), NetworkTopology)

    def test_parses_correct_neighbor_count(self) -> None:
        self.assertEqual(len(self._topology().neighbors), 2)

    def test_parses_chassis_id(self) -> None:
        topo = self._topology()
        eth0 = next(n for n in topo.neighbors if n.local_interface == "eth0")
        self.assertEqual(eth0.remote_chassis_id, "aa:bb:cc:dd:ee:ff")

    def test_parses_system_name(self) -> None:
        topo = self._topology()
        eth0 = next(n for n in topo.neighbors if n.local_interface == "eth0")
        self.assertEqual(eth0.remote_system_name, "spine-switch-1")

    def test_parses_port_id(self) -> None:
        topo = self._topology()
        eth0 = next(n for n in topo.neighbors if n.local_interface == "eth0")
        self.assertEqual(eth0.remote_port_id, "Ethernet1")

    def test_returns_empty_topology_on_garbage_input(self) -> None:
        result = parse_lldp_keyvalue("completely garbage\n\n\n??")
        self.assertIsInstance(result, NetworkTopology)
        # No valid lldp. lines → empty neighbors
        self.assertEqual(len(result.neighbors), 0)


# ---------------------------------------------------------------------------
# discover_topology graceful-failure tests
# ---------------------------------------------------------------------------


class DiscoverTopologyTests(unittest.TestCase):
    """Tests for discover_topology() safety and subprocess integration."""

    def test_returns_empty_when_lldpctl_missing(self) -> None:
        with patch("freetalon.mesh.recon.shutil.which", return_value=None):
            result = discover_topology()
        self.assertIsInstance(result, NetworkTopology)
        self.assertEqual(len(result.neighbors), 0)

    def test_returns_empty_when_subprocess_times_out(self) -> None:
        with patch("freetalon.mesh.recon.shutil.which", return_value="/usr/sbin/lldpctl"), \
             patch(
                 "freetalon.mesh.recon.subprocess.run",
                 side_effect=subprocess.TimeoutExpired(cmd="lldpctl", timeout=3),
             ):
            result = discover_topology()
        self.assertIsInstance(result, NetworkTopology)
        self.assertEqual(len(result.neighbors), 0)

    def test_returns_empty_when_subprocess_raises_oserror(self) -> None:
        with patch("freetalon.mesh.recon.shutil.which", return_value="/usr/sbin/lldpctl"), \
             patch("freetalon.mesh.recon.subprocess.run", side_effect=OSError("no such file")):
            result = discover_topology()
        self.assertIsInstance(result, NetworkTopology)
        self.assertEqual(len(result.neighbors), 0)

    def test_uses_json_output_when_available(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _JSON_FIXTURE

        with patch("freetalon.mesh.recon.shutil.which", return_value="/usr/sbin/lldpctl"), \
             patch("freetalon.mesh.recon.subprocess.run", return_value=mock_result):
            result = discover_topology()

        self.assertEqual(len(result.neighbors), 2)

    def test_falls_back_to_keyvalue_when_json_fails(self) -> None:
        json_result = MagicMock()
        json_result.returncode = 0
        json_result.stdout = "not json output at all"

        kv_result = MagicMock()
        kv_result.returncode = 0
        kv_result.stdout = _KEYVALUE_FIXTURE

        call_count = [0]

        def _side_effect(cmd, **kwargs):  # noqa: ANN001
            call_count[0] += 1
            if call_count[0] == 1:
                return json_result
            return kv_result

        with patch("freetalon.mesh.recon.shutil.which", return_value="/usr/sbin/lldpctl"), \
             patch("freetalon.mesh.recon.subprocess.run", side_effect=_side_effect):
            result = discover_topology()

        self.assertEqual(len(result.neighbors), 2)

    def test_returns_empty_when_both_formats_fail(self) -> None:
        bad_result = MagicMock()
        bad_result.returncode = 1
        bad_result.stdout = ""

        with patch("freetalon.mesh.recon.shutil.which", return_value="/usr/sbin/lldpctl"), \
             patch("freetalon.mesh.recon.subprocess.run", return_value=bad_result):
            result = discover_topology()

        self.assertIsInstance(result, NetworkTopology)
        self.assertEqual(len(result.neighbors), 0)


# ---------------------------------------------------------------------------
# NetworkTopology serialisation tests
# ---------------------------------------------------------------------------


class NetworkTopologySerializationTests(unittest.TestCase):
    """Tests for NetworkTopology.to_dict() and to_json()."""

    def _sample_topology(self) -> NetworkTopology:
        return NetworkTopology(
            neighbors=(
                Neighbor(
                    local_interface="eth0",
                    remote_chassis_id="aa:bb:cc:dd:ee:ff",
                    remote_port_id="Eth1",
                    remote_system_name="sw1",
                    link_type="dac",
                ),
            )
        )

    def test_to_dict_returns_dict(self) -> None:
        self.assertIsInstance(self._sample_topology().to_dict(), dict)

    def test_to_dict_contains_neighbors_key(self) -> None:
        d = self._sample_topology().to_dict()
        self.assertIn("neighbors", d)

    def test_to_dict_neighbor_fields(self) -> None:
        neighbor = self._sample_topology().to_dict()["neighbors"][0]
        self.assertEqual(neighbor["local_interface"], "eth0")
        self.assertEqual(neighbor["remote_chassis_id"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(neighbor["link_type"], "dac")

    def test_to_json_is_valid_json(self) -> None:
        js = self._sample_topology().to_json()
        parsed = json.loads(js)
        self.assertEqual(len(parsed["neighbors"]), 1)

    def test_empty_topology_to_json(self) -> None:
        topo = NetworkTopology(neighbors=())
        parsed = json.loads(topo.to_json())
        self.assertEqual(parsed["neighbors"], [])


if __name__ == "__main__":
    unittest.main()
