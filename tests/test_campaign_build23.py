"""Campaign build #23 (2026-11-25): the live method-matrix probe.

The method-coverage diagnosis (2026-11-25) found pairing's
wrong-method miss invisible to every deterministic check: the
static method_surface probe passes once ANY handler beyond
do_GET/do_POST exists, and the build #6/#16 smoke probe sampled
exactly one wrong method — PUT, the one the loop routes
correctly — while PATCH was plan-absent in 6/6 plan versions
and DELETE/HEAD answered a blanket 404 where CONTRACT.md
(ll.175-176: "Unknown routes → 404; a known path with the wrong
method → 405") wants 405 on known paths. On held-out task A the
same rule shape held and only the static probe testified.

The matrix exercises every closed-set method beyond GET/POST
(PUT, DELETE, PATCH, HEAD, OPTIONS) against one known path and
one unknown path, with expectations derived from the contract's
own 405/404 rule. Records carry probe="method_matrix", the
flow name, and the server file, so failures enter the existing
probe-failure channel (contract_probe_failures → re-plan
briefs, probe_suspect_map → repair seams) unchanged. The
jobqueue family gets a matrix-only live runner on the same
logic, which is what the held-out A driver executes.

No GPU is touched: the probe tests boot fixture servers on
loopback, exactly as builds #6/#12/#16 did.

Build #24 note (2026-11-25): the matrix was widened from the
known/unknown pair to each family's full route table (see
tests/test_campaign_build24.py). This file's expectations were
updated where the grid widened: the scripted unit tests pin a
primary route defining GET+POST, so build #23's ten flows are
exactly their expectation; the live flow lists grew (pairing
10 -> 29, jobqueue 10 -> 34); the NO_PATCH fixture now fails
the whole PATCH column, not two cells; and the jobqueue GOOD
fixture gained a real submit plus shape-true do_GET/do_POST,
because the sweep mints the job id it sweeps against.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.contract_clauses import (
    _method_matrix_flows,
    probe_failures,
    probe_suspect_map,
    run_contract_probes,
)
from tests.test_campaign_build6 import (
    FIXTURE_SERVER_GOOD,
    SMOKE_CLAUSES,
    _fixture_tree,
    _write,
)

# Arm #29/#30's pairing defect, one half: no do_PATCH anywhere —
# PATCH falls through to the stdlib 501 on every path.
FIXTURE_SERVER_NO_PATCH = FIXTURE_SERVER_GOOD.replace(
    '''    def do_PATCH(self):
        if self.path.startswith("/pairing"):
            self._json(405, {"error": "method not allowed"})
        else:
            self._json(404, {"error": "not found"})

''',
    "",
)
assert FIXTURE_SERVER_NO_PATCH != FIXTURE_SERVER_GOOD

# Arm #29/#30's pairing defect, the other half: do_DELETE exists
# but answers a blanket 404 — no known/unknown split, so a known
# path gets the unknown-route answer instead of the contract's
# 405.
FIXTURE_SERVER_BLANKET_404_DELETE = FIXTURE_SERVER_GOOD.replace(
    '''    def do_DELETE(self):
        if self.path.startswith("/pairing"):
            self._json(405, {"error": "method not allowed"})
        else:
            self._json(404, {"error": "not found"})
''',
    '''    def do_DELETE(self):
        self._json(404, {"error": "not found"})
''',
)
assert FIXTURE_SERVER_BLANKET_404_DELETE != FIXTURE_SERVER_GOOD

MATRIX_METHODS = ("put", "delete", "patch", "head", "options")
EXPECTED_MATRIX_FLOWS = [
    f"method_matrix_{method}_{side}"
    for method in MATRIX_METHODS
    for side in ("known", "unknown")
]

# Build #24: the full route-table flow lists, in emission order
# (build #23's ten first, then the sweep in route-table order,
# methods in closed-set order within each route).
_SWEEP_METHODS = ("get", "post", "put", "delete", "patch", "head", "options")


def _sweep_flows(tag, defined):
    return [
        f"method_matrix_{method}_on_{tag}"
        for method in _SWEEP_METHODS
        if method not in defined
    ]


EXPECTED_PAIRING_MATRIX_FLOWS = (
    EXPECTED_MATRIX_FLOWS
    + ["method_matrix_get_known"]
    + _sweep_flows("list", {"get"})
    + _sweep_flows("poll", {"get"})
    + _sweep_flows("confirm", {"post"})
)
EXPECTED_JOBQUEUE_MATRIX_FLOWS = (
    EXPECTED_MATRIX_FLOWS
    + _sweep_flows("claim", {"post"})
    + _sweep_flows("item", {"get"})
    + _sweep_flows("complete", {"post"})
    + _sweep_flows("fail", {"post"})
)


def _control_flows(tag, defined):
    suffix = "known" if tag == "known" else f"on_{tag}"
    return [
        f"positive_control_{method}_{suffix}"
        for method in _SWEEP_METHODS
        if method in defined
    ]


# Build #34: the positive controls ride the same expected lists,
# appended after the sweep in route-table order (one per defined
# method per route).
EXPECTED_PAIRING_MATRIX_FLOWS = (
    EXPECTED_PAIRING_MATRIX_FLOWS
    + _control_flows("known", {"post"})
    + _control_flows("list", {"get"})
    + _control_flows("poll", {"get"})
    + _control_flows("confirm", {"post"})
)
EXPECTED_JOBQUEUE_MATRIX_FLOWS = (
    EXPECTED_JOBQUEUE_MATRIX_FLOWS
    + _control_flows("known", {"get", "post"})
    + _control_flows("claim", {"post"})
    + _control_flows("item", {"get"})
    + _control_flows("complete", {"post"})
    + _control_flows("fail", {"post"})
)


def _matrix_records(results, clause_id="C09-http-api-server-py"):
    return [
        r
        for r in results
        if r["probe"] == "method_matrix" and r["clause_id"] == clause_id
    ]


def _matrix_flow(results, name, clause_id="C09-http-api-server-py"):
    return [
        r for r in _matrix_records(results, clause_id) if r.get("flow") == name
    ]


class TestMethodMatrixUnit(unittest.TestCase):
    """The matrix logic itself, against a scripted requester."""

    def _requester(self, statuses):
        def request(name, method, path):
            return (statuses[(method, path)], b""), None

        return request

    def test_correct_server_passes_all_ten(self):
        statuses = {}
        for method in ("PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            statuses[(method, "/known")] = 405
            statuses[(method, "/unknown")] = 404
        # Build #34: the positive controls sample the defined
        # pairs; a correct server answers them with its own
        # statuses.
        statuses[("GET", "/known")] = 200
        statuses[("POST", "/known")] = 201
        flows = _method_matrix_flows(
            self._requester(statuses),
            routes=[("/known", frozenset({"GET", "POST"}), "known")],
            unknown_path="/unknown",
        )
        self.assertEqual(
            [name for name, _ok, _d in flows],
            EXPECTED_MATRIX_FLOWS
            + ["positive_control_get_known", "positive_control_post_known"],
        )
        self.assertTrue(all(ok for _name, ok, _d in flows))

    def test_missing_patch_fails_with_the_501_fact(self):
        statuses = {}
        for method in ("PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            statuses[(method, "/known")] = 405
            statuses[(method, "/unknown")] = 404
        # Build #34: the positive controls sample the defined
        # pairs; a correct server answers them with its own
        # statuses.
        statuses[("GET", "/known")] = 200
        statuses[("POST", "/known")] = 201
        statuses[("PATCH", "/known")] = 501
        statuses[("PATCH", "/unknown")] = 501
        flows = dict()
        for name, ok, detail in _method_matrix_flows(
            self._requester(statuses),
            routes=[("/known", frozenset({"GET", "POST"}), "known")],
            unknown_path="/unknown",
        ):
            flows[name] = (ok, detail)
        ok, detail = flows["method_matrix_patch_known"]
        self.assertFalse(ok)
        self.assertIn(
            "method_matrix_patch_known_status: expected 405, got 501",
            detail,
        )
        self.assertIn("no do_PATCH handler is defined", detail)
        ok, detail = flows["method_matrix_patch_unknown"]
        self.assertFalse(ok)
        self.assertIn(
            "method_matrix_patch_unknown_status: expected 404, got 501",
            detail,
        )
        # Every other method still passes: the failure is
        # per-method, not a blanket matrix failure.
        self.assertTrue(
            all(
                ok
                for name, (ok, _d) in flows.items()
                if not name.startswith("method_matrix_patch")
            )
        )

    def test_blanket_404_delete_fails_as_405_expected(self):
        statuses = {}
        for method in ("PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            statuses[(method, "/known")] = 405
            statuses[(method, "/unknown")] = 404
        # Build #34: the positive controls sample the defined
        # pairs; a correct server answers them with its own
        # statuses.
        statuses[("GET", "/known")] = 200
        statuses[("POST", "/known")] = 201
        statuses[("DELETE", "/known")] = 404
        flows = {
            name: (ok, detail)
            for name, ok, detail in _method_matrix_flows(
                self._requester(statuses),
                routes=[("/known", frozenset({"GET", "POST"}), "known")],
                unknown_path="/unknown",
            )
        }
        ok, detail = flows["method_matrix_delete_known"]
        self.assertFalse(ok)
        self.assertIn(
            "method_matrix_delete_known_status: expected 405, got 404",
            detail,
        )
        self.assertIn("blanket", detail)
        # The unknown half is genuinely correct and passes.
        self.assertTrue(flows["method_matrix_delete_unknown"][0])


class TestPairingMethodMatrix(unittest.TestCase):
    def test_good_server_passes_the_whole_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_GOOD)
            results = run_contract_probes(SMOKE_CLAUSES, root)
            matrix = _matrix_records(results)
            self.assertEqual(
                [r["flow"] for r in matrix], EXPECTED_PAIRING_MATRIX_FLOWS
            )
            self.assertTrue(all(r["ok"] for r in matrix))
            for record in matrix:
                self.assertEqual(record["file"], "freetalon/pairing/server.py")
            self.assertEqual(probe_failures(results), [])

    def test_missing_patch_fails_the_matrix_with_the_right_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_NO_PATCH)
            results = run_contract_probes(SMOKE_CLAUSES, root)
            known = _matrix_flow(results, "method_matrix_patch_known")
            self.assertEqual(len(known), 1)
            self.assertFalse(known[0]["ok"])
            self.assertIn(
                "method_matrix_patch_known_status: expected 405, got 501",
                known[0]["detail"],
            )
            self.assertIn(known[0], probe_failures(results))
            unknown = _matrix_flow(results, "method_matrix_patch_unknown")
            self.assertEqual(len(unknown), 1)
            self.assertFalse(unknown[0]["ok"])
            # The matrix failure is the ONLY failure: the static
            # surface probe and the ten functional flows all pass
            # against this fixture — before build #23, no check
            # the loop runs could see the defect at all. Since
            # build #24 the failure is the whole PATCH column:
            # the sweep samples PATCH on every route, and this
            # fixture has no do_PATCH anywhere.
            self.assertEqual(
                {r["flow"] for r in probe_failures(results)},
                {
                    "method_matrix_patch_known",
                    "method_matrix_patch_unknown",
                    "method_matrix_patch_on_list",
                    "method_matrix_patch_on_poll",
                    "method_matrix_patch_on_confirm",
                },
            )

    def test_blanket_404_delete_fails_as_405_expected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_BLANKET_404_DELETE)
            results = run_contract_probes(SMOKE_CLAUSES, root)
            known = _matrix_flow(results, "method_matrix_delete_known")
            self.assertEqual(len(known), 1)
            self.assertFalse(known[0]["ok"])
            self.assertIn(
                "method_matrix_delete_known_status: expected 405, got 404",
                known[0]["detail"],
            )
            self.assertIn(known[0], probe_failures(results))
            self.assertTrue(
                _matrix_flow(results, "method_matrix_delete_unknown")[0]["ok"]
            )

    def test_matrix_failure_names_the_server_as_suspect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_NO_PATCH)
            results = run_contract_probes(SMOKE_CLAUSES, root)
            suspect = probe_suspect_map(probe_failures(results))
            self.assertIsNotNone(suspect)
            self.assertEqual(suspect["file"], "freetalon/pairing/server.py")
            self.assertEqual(
                {f["flow"] for f in suspect["flows"]},
                {
                    "method_matrix_patch_known",
                    "method_matrix_patch_unknown",
                    "method_matrix_patch_on_list",
                    "method_matrix_patch_on_poll",
                    "method_matrix_patch_on_confirm",
                },
            )

    def test_matrix_failure_reaches_the_replan_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_NO_PATCH)
            results = run_contract_probes(SMOKE_CLAUSES, root)
            failures = probe_failures(results)
            self.assertTrue(failures)
            brief = {
                "goal": {"objective": "pairing goal"},
                "plan_id": "plan-x",
                "plan_version": 1,
                "contract_probe_failures": failures,
            }
            prompt = planner_module._format_brief_for_prompt(brief)
            self.assertIn(
                "method_matrix_patch_known_status: expected 405, got 501",
                prompt,
            )

    def test_matrix_passes_against_the_production_pairing_server(self):
        repo_root = Path(__file__).resolve().parents[1]
        results = run_contract_probes(SMOKE_CLAUSES, repo_root)
        matrix = _matrix_records(results)
        self.assertEqual(
            [r["flow"] for r in matrix], EXPECTED_PAIRING_MATRIX_FLOWS
        )
        self.assertTrue(all(r["ok"] for r in matrix))
        self.assertEqual(probe_failures(results), [])


# ── Jobqueue family (held-out task A's contract shape) ─────────

FIXTURE_JOBQUEUE_STORE = '''"""Fixture jobqueue store: construction only."""


class JobQueueStore:
    def __init__(self, path):
        self.path = path
'''

FIXTURE_JOBQUEUE_SERVER_GOOD = '''"""Fixture jobqueue server: the contract surface, minimally.

Build #24: the route-table sweep mints the job id it sweeps
against with a real submit, and samples wrong methods on every
route shape — so the fixture keeps a minimal in-memory store
and routes every method by path shape, the way CONTRACT-A's
rule reads.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    jobs = {}
    next_id = 1

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode() or "{}")
        except ValueError:
            return {}

    def _shape(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if parts == ["queue", "jobs"]:
            return "jobs"
        if parts == ["queue", "claim"]:
            return "claim"
        if len(parts) == 3 and parts[:2] == ["queue", "jobs"]:
            return "item"
        if (
            len(parts) == 4
            and parts[:2] == ["queue", "jobs"]
            and parts[3] in ("complete", "fail")
        ):
            return parts[3]
        return None

    def _job_id(self):
        return self.path.split("?")[0].split("/")[3]

    def _reject(self):
        if self._shape() is not None:
            self._json(405, {"error": "method not allowed"})
        else:
            self._json(404, {"error": "not found"})

    def do_GET(self):
        shape = self._shape()
        if shape == "jobs":
            self._json(200, {"jobs": list(type(self).jobs.values())})
        elif shape == "item":
            job = type(self).jobs.get(self._job_id())
            if job is None:
                self._json(404, {"error": "not found"})
            else:
                self._json(200, job)
        elif shape is not None:
            self._json(405, {"error": "method not allowed"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        shape = self._shape()
        if shape == "jobs":
            body = self._body()
            if "payload" not in body:
                self._json(400, {"error": "invalid job"})
                return
            job_id = "job-%06d" % type(self).next_id
            type(self).next_id += 1
            job = {
                "id": job_id,
                "payload": body["payload"],
                "status": "queued",
            }
            type(self).jobs[job_id] = job
            self._json(201, job)
        elif shape == "claim":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif shape in ("complete", "fail"):
            if self._job_id() not in type(self).jobs:
                self._json(404, {"error": "not found"})
            else:
                self._json(409, {"error": "not claimed"})
        elif shape is not None:
            self._json(405, {"error": "method not allowed"})
        else:
            self._json(404, {"error": "not found"})

    def do_PUT(self):
        self._reject()

    def do_DELETE(self):
        self._reject()

    def do_PATCH(self):
        self._reject()

    def do_HEAD(self):
        self._reject()

    def do_OPTIONS(self):
        self._reject()


def make_server(store, host="127.0.0.1", port=0):
    return ThreadingHTTPServer((host, port), _Handler)
'''

# Task A's shipped defect in all three failing runs: only
# do_GET/do_POST — DELETE (and every other method) falls through
# to the stdlib 501.
FIXTURE_JOBQUEUE_SERVER_NO_DELETE = FIXTURE_JOBQUEUE_SERVER_GOOD.replace(
    '''    def do_DELETE(self):
        self._reject()

''',
    "",
)
assert FIXTURE_JOBQUEUE_SERVER_NO_DELETE != FIXTURE_JOBQUEUE_SERVER_GOOD

JOBQUEUE_CLAUSES = [
    {
        "id": "C09-http-api-server-py",
        "spec": "freetalon/jobqueue/CONTRACT.md",
        "heading": "HTTP API (server.py)",
        "text": "## HTTP API (server.py)\n",
        "declared_files": ["freetalon/jobqueue/server.py"],
        "method_surface": True,
    }
]


def _jobqueue_tree(root: Path, server_text: str) -> Path:
    package = root / "freetalon" / "jobqueue"
    _write(root / "freetalon" / "__init__.py", "")
    _write(package / "__init__.py", "")
    _write(package / "store.py", FIXTURE_JOBQUEUE_STORE)
    _write(package / "server.py", server_text)
    return root


class TestJobqueueMethodMatrix(unittest.TestCase):
    def test_good_jobqueue_server_passes_the_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _jobqueue_tree(Path(tmp), FIXTURE_JOBQUEUE_SERVER_GOOD)
            results = run_contract_probes(JOBQUEUE_CLAUSES, root)
            matrix = _matrix_records(results)
            self.assertEqual(
                [r["flow"] for r in matrix], EXPECTED_JOBQUEUE_MATRIX_FLOWS
            )
            self.assertTrue(all(r["ok"] for r in matrix))
            for record in matrix:
                self.assertEqual(
                    record["file"], "freetalon/jobqueue/server.py"
                )

    def test_missing_delete_fails_the_matrix_with_the_right_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _jobqueue_tree(Path(tmp), FIXTURE_JOBQUEUE_SERVER_NO_DELETE)
            results = run_contract_probes(JOBQUEUE_CLAUSES, root)
            known = _matrix_flow(results, "method_matrix_delete_known")
            self.assertEqual(len(known), 1)
            self.assertFalse(known[0]["ok"])
            self.assertIn(
                "method_matrix_delete_known_status: expected 405, got 501",
                known[0]["detail"],
            )
            self.assertIn(known[0], probe_failures(results))
            suspect = probe_suspect_map(probe_failures(results))
            self.assertIsNotNone(suspect)
            self.assertEqual(suspect["file"], "freetalon/jobqueue/server.py")


if __name__ == "__main__":
    unittest.main()
