from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .audit import AuditLogger
from .config import HiveConfig
from .docker_manager import DockerManager
from .hardware import HostCapabilities, detect_host_capabilities
from .security import sanitize_payload


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    payload: dict[str, Any]
    status: str = "queued"
    attempt: int = 0
    max_retries: int = 0
    backoff_seconds: float = 0.2
    next_run_at: float = field(default_factory=time.time)
    last_error: str | None = None
    result: Any = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_requested: bool = False
    worker_id: str | None = None


class HiveController:
    def __init__(
        self, config: HiveConfig, host: HostCapabilities | None = None
    ) -> None:
        self.config = config
        self.config.ensure_directories()
        self.host: HostCapabilities = host or detect_host_capabilities()
        # Per ADR 0002: invalid distributed combinations (e.g. requested GPU
        # world size exceeding available GPUs, rdma transport without RDMA
        # hardware, or NCCL-requiring parallelism without NCCL) are caught and
        # reported here, before any worker is spawned.
        self.config.validate_against_host(self.host)
        self.tuning = self.config.runtime_tuning(self.host)
        self.audit = AuditLogger(self.config.audit_log_path)

        self._tasks: dict[str, TaskRecord] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._worker_heartbeats: dict[str, float] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=self.tuning.worker_count)
        self._shutdown = threading.Event()
        self._scheduler_thread: threading.Thread | None = None

        self._load_state()

    def start(self) -> None:
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return
        self._shutdown.clear()
        self._scheduler_thread = threading.Thread(target=self._run_scheduler, daemon=True)
        self._scheduler_thread.start()
        self.audit.log(
            "hive.started",
            workers=self.tuning.worker_count,
            max_queue_size=self.tuning.max_queue_size,
            cpu_count=self.host.cpu_count,
            memory_mib=self.host.memory_mib,
            gpu=self.host.gpu_available,
        )

    def stop(self) -> None:
        self._shutdown.set()
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=3)
        self._executor.shutdown(wait=True, cancel_futures=False)
        self._persist_state()
        self.audit.log("hive.stopped", task_count=len(self._tasks))

    def submit_task(self, payload: dict[str, Any]) -> TaskRecord:
        clean = sanitize_payload(payload)
        with self._lock:
            active = [t for t in self._tasks.values() if t.status in {"queued", "retrying", "running"}]
            if len(active) >= self.tuning.max_queue_size:
                raise ValueError("Queue is full")
            task_id = uuid.uuid4().hex[:12]
            task = TaskRecord(
                task_id=task_id,
                payload=clean,
                max_retries=int(clean.get("retries", 0)),
                backoff_seconds=float(clean.get("backoff_seconds", 0.2)),
            )
            self._tasks[task_id] = task
            self._persist_state()
        self.audit.log("task.submitted", task_id=task.task_id, action=clean["action"])
        return task

    def cancel_task(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if task.status in {"succeeded", "failed", "cancelled"}:
                return False
            if task.status in {"queued", "retrying"}:
                task.status = "cancelled"
                task.updated_at = time.time()
                self._persist_state()
                self.audit.log("task.cancelled", task_id=task_id, queued=True)
                return True
            task.cancel_requested = True
            task.updated_at = time.time()
            self._persist_state()
        self.audit.log("task.cancel_requested", task_id=task_id, queued=False)
        return True

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return asdict(task) if task else None

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            tasks = [asdict(t) for t in self._tasks.values()]
        if status:
            tasks = [t for t in tasks if t["status"] == status]
        return tasks

    def status(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            tasks = {k: asdict(v) for k, v in self._tasks.items()}
            healthy_workers = sum(
                1
                for ts in self._worker_heartbeats.values()
                if now - ts < self.config.heartbeat_timeout_seconds
            )
            queued = sum(
                1
                for task in self._tasks.values()
                if task.status in {"queued", "retrying"}
            )
            running = sum(1 for task in self._tasks.values() if task.status == "running")
        return {
            "workers": {
                "configured": self.tuning.worker_count,
                "healthy": healthy_workers,
                "known": len(self._worker_heartbeats),
            },
            "queue": {
                "active": queued + running,
                "max": self.tuning.max_queue_size,
                "queued": queued,
                "running": running,
            },
            "host": {
                "cpu_count": self.host.cpu_count,
                "memory_mib": self.host.memory_mib,
                "gpu_available": self.host.gpu_available,
                "acceleration_libs": list(self.host.acceleration_libs),
            },
            "tasks": tasks,
        }

    def _run_scheduler(self) -> None:
        while not self._shutdown.is_set():
            now = time.time()
            with self._lock:
                completed = [tid for tid, fut in self._futures.items() if fut.done()]
                for task_id in completed:
                    self._futures.pop(task_id, None)

                runnable = [
                    task
                    for task in self._tasks.values()
                    if task.status in {"queued", "retrying"}
                    and not task.cancel_requested
                    and task.next_run_at <= now
                ]

                available_slots = max(self.tuning.worker_count - len(self._futures), 0)
                for task in runnable[:available_slots]:
                    task.status = "running"
                    task.attempt += 1
                    task.worker_id = f"worker-{hash(task.task_id) % self.tuning.worker_count}"
                    task.updated_at = now
                    future = self._executor.submit(self._execute_task, task.task_id)
                    self._futures[task.task_id] = future

                self._persist_state()
            time.sleep(self.config.poll_interval_seconds)

    def _execute_task(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            worker_id = task.worker_id or "worker-unknown"
            payload = dict(task.payload)
        self._worker_heartbeats[worker_id] = time.time()

        try:
            result = self._run_action(task_id, payload, worker_id)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                task = self._tasks[task_id]
                task.last_error = str(exc)
                if task.cancel_requested:
                    task.status = "cancelled"
                elif task.attempt <= task.max_retries:
                    task.status = "retrying"
                    task.next_run_at = time.time() + (task.backoff_seconds * task.attempt)
                else:
                    task.status = "failed"
                task.updated_at = time.time()
                self._persist_state()
            self.audit.log("task.failed", task_id=task_id, error=str(exc), attempt=task.attempt)
            return

        with self._lock:
            task = self._tasks[task_id]
            if task.cancel_requested:
                task.status = "cancelled"
                task.result = None
            else:
                task.status = "succeeded"
                task.result = result
            task.updated_at = time.time()
            self._persist_state()

        self.audit.log("task.completed", task_id=task_id, status=task.status)

    def _run_action(self, task_id: str, payload: dict[str, Any], worker_id: str) -> Any:
        action = payload["action"]

        if payload.get("requires_gpu") and not self.host.gpu_available:
            raise RuntimeError("Task requires GPU, but no GPU acceleration is available")

        if action == "echo":
            return {"echo": payload["text"]}

        if action == "sum":
            values = payload["values"]
            path = "python"
            if self.host.acceleration_libs:
                path = self.host.acceleration_libs[0]
            return {"sum": sum(values), "path": path}

        if action == "sleep":
            duration = float(payload["duration"])
            deadline = time.time() + duration
            while time.time() < deadline:
                with self._lock:
                    if self._tasks[task_id].cancel_requested:
                        raise RuntimeError("Task cancelled")
                self._worker_heartbeats[worker_id] = time.time()
                remaining = max(deadline - time.time(), 0.0)
                time.sleep(min(0.05, remaining))
            return {"slept_seconds": duration}

        if action == "docker_claw":
            payload["_worker_id"] = worker_id
            return self._run_docker_claw(task_id, payload)

        raise RuntimeError(f"Unsupported action: {action}")

    def _run_docker_claw(self, task_id: str, payload: dict[str, Any]) -> Any:
        dm = DockerManager()
        code = payload["code"]
        profile = payload.get("profile", "default")
        timeout = float(payload.get("timeout", 30.0))
        dm.spawn_claw(task_id, code, profile)
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                with self._lock:
                    if self._tasks[task_id].cancel_requested:
                        dm.kill_claw(task_id)
                        raise RuntimeError("Task cancelled")
                self._worker_heartbeats[payload.get("_worker_id", task_id)] = time.time()
                status = dm.claw_status(task_id)
                if status in {"exited", "dead"}:
                    break
                time.sleep(0.5)
            else:
                dm.kill_claw(task_id)
                raise RuntimeError("docker_claw timed out")
            return dm.collect_result(task_id)
        finally:
            dm.remove_claw(task_id)

    def _persist_state(self) -> None:
        state = {
            "saved_at": time.time(),
            "tasks": [asdict(t) for t in self._tasks.values()],
        }
        with self.config.state_path.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, separators=(",", ":"), sort_keys=True)

    def _load_state(self) -> None:
        path: Path = self.config.state_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            tasks = data.get("tasks", [])
            with self._lock:
                for item in tasks:
                    task = TaskRecord(**item)
                    if task.status == "running":
                        task.status = "failed"
                        task.last_error = "Recovered after restart while task was running"
                    self._tasks[task.task_id] = task
        except Exception:  # noqa: BLE001
            self.audit.log("state.load_failed", path=str(path))
