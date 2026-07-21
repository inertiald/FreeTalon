"""FreeTalon mesh discovery package.

Provides network topology reconnaissance via LLDP, modelling discovered
DAC/Ethernet neighbours as structured, JSON-serialisable dataclasses.
"""

from .recon import Neighbor, NetworkTopology, discover_topology

__all__ = ["Neighbor", "NetworkTopology", "discover_topology"]
