from __future__ import annotations

from typing import Any

from .config import HiveConfig
from .security import sanitize_training_payload


class DeepSpeedTrainingEngine:
    """Wrapper around the DeepSpeed ``initialize()`` interface.

    Per ADR 0002, DeepSpeed is adopted for distributed training workloads
    (ZeRO stages 1/2/3, mixed precision, gradient checkpointing). It follows
    the task-payload + security-boundary pattern established in ADR 0001: the
    incoming payload is validated and sanitized before any engine-specific
    parameters are derived, and the raw payload never reaches framework
    internals directly.

    The concrete implementation is intentionally deferred: DeepSpeed (and its
    dependency tree — ``triton``, ``hjson``, ``py-cpuinfo``, etc.) must be
    vendored (a point-in-time snapshot, pinned with SHA256 hashes and recorded
    in ``docs/approved-dependency-baseline.md``) before it can be imported.
    Until that vendored snapshot lands, initialization raises
    ``NotImplementedError`` rather than fetching anything at runtime.
    """

    task_type = "training"

    def __init__(self, config: HiveConfig, payload: dict[str, Any]) -> None:
        # Security boundary: validate/sanitize before touching the framework.
        self.config = config
        self.payload = sanitize_training_payload(payload)
        # Engine construction parameters sourced from the validated payload and
        # the schema-validated config. ZeRO stage and parallelism knobs are
        # exposed as task parameters per ADR 0002.
        self.model = self.payload["model"]
        self.deepspeed_zero_stage = self.payload.get(
            "deepspeed_zero_stage", config.deepspeed_zero_stage
        )
        self.tensor_parallel_size = self.payload.get(
            "tensor_parallel_size", config.tensor_parallel_size
        )
        self.pipeline_parallel_size = self.payload.get(
            "pipeline_parallel_size", config.pipeline_parallel_size
        )
        self.data_parallel_size = self.payload.get(
            "data_parallel_size", config.data_parallel_size
        )
        raise NotImplementedError("Pending vendored DeepSpeed snapshot")

    def initialize(self) -> Any:
        raise NotImplementedError("Pending vendored DeepSpeed snapshot")
