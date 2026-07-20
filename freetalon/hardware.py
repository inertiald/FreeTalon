from __future__ import annotations

import ctypes.util
import importlib.util
import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class HostCapabilities:
    cpu_count: int
    memory_mib: int
    gpu_available: bool
    acceleration_libs: tuple[str, ...]
    rdma_available: bool = False
    nccl_available: bool = False
    gpu_count: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeTuning:
    worker_count: int
    max_queue_size: int


def _detect_memory_mib() -> int:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return max(int(line.split()[1]) // 1024, 256)
    except OSError:
        pass
    return 4096


@lru_cache(maxsize=16)
def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _detect_rdma() -> bool:
    for tool in ("ibstat", "ibv_devices", "rdma"):
        if shutil.which(tool):
            return True
    try:
        result = subprocess.run(
            ["lsmod"], capture_output=True, text=True, timeout=2
        )
        if "rdma_rxe" in result.stdout or "ib_core" in result.stdout:
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _detect_nccl() -> bool:
    if _has_module("nccl"):
        return True
    return bool(ctypes.util.find_library("nccl"))


def _detect_gpu_count() -> int:
    # Prefer an authoritative query via nvidia-smi when present; fall back to
    # counting /dev/nvidia* device nodes. Never raises — returns 0 on failure.
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            result = subprocess.run(
                [smi, "--query-gpu=index", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                count = sum(1 for line in result.stdout.splitlines() if line.strip())
                if count:
                    return count
        except Exception:  # noqa: BLE001
            pass
    try:
        return sum(
            1
            for name in os.listdir("/dev")
            if name.startswith("nvidia") and name[6:].isdigit()
        )
    except OSError:
        return 0


def detect_host_capabilities() -> HostCapabilities:
    cpu_count = max(os.cpu_count() or 1, 1)
    memory_mib = _detect_memory_mib()
    libs: list[str] = []
    for lib in ("cupy", "torch", "numba"):
        if _has_module(lib):
            libs.append(lib)
    gpu_count = _detect_gpu_count()
    gpu_available = _has_module("cupy") or gpu_count > 0
    return HostCapabilities(
        cpu_count=cpu_count,
        memory_mib=memory_mib,
        gpu_available=gpu_available,
        acceleration_libs=tuple(libs),
        rdma_available=_detect_rdma(),
        nccl_available=_detect_nccl(),
        gpu_count=gpu_count,
    )


def adaptive_tuning(
    capabilities: HostCapabilities,
    worker_cap: int,
    queue_multiplier: int,
) -> RuntimeTuning:
    memory_bound = max(capabilities.memory_mib // 768, 1)
    cpu_bound = max(capabilities.cpu_count - 1, 1)
    worker_count = max(1, min(worker_cap, cpu_bound, memory_bound))
    max_queue_size = max(worker_count * max(queue_multiplier, 1), 1)
    return RuntimeTuning(worker_count=worker_count, max_queue_size=max_queue_size)
