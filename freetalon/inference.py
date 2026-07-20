from __future__ import annotations

from typing import Any

from .config import HiveConfig
from .security import sanitize_inference_payload


class VLLMInferenceEngine:
    """Wrapper around the vLLM ``LLM`` / ``AsyncLLMEngine`` interface.

    Per ADR 0002, this engine is adopted for high-throughput, local and
    air-gapped LLM inference. It follows the task-payload + security-boundary
    pattern established in ADR 0001: the incoming payload is validated and
    sanitized before any engine-specific parameters are derived, and the raw
    payload never reaches framework internals directly.

    The concrete implementation is intentionally deferred: vLLM must be
    vendored (a point-in-time snapshot, pinned with SHA256 hashes and recorded
    in ``docs/approved-dependency-baseline.md``) before it can be imported.
    Until that vendored snapshot lands, initialization raises
    ``NotImplementedError`` rather than fetching anything at runtime.
    """

    task_type = "inference"

    def __init__(self, config: HiveConfig, payload: dict[str, Any]) -> None:
        # Security boundary: validate/sanitize before touching the framework.
        self.config = config
        self.payload = sanitize_inference_payload(payload)
        # Engine construction parameters sourced from the validated payload and
        # the schema-validated config (ADR 0002 forwards these to the vLLM
        # engine constructor).
        self.tensor_parallel_size = self.payload.get(
            "tensor_parallel_size", config.tensor_parallel_size
        )
        self.pipeline_parallel_size = self.payload.get(
            "pipeline_parallel_size", config.pipeline_parallel_size
        )
        self.max_model_len = config.vllm_max_model_len
        self.dtype = config.vllm_dtype
        raise NotImplementedError("Pending vendored vLLM snapshot")

    def generate(self, prompt: str) -> Any:
        raise NotImplementedError("Pending vendored vLLM snapshot")
