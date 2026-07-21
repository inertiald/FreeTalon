"""Network topology reconnaissance via LLDP.

Wraps the ``lldpctl`` CLI to discover local network neighbours (DAC and
Ethernet links) and returns a structured, JSON-serialisable model.  Every
probe is fully guarded: if ``lldpctl`` is absent or errors, an empty
:class:`NetworkTopology` is returned rather than an exception being raised.

Link-type heuristic
-------------------
The ``link_type`` field of :class:`Neighbor` is classified as follows:

* ``"dac"``      – port description or chassis description contains one of the
  keywords ``dac``, ``sfp``, ``qsfp``, ``direct-attach``, or ``twinax``
  (case-insensitive).
* ``"ethernet"`` – port description or capability contains ``ethernet``,
  ``100base``, ``1000base``, ``10gbase``, or ``copper`` (case-insensitive).
* ``"unknown"``  – neither heuristic matched.

The heuristic is intentionally small; add new patterns to ``_DAC_KEYWORDS``
or ``_ETHERNET_KEYWORDS`` to extend it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Link-type classification keywords
# ---------------------------------------------------------------------------

_DAC_KEYWORDS: frozenset[str] = frozenset(
    {"dac", "sfp", "qsfp", "direct-attach", "twinax"}
)
_ETHERNET_KEYWORDS: frozenset[str] = frozenset(
    {"ethernet", "100base", "1000base", "10gbase", "copper"}
)


def _classify_link(description: str) -> str:
    """Return ``"dac"``, ``"ethernet"``, or ``"unknown"`` for *description*."""
    lowered = description.lower()
    for kw in _DAC_KEYWORDS:
        if kw in lowered:
            return "dac"
    for kw in _ETHERNET_KEYWORDS:
        if kw in lowered:
            return "ethernet"
    return "unknown"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Neighbor:
    """A single LLDP-discovered neighbour on a local interface."""

    local_interface: str
    remote_chassis_id: str
    remote_port_id: str
    remote_system_name: str
    link_type: str  # "dac" | "ethernet" | "unknown"


@dataclass(frozen=True, slots=True)
class NetworkTopology:
    """The full local network topology as discovered via LLDP."""

    neighbors: tuple[Neighbor, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a plain :class:`dict` representation suitable for JSON serialisation."""
        return {
            "neighbors": [
                {
                    "local_interface": n.local_interface,
                    "remote_chassis_id": n.remote_chassis_id,
                    "remote_port_id": n.remote_port_id,
                    "remote_system_name": n.remote_system_name,
                    "link_type": n.link_type,
                }
                for n in self.neighbors
            ]
        }

    def to_json(self, **kwargs: Any) -> str:
        """Return a JSON string representation of the topology."""
        return json.dumps(self.to_dict(), **kwargs)


# Convenience constant for an empty (no-neighbours) topology.
_EMPTY_TOPOLOGY = NetworkTopology(neighbors=())


# ---------------------------------------------------------------------------
# Parsers (public so tests can exercise them with fixture strings)
# ---------------------------------------------------------------------------


def parse_lldp_json(raw: str) -> NetworkTopology:
    """Parse the output of ``lldpctl -f json`` into a :class:`NetworkTopology`.

    Returns an empty topology on any parse error.

    The ``lldpctl -f json`` output has the shape::

        {
          "lldp": {
            "interface": {
              "eth0": {
                "chassis": {"<chassis-id>": {"name": [{"value": "..."}], ...}},
                "port": {"id": {"value": "..."}, "descr": "..."},
                ...
              },
              ...
            }
          }
        }

    Both the ``"interface"`` value being an object (keyed by interface name)
    and an array of such objects are handled.
    """
    try:
        data = json.loads(raw)
        lldp = data.get("lldp", {})
        iface_data = lldp.get("interface", {})

        # lldpctl may wrap the interface map in a list when there is only one
        # entry, or return it directly as a dict.
        if isinstance(iface_data, list):
            iface_items: list[dict[str, Any]] = iface_data
        else:
            iface_items = [iface_data]

        neighbors: list[Neighbor] = []
        for iface_block in iface_items:
            for local_iface, details in iface_block.items():
                neighbor = _parse_json_neighbor(local_iface, details)
                if neighbor is not None:
                    neighbors.append(neighbor)

        return NetworkTopology(neighbors=tuple(neighbors))
    except (json.JSONDecodeError, KeyError, AttributeError, TypeError):
        return _EMPTY_TOPOLOGY
    except Exception:  # noqa: BLE001
        return _EMPTY_TOPOLOGY


def _extract_lldp_value(val: Any) -> str:
    """Return the string value from a lldpctl JSON field.

    lldpctl may represent a field as a plain string, as a list of
    ``{"value": "…"}`` dicts, or as a list of plain strings.  This helper
    normalises all three representations to a single string.
    """
    if isinstance(val, str):
        return val
    if isinstance(val, list) and val:
        first = val[0]
        return first.get("value", "") if isinstance(first, dict) else str(first)
    return ""


def _parse_json_neighbor(local_iface: str, details: dict[str, Any]) -> Neighbor | None:
    """Extract a :class:`Neighbor` from a single interface block."""
    try:
        chassis_block = details.get("chassis", {})
        # chassis_block is typically {"<chassis-id>": {...}}
        chassis_id = ""
        system_name = ""
        chassis_desc = ""
        if isinstance(chassis_block, dict):
            for cid, cinfo in chassis_block.items():
                chassis_id = cid
                if isinstance(cinfo, dict):
                    system_name = _extract_lldp_value(cinfo.get("name", ""))
                    chassis_desc = _extract_lldp_value(cinfo.get("descr", ""))
                break

        port_block = details.get("port", {})
        port_id = ""
        port_desc = ""
        if isinstance(port_block, dict):
            id_val = port_block.get("id", {})
            if isinstance(id_val, dict):
                port_id = str(id_val.get("value", ""))
            elif isinstance(id_val, str):
                port_id = id_val
            port_desc = _extract_lldp_value(port_block.get("descr", ""))

        link_hint = " ".join([port_desc, chassis_desc])
        return Neighbor(
            local_interface=local_iface,
            remote_chassis_id=chassis_id,
            remote_port_id=port_id,
            remote_system_name=system_name,
            link_type=_classify_link(link_hint),
        )
    except (KeyError, AttributeError, TypeError):
        return None
    except Exception:  # noqa: BLE001
        return None


def parse_lldp_keyvalue(raw: str) -> NetworkTopology:
    """Parse the output of ``lldpctl -f keyvalue`` into a :class:`NetworkTopology`.

    The keyvalue format emitted by ``lldpctl`` looks like::

        lldp.eth0.chassis.mac=aa:bb:cc:dd:ee:ff
        lldp.eth0.chassis.name=switch-a
        lldp.eth0.port.ifname=GigabitEthernet0/1
        lldp.eth0.port.descr=SFP DAC cable
        lldp.eth1.chassis.mac=11:22:33:44:55:66
        ...

    Returns an empty topology on any parse error.
    """
    try:
        # Group lines by interface name (second dotted component).
        iface_data: dict[str, dict[str, str]] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            parts = key.split(".")
            # Expected prefix: lldp.<iface>.<rest…>
            if len(parts) < 3 or parts[0] != "lldp":
                continue
            iface = parts[1]
            sub_key = ".".join(parts[2:])
            iface_data.setdefault(iface, {})[sub_key] = value

        neighbors: list[Neighbor] = []
        for iface, kv in iface_data.items():
            chassis_id = kv.get("chassis.mac") or kv.get("chassis.id", "")
            system_name = kv.get("chassis.name", "")
            port_id = kv.get("port.ifname") or kv.get("port.id", "")
            port_desc = kv.get("port.descr", "")
            chassis_desc = kv.get("chassis.descr", "")
            link_hint = " ".join([port_desc, chassis_desc])
            neighbors.append(
                Neighbor(
                    local_interface=iface,
                    remote_chassis_id=chassis_id,
                    remote_port_id=port_id,
                    remote_system_name=system_name,
                    link_type=_classify_link(link_hint),
                )
            )

        return NetworkTopology(neighbors=tuple(neighbors))
    except (KeyError, AttributeError, ValueError, TypeError):
        return _EMPTY_TOPOLOGY
    except Exception:  # noqa: BLE001
        return _EMPTY_TOPOLOGY


# ---------------------------------------------------------------------------
# Top-level discovery function
# ---------------------------------------------------------------------------


def discover_topology() -> NetworkTopology:
    """Discover the local network topology via ``lldpctl``.

    Tries ``lldpctl -f json`` first; falls back to ``lldpctl -f keyvalue`` if
    the JSON output cannot be parsed.  Returns an empty :class:`NetworkTopology`
    if ``lldpctl`` is not installed, times out, or fails for any other reason.

    This function never raises.
    """
    lldpctl = shutil.which("lldpctl")
    if not lldpctl:
        return _EMPTY_TOPOLOGY

    # Attempt JSON output.
    try:
        result = subprocess.run(
            [lldpctl, "-f", "json"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            topology = parse_lldp_json(result.stdout)
            # Only trust the JSON parse if it differs from empty OR if JSON
            # output was explicitly non-empty (avoids silently returning empty
            # on a parse error when lldpctl is present but has no neighbours).
            if topology.neighbors or _looks_like_valid_json(result.stdout):
                return topology
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    except Exception:  # noqa: BLE001
        pass

    # Fallback: keyvalue format.
    try:
        result = subprocess.run(
            [lldpctl, "-f", "keyvalue"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            return parse_lldp_keyvalue(result.stdout)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    except Exception:  # noqa: BLE001
        pass

    return _EMPTY_TOPOLOGY


def _looks_like_valid_json(text: str) -> bool:
    """Return True if *text* can be decoded as JSON without error."""
    try:
        json.loads(text)
        return True
    except ValueError:
        return False
