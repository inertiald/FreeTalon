"""Deterministic measurement of executed plan nodes (loop Stage 4).

Measurement is a separate component from execution: it runs after a node
finishes and evaluates the criteria written in the plan against the result
envelope and the artifacts on disk. The language model plans and interprets,
but verdicts come from deterministic checks on real artifacts — exit codes,
files, numbers, test results.

Verdicts (design doc, measurement contract):
- ``PASS`` — every criterion passed.
- ``FAIL`` — any criterion failed (including a non-zero exit code).
- ``UNMEASURABLE`` — an artifact or metric a criterion needs is missing.
  Treated as failure for progress, but recorded distinctly: it usually
  means the plan was underspecified, which is itself a finding.

A node is COMPLETED only when its handler finished AND its measurement is
PASS. A script that exits non-zero can therefore never surface as success:
the executor demotes its own COMPLETED to an input of this module.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

# Verdict constants.
VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_UNMEASURABLE = "UNMEASURABLE"

# Criterion types.
CRITERION_EXIT_CODE = "exit_code"
CRITERION_FILE_EXISTS = "file_exists"
CRITERION_FILE_MATCHES = "file_matches"
CRITERION_METRIC = "metric"
CRITERION_TESTS_PASS = "tests_pass"
CRITERION_CUSTOM_CHECK = "custom_check"

# A custom checker receives (envelope, workdir) and returns
# (passed: bool, detail: str). Checkers are pre-registered by name — never
# code generated during the same run, which would let the system grade
# itself with a ruler it just drew.
CustomChecker = Callable[[dict[str, Any], Path], tuple[bool, str]]

_CUSTOM_CHECKERS: dict[str, CustomChecker] = {}


def register_checker(name: str, checker: CustomChecker) -> None:
    """Register a named custom checker available to ``custom_check`` criteria."""
    _CUSTOM_CHECKERS[name] = checker


def custom_checkers() -> dict[str, CustomChecker]:
    """Return a copy of the registered custom checkers."""
    return dict(_CUSTOM_CHECKERS)


def _resolve(path_value: str, workdir: Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else workdir / path


def _compare(op: str, measured: float, expected: float) -> bool:
    if op == "<":
        return measured < expected
    if op == "<=":
        return measured <= expected
    if op == "==":
        return measured == expected
    if op == ">=":
        return measured >= expected
    if op == ">":
        return measured > expected
    raise ValueError(f"Unknown comparison operator: {op!r}")


def _result(
    criterion: dict[str, Any], *, passed: bool, measured: bool, detail: str
) -> dict[str, Any]:
    return {
        "criterion": criterion,
        "passed": passed,
        "measured": measured,
        "detail": detail,
    }


def _eval_exit_code(
    criterion: dict[str, Any], envelope: dict[str, Any], workdir: Path | None = None
) -> dict[str, Any]:
    """Evaluate an exit_code criterion.

    Retry #23: a criterion with a `command` measures THAT command —
    the command runs here (shell, in the node workdir) and its return
    code is compared to `equals`. The old code ignored the command
    field entirely and compared the NODE's exit code, so v1 node-1
    "passed" a `curl -sf` criterion because the scaffold write exited
    0 — no live curl ever executed. A bare criterion (no command)
    measures the node command's exit code — that is the explicit
    default. A criterion command that times out or fails to start is
    a measured failure, not unmeasurable: the measurement ran.
    """
    expected = criterion.get("equals", 0)
    command = criterion.get("command")
    if isinstance(command, str) and command.strip():
        timeout = _criterion_timeout_s(criterion, default=60.0)
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return _result(
                criterion,
                passed=False,
                measured=True,
                detail=f"criterion command timed out after {_fmt_timeout(timeout)}",
            )
        except OSError as exc:
            return _result(
                criterion,
                passed=False,
                measured=True,
                detail=f"criterion command failed to start: {exc}",
            )
        if proc.returncode == expected:
            return _result(
                criterion,
                passed=True,
                measured=True,
                detail=f"criterion command exit code {proc.returncode} == {expected}",
            )
        return _result(
            criterion,
            passed=False,
            measured=True,
            detail=f"criterion command exit code {proc.returncode} != {expected}",
        )
    exit_code = envelope.get("exit_code")
    if exit_code is None:
        return _result(
            criterion, passed=False, measured=False, detail="no exit code reported"
        )
    if exit_code == expected:
        return _result(
            criterion, passed=True, measured=True, detail=f"exit_code == {expected}"
        )
    return _result(
        criterion,
        passed=False,
        measured=True,
        detail=f"exit_code {exit_code} != {expected}",
    )


def _criterion_timeout_s(criterion: dict[str, Any], default: float) -> float:
    """Timeout for a criterion command, coerced from timeout_s (default 60s)."""
    timeout = criterion.get("timeout_s", default)
    try:
        timeout_f = float(timeout)
    except (TypeError, ValueError):
        return default
    return timeout_f if timeout_f > 0 else default


def _fmt_timeout(timeout: float) -> str:
    """Render a timeout as seconds, e.g. 60s or 2.5s."""
    if float(timeout).is_integer():
        return f"{int(timeout)}s"
    return f"{timeout}s"


def _eval_file_exists(
    criterion: dict[str, Any], workdir: Path
) -> dict[str, Any]:
    raw_path = criterion.get("path", "")
    path = _resolve(str(raw_path), workdir)
    if path.exists():
        return _result(
            criterion, passed=True, measured=True, detail=f"{path} exists"
        )
    # A missing artifact is UNMEASURABLE, not FAIL: the plan asked for
    # something the run never produced, which is a finding for the re-planner.
    return _result(
        criterion, passed=False, measured=False, detail=f"{path} does not exist"
    )


def _eval_file_matches(
    criterion: dict[str, Any], workdir: Path
) -> dict[str, Any]:
    raw_path = criterion.get("path", "")
    path = _resolve(str(raw_path), workdir)
    if not path.exists():
        return _result(
            criterion, passed=False, measured=False, detail=f"{path} does not exist"
        )
    if "sha256" in criterion:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        expected = str(criterion["sha256"]).lower()
        if digest == expected:
            return _result(
                criterion, passed=True, measured=True, detail=f"sha256 {digest[:12]}… matches"
            )
        return _result(
            criterion,
            passed=False,
            measured=True,
            detail=f"sha256 {digest[:12]}… != expected {expected[:12]}…",
        )
    if "json_keys" in criterion:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return _result(
                criterion, passed=False, measured=True, detail=f"not valid JSON: {exc}"
            )
        if not isinstance(data, dict):
            return _result(
                criterion, passed=False, measured=True, detail="JSON is not an object"
            )
        missing = [k for k in criterion["json_keys"] if k not in data]
        if not missing:
            return _result(
                criterion,
                passed=True,
                measured=True,
                detail=f"all required keys present: {criterion['json_keys']}",
            )
        return _result(
            criterion,
            passed=False,
            measured=True,
            detail=f"missing JSON keys: {missing}",
        )
    return _result(
        criterion,
        passed=False,
        measured=False,
        detail="file_matches needs 'sha256' or 'json_keys'",
    )


def _eval_metric(
    criterion: dict[str, Any], envelope: dict[str, Any]
) -> dict[str, Any]:
    name = str(criterion.get("name", ""))
    metrics = envelope.get("metrics") or {}
    if name not in metrics:
        return _result(
            criterion, passed=False, measured=False, detail=f"metric {name!r} not reported"
        )
    raw = metrics[name]
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return _result(
            criterion,
            passed=False,
            measured=True,
            detail=f"metric {name!r} is not numeric: {raw!r}",
        )
    op = str(criterion.get("op", ">="))
    expected = criterion.get("value")
    if not isinstance(expected, (int, float)) or isinstance(expected, bool):
        return _result(
            criterion,
            passed=False,
            measured=False,
            detail=f"metric criterion {name!r} has no numeric 'value'",
        )
    try:
        passed = _compare(op, float(raw), float(expected))
    except ValueError as exc:
        return _result(criterion, passed=False, measured=False, detail=str(exc))
    return _result(
        criterion,
        passed=passed,
        measured=True,
        detail=f"{name} {raw} {op} {expected}",
    )


def _eval_tests_pass(
    criterion: dict[str, Any], workdir: Path
) -> dict[str, Any]:
    raw_suite = criterion.get("suite", "")
    suite = _resolve(str(raw_suite), workdir)
    timeout_s = float(criterion.get("timeout_s", 120))
    if not suite.exists():
        return _result(
            criterion,
            passed=False,
            measured=False,
            detail=f"test suite {suite} does not exist",
        )
    if suite.is_dir():
        cmd = [sys.executable, "-m", "unittest", "discover", "-s", str(suite)]
    else:
        cmd = [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            str(suite.parent),
            "-p",
            suite.name,
        ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, cwd=str(workdir)
        )
    except subprocess.TimeoutExpired:
        return _result(
            criterion,
            passed=False,
            measured=True,
            detail=f"test suite timed out after {timeout_s}s",
        )
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
    if proc.returncode == 0:
        return _result(
            criterion, passed=True, measured=True, detail="suite passed: " + " | ".join(tail)
        )
    return _result(
        criterion,
        passed=False,
        measured=True,
        detail="suite failed: " + " | ".join(tail),
    )


def _eval_custom_check(
    criterion: dict[str, Any],
    envelope: dict[str, Any],
    workdir: Path,
    checkers: dict[str, CustomChecker] | None,
) -> dict[str, Any]:
    name = str(criterion.get("name", ""))
    available = checkers if checkers is not None else _CUSTOM_CHECKERS
    checker = available.get(name)
    if checker is None:
        return _result(
            criterion,
            passed=False,
            measured=False,
            detail=f"unknown custom checker: {name!r}",
        )
    passed, detail = checker(envelope, workdir)
    return _result(criterion, passed=bool(passed), measured=True, detail=str(detail))


def evaluate_criterion(
    criterion: dict[str, Any],
    envelope: dict[str, Any],
    workdir: Path,
    checkers: dict[str, CustomChecker] | None = None,
) -> dict[str, Any]:
    """Evaluate one criterion deterministically against an envelope."""
    ctype = str(criterion.get("type", ""))
    if ctype == CRITERION_EXIT_CODE:
        return _eval_exit_code(criterion, envelope, workdir)
    if ctype == CRITERION_FILE_EXISTS:
        return _eval_file_exists(criterion, workdir)
    if ctype == CRITERION_FILE_MATCHES:
        return _eval_file_matches(criterion, workdir)
    if ctype == CRITERION_METRIC:
        return _eval_metric(criterion, envelope)
    if ctype == CRITERION_TESTS_PASS:
        return _eval_tests_pass(criterion, workdir)
    if ctype == CRITERION_CUSTOM_CHECK:
        return _eval_custom_check(criterion, envelope, workdir, checkers)
    return _result(
        criterion,
        passed=False,
        measured=False,
        detail=f"unknown criterion type: {ctype!r}",
    )


def build_envelope(
    node_id: str,
    handler_result: dict[str, Any] | None,
    *,
    duration_s: float,
    status: str = "completed",
    stdout: str = "",
    stderr: str = "",
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the structured result envelope for a finished node.

    The handler return value is only one field of the envelope — the
    executor assembles the rest from the process and the filesystem, so a
    handler cannot accidentally (or optimistically) hide a failure.
    """
    result = handler_result or {}
    exit_code = result.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        exit_code = None
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
    return {
        "node_id": node_id,
        "status": status,
        "exit_code": exit_code,
        "duration_s": round(float(duration_s), 3),
        "stdout": result.get("stdout", stdout) if isinstance(result.get("stdout", stdout), str) else stdout,
        "stderr": result.get("stderr", stderr) if isinstance(result.get("stderr", stderr), str) else stderr,
        "artifacts": artifacts,
        "metrics": metrics,
        "error": error,
    }


def measure_node(
    node: Any,
    envelope: dict[str, Any],
    workdir: Path | str,
    checkers: dict[str, CustomChecker] | None = None,
) -> dict[str, Any]:
    """Measure one executed node against its plan criteria.

    ``node`` needs ``id`` and ``criteria`` attributes (a PlanNode, or any
    object with those attributes). Returns a JSON-safe measurement record.
    """
    workdir = Path(workdir)
    criteria = list(getattr(node, "criteria", []) or [])
    results: list[dict[str, Any]] = []

    # The structural fix for false success: a non-zero exit code fails the
    # node even when the plan wrote no explicit exit_code criterion. When
    # the plan DOES declare an exit_code criterion, that evaluation covers
    # the same ground — the implicit check is skipped to avoid a duplicate.
    exit_code = envelope.get("exit_code")
    has_exit_criterion = any(
        isinstance(c, dict) and c.get("type") == CRITERION_EXIT_CODE
        for c in criteria
    )
    if exit_code is not None and exit_code != 0 and not has_exit_criterion:
        results.append(
            _result(
                {"type": CRITERION_EXIT_CODE, "equals": 0},
                passed=False,
                measured=True,
                detail=f"exit_code {exit_code} != 0",
            )
        )
    if envelope.get("status") not in ("completed", None):
        status = envelope.get("status")
        wording = {
            "timeout": "node timed out",
            "killed": "node was killed",
            "failed": "node failed during execution",
        }.get(str(status), f"node did not complete: status={status}")
        results.append(
            _result(
                {"type": "execution", "status": status},
                passed=False,
                measured=True,
                detail=wording,
            )
        )

    for criterion in criteria:
        results.append(evaluate_criterion(criterion, envelope, workdir, checkers))

    if any(not r["passed"] and r["measured"] for r in results):
        verdict = VERDICT_FAIL
    elif any(not r["measured"] for r in results):
        verdict = VERDICT_UNMEASURABLE
    else:
        verdict = VERDICT_PASS

    return {
        "node_id": getattr(node, "id", envelope.get("node_id")),
        "verdict": verdict,
        "results": results,
        "exit_code": exit_code,
        "duration_s": envelope.get("duration_s"),
        "metrics": dict(envelope.get("metrics") or {}),
        "envelope_status": envelope.get("status"),
        "measured_at": datetime.now(UTC).isoformat(),
    }


def measurement_failed_detail(measurement: dict[str, Any]) -> str:
    """One-line human summary of the first failing/unmeasurable criterion."""
    for result in measurement.get("results", []):
        if not result.get("passed"):
            kind = "unmeasurable" if not result.get("measured") else "failed"
            return f"{kind} criterion: {result.get('detail')}"
    return "measurement did not pass"
