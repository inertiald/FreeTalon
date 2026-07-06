from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .hardware import HostCapabilities, RuntimeTuning, adaptive_tuning, detect_host_capabilities


class HiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8765, ge=1024, le=65535)
    workspace: Path = Field(default=Path.home() / "freetalon-workspace")
    state_path: Path = Field(default=Path.home() / "freetalon-workspace" / "hive-state.json")
    audit_log_path: Path = Field(default=Path.home() / "freetalon-workspace" / "audit.log")
    api_token_env: str = Field(default="FREETALON_API_TOKEN")
    api_token_file: Path | None = Field(default=None)
    worker_cap: int = Field(default=8, ge=1, le=64)
    queue_multiplier: int = Field(default=6, ge=1, le=50)
    heartbeat_timeout_seconds: float = Field(default=20.0, ge=1.0, le=300.0)
    poll_interval_seconds: float = Field(default=0.1, ge=0.01, le=5.0)

    # ADR 0002 — distributed topology and parallelism
    topology: Literal["star", "ring"] = Field(default="star")
    transport: Literal["tcp", "rdma"] = Field(default="tcp")
    tensor_parallel_size: int = Field(default=1, ge=1, le=512)
    pipeline_parallel_size: int = Field(default=1, ge=1, le=128)
    data_parallel_size: int = Field(default=1, ge=1, le=1024)
    nccl_socket_ifname: str = Field(default="lo", max_length=32)
    nccl_debug: bool = Field(default=False)

    @field_validator("workspace", "state_path", "audit_log_path", mode="before")
    @classmethod
    def _expand_path(cls, value: object) -> Path:
        return Path(value).expanduser().resolve()

    def runtime_tuning(self, host: HostCapabilities | None = None) -> RuntimeTuning:
        capabilities = host or detect_host_capabilities()
        return adaptive_tuning(capabilities, self.worker_cap, self.queue_multiplier)

    def ensure_directories(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
