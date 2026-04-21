#!/usr/bin/env python3
"""FreeTalon Resource Manager — host-aware dynamic resource allocation.

Probes the host machine for total CPU cores and RAM, then computes
per-container resource caps for each task profile.  Every allocation
is clamped so that a single claw can never exhaust the host.

Task profiles
-------------
default
    Conservative limits suitable for lightweight Python scripts.
video
    Up to 8 GiB RAM and 400 % CPU (4 cores) for media-processing claws
    that run FFmpeg or similar workloads.
youtube_upload
    Same compute budget as *video* but with **internet access** via a
    non-internal bridge network so the container can reach YouTube's API.

Usage::

    rm = ResourceManager()
    limits = rm.limits_for("video")
    # limits.mem_limit   -> "8g"   (or less if the host is constrained)
    # limits.cpu_period  -> 100000
    # limits.cpu_quota   -> 400000 (4 cores)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Host-probing helpers
# ---------------------------------------------------------------------------

_GiB = 1024 ** 3
_MiB = 1024 ** 2


def _host_cpu_count() -> int:
    """Return the number of logical CPUs on the host (minimum 1)."""
    return max(os.cpu_count() or 1, 1)


def _host_total_ram_bytes() -> int:
    """Return total physical RAM in bytes.

    Reads ``/proc/meminfo`` on Linux.  Falls back to ``os.sysconf`` if
    available, and ultimately defaults to 4 GiB when detection fails.
    """
    # Try /proc/meminfo first (most reliable on Linux).
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        try:
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    # Format: "MemTotal:       16384000 kB"
                    parts = line.split()
                    return int(parts[1]) * 1024  # kB → bytes
        except (OSError, ValueError, IndexError):
            pass

    # Fallback: os.sysconf (POSIX).
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return pages * page_size
    except (AttributeError, ValueError, OSError):
        pass

    # Last resort — assume 4 GiB so we can still compute reasonable caps.
    logger.warning(
        "Could not detect host RAM; defaulting to 4 GiB for resource caps."
    )
    return 4 * _GiB


# ---------------------------------------------------------------------------
# Resource-limit data class
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Immutable set of Docker resource-cap parameters for a single claw."""

    mem_limit: str
    """Docker memory limit string, e.g. ``"512m"`` or ``"8g"``."""

    cpu_period: int
    """CFS scheduler period in microseconds (always 100 000)."""

    cpu_quota: int
    """CFS scheduler quota in microseconds.  ``cpu_quota / cpu_period``
    equals the number of CPU cores the container may saturate."""

    read_only: bool
    """Whether to mount the root filesystem read-only."""

    network_internal: bool
    """When *True* the container is placed on an internal-only bridge with
    no route to the public internet.  When *False* the container gets a
    normal (non-internal) bridge, allowing outbound traffic."""

    output_volume: bool
    """When *True* the orchestrator should bind-mount ``/workspace/output``
    in read-write mode so the claw can persist artefacts."""


# ---------------------------------------------------------------------------
# Profile definitions (desired maximums)
# ---------------------------------------------------------------------------

# (desired_ram_bytes, desired_cpu_percent, read_only, network_internal, output_volume)
_PROFILES: dict[str, tuple[int, int, bool, bool, bool]] = {
    "default": (512 * _MiB, 50, True, True, False),
    "video": (8 * _GiB, 400, False, True, True),
    "youtube_upload": (8 * _GiB, 400, False, False, True),
}

_CPU_PERIOD = 100_000  # standard CFS period (µs)

# Safety margin — never assign more than this fraction of host resources to
# a single claw, regardless of what the profile requests.
_MAX_RAM_FRACTION = 0.80
_MAX_CPU_FRACTION = 0.80


# ---------------------------------------------------------------------------
# ResourceManager
# ---------------------------------------------------------------------------


class ResourceManager:
    """Host-aware resource allocator for FreeTalon claws.

    Instantiation triggers a one-shot probe of the host's CPU count and
    total RAM.  Subsequent calls to :meth:`limits_for` are pure look-ups.
    """

    def __init__(self) -> None:
        self.host_cpus: int = _host_cpu_count()
        self.host_ram_bytes: int = _host_total_ram_bytes()
        logger.info(
            "ResourceManager initialised — %d CPU(s), %.1f GiB RAM",
            self.host_cpus,
            self.host_ram_bytes / _GiB,
        )

    # ------------------------------------------------------------------

    def limits_for(self, profile: str = "default") -> ResourceLimits:
        """Return clamped :class:`ResourceLimits` for *profile*.

        Parameters
        ----------
        profile:
            One of ``"default"``, ``"video"``, or ``"youtube_upload"``.

        Raises
        ------
        ValueError
            If *profile* is not recognised.
        """
        if profile not in _PROFILES:
            raise ValueError(
                f"Unknown resource profile {profile!r}.  "
                f"Choose from {sorted(_PROFILES)}."
            )

        desired_ram, desired_cpu_pct, ro, net_internal, out_vol = _PROFILES[profile]

        # Clamp RAM so we never exceed _MAX_RAM_FRACTION of host memory.
        max_ram = int(self.host_ram_bytes * _MAX_RAM_FRACTION)
        effective_ram = min(desired_ram, max_ram)
        mem_str = self._bytes_to_docker_mem(effective_ram)

        # Clamp CPU so we never exceed _MAX_CPU_FRACTION of host cores.
        max_cpu_pct = int(self.host_cpus * 100 * _MAX_CPU_FRACTION)
        effective_cpu_pct = min(desired_cpu_pct, max_cpu_pct)
        cpu_quota = int(_CPU_PERIOD * effective_cpu_pct / 100)
        # Ensure at least 10 % of one core.
        cpu_quota = max(cpu_quota, _CPU_PERIOD // 10)

        limits = ResourceLimits(
            mem_limit=mem_str,
            cpu_period=_CPU_PERIOD,
            cpu_quota=cpu_quota,
            read_only=ro,
            network_internal=net_internal,
            output_volume=out_vol,
        )
        logger.debug(
            "Profile %r → mem=%s cpu_quota=%d (%.0f%% of host) ro=%s internal=%s output=%s",
            profile,
            limits.mem_limit,
            limits.cpu_quota,
            effective_cpu_pct,
            ro,
            net_internal,
            out_vol,
        )
        return limits

    # ------------------------------------------------------------------

    @staticmethod
    def _bytes_to_docker_mem(nbytes: int) -> str:
        """Convert a byte count to a Docker ``mem_limit`` string.

        Uses ``g`` (GiB) for values that divide evenly into whole GiB,
        ``m`` (MiB) otherwise for precision.
        """
        if nbytes >= _GiB and nbytes % _GiB == 0:
            gib = nbytes // _GiB
            return f"{gib}g"
        mib = max(nbytes // _MiB, 64)  # floor at 64 MiB
        return f"{mib}m"

    # ------------------------------------------------------------------

    def summary(self) -> dict[str, object]:
        """Return a JSON-friendly snapshot of host resources and profiles."""
        profiles: dict[str, dict[str, object]] = {}
        for name in _PROFILES:
            lim = self.limits_for(name)
            profiles[name] = {
                "mem_limit": lim.mem_limit,
                "cpu_quota": lim.cpu_quota,
                "cpu_period": lim.cpu_period,
                "read_only": lim.read_only,
                "network_internal": lim.network_internal,
                "output_volume": lim.output_volume,
            }
        return {
            "host_cpus": self.host_cpus,
            "host_ram_gib": round(self.host_ram_bytes / _GiB, 2),
            "profiles": profiles,
        }
