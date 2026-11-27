"""Campaign build #24 (2026-11-25): the route-table method sweep.

Build #23's matrix sampled one known path per family. Arm #32
then failed on a cell the pair never sampled — GET on
/pairing/<code>/confirm answered 404 where CONTRACT.md wants
405 — while passing all ten matrix flows: the grid was one path
deep. The sub-path grid diagnosis (2026-11-25) counted the full
grids from the contracts' own route tables: pairing 24
wrong-method cells (12 on parameterized sub-paths), jobqueue 29
(18 on sub-paths), of which the pair sampled 5 each.

The sweep generalizes _method_matrix_flows to a per-family
route table: every contract route x every closed-set method the
route does not define -> 405; the unknown-path column is
unchanged. Build #23's ten flow names are preserved; new cells
are method_matrix_<method>_on_<route tag> (plus
method_matrix_get_known for the primary route's added cell).
The jobqueue runner mints a real job id with one setup submit
before sweeping the parameterized routes. Expectations come
from the contracts' 405/404 rules — the 405 decision is by path
SHAPE before any store lookup (the pairing acceptance test
asserts 405 on routes for a code that was never registered).

No GPU is touched: the probe tests boot fixture servers on
loopback, exactly as builds #6/#12/#16/#23 did.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.contract_clauses import (
    _method_matrix_flows,
    probe_failures,
    run_contract_probes,
)
from tests.test_campaign_build6 import (
    FIXTURE_SERVER_GOOD,
    SMOKE_CLAUSES,
    _fixture_tree,
)
from tests.test_campaign_build23 import (
    EXPECTED_JOBQUEUE_MATRIX_FLOWS,
    EXPECTED_PAIRING_MATRIX_FLOWS,
    FIXTURE_JOBQUEUE_SERVER_GOOD,
    JOBQUEUE_CLAUSES,
    _jobqueue_tree,
    _matrix_flow,
    _matrix_records,
)

UNKNOWN = "/smoke-probe/no-such-route"

PAIRING_ROUTES = [
    ("/pairing/register", frozenset({"POST"}), "known"),
    ("/pairing", frozenset({"GET"}), "list"),
    ("/pairing/SMOKE-0001", frozenset({"GET"}), "poll"),
    ("/pairing/SMOKE-0001/confirm", frozenset({"POST"}), "confirm"),
]

JOBQUEUE_ROUTES = [
    ("/queue/jobs", frozenset({"GET", "POST"}), "known"),
    ("/queue/claim", frozenset({"POST"}), "claim"),
    ("/queue/jobs/job-000001", frozenset({"GET"}), "item"),
    ("/queue/jobs/job-000001/complete", frozenset({"POST"}), "complete"),
    ("/queue/jobs/job-000001/fail", frozenset({"POST"}), "fail"),
]


def _defined_pairs(routes):
    return {
        (method, path)
        for path, methods, _tag in routes
        for method in methods
        if path is not None
    }


def _correct_requester(calls, overrides=None, defined=None):
    """A scripted requester for a contract-perfect server.

    405 on every known path's wrong methods, 404 on the unknown
    path, and the route's own answer (200) on its defined
    (method, path) pairs — build #34's positive controls sample
    those. Overrides win. Records every call.
    """
    overrides = overrides or {}
    defined = defined or set()

    def request(name, method, path):
        calls.append((method, path))
        if (method, path) in overrides:
            return (overrides[(method, path)], b""), None
        if (method, path) in defined:
            return (200, b""), None
        return ((404 if path == UNKNOWN else 405), b""), None

    return request


class TestRouteTableUnits(unittest.TestCase):
    """The sweep logic itself, against a scripted requester."""

    def test_pairing_table_correct_server_passes_all_33(self):
        calls = []
        flows = _method_matrix_flows(
            _correct_requester(
                calls, defined=_defined_pairs(PAIRING_ROUTES)
            ),
            routes=PAIRING_ROUTES,
            unknown_path=UNKNOWN,
        )
        self.assertEqual(
            [name for name, _ok, _d in flows], EXPECTED_PAIRING_MATRIX_FLOWS
        )
        self.assertTrue(all(ok for _name, ok, _d in flows))
        # The negative sweep samples only wrong-method cells —
        # never a defined pair. Build #34's positive controls
        # then sample each defined pair exactly once.
        defined = _defined_pairs(PAIRING_ROUTES)
        negative_calls = calls[: len(calls) - len(defined)]
        self.assertTrue(defined.isdisjoint(set(negative_calls)))
        self.assertEqual(
            sorted(calls[len(calls) - len(defined) :]), sorted(defined)
        )
        self.assertEqual(len(calls), 33)

    def test_jobqueue_table_correct_server_passes_all_40(self):
        calls = []
        flows = _method_matrix_flows(
            _correct_requester(
                calls, defined=_defined_pairs(JOBQUEUE_ROUTES)
            ),
            routes=JOBQUEUE_ROUTES,
            unknown_path=UNKNOWN,
        )
        self.assertEqual(
            [name for name, _ok, _d in flows], EXPECTED_JOBQUEUE_MATRIX_FLOWS
        )
        self.assertTrue(all(ok for _name, ok, _d in flows))
        defined = _defined_pairs(JOBQUEUE_ROUTES)
        negative_calls = calls[: len(calls) - len(defined)]
        self.assertTrue(defined.isdisjoint(set(negative_calls)))
        self.assertEqual(
            sorted(calls[len(calls) - len(defined) :]), sorted(defined)
        )
        self.assertEqual(len(calls), 40)

    def test_arm32_cell_get_on_confirm_404_is_caught(self):
        # Arm #32's exact miss, in isolation: the shipped server
        # answered GET /pairing/<code>/confirm with the
        # unknown-route 404 instead of the contract's 405, and
        # every pre-#24 check passed it.
        calls = []
        flows = _method_matrix_flows(
            _correct_requester(
                calls,
                overrides={("GET", "/pairing/SMOKE-0001/confirm"): 404},
                defined=_defined_pairs(PAIRING_ROUTES),
            ),
            routes=PAIRING_ROUTES,
            unknown_path=UNKNOWN,
        )
        failing = [(n, d) for n, ok, d in flows if not ok]
        self.assertEqual([n for n, _d in failing], ["method_matrix_get_on_confirm"])
        self.assertIn(
            "method_matrix_get_on_confirm_status: expected 405, got 404",
            failing[0][1],
        )
        self.assertIn("GET /pairing/SMOKE-0001/confirm -> 404", failing[0][1])

    def test_blocked_routes_fail_with_the_setup_detail(self):
        routes = [
            ("/queue/jobs", frozenset({"GET", "POST"}), "known"),
            ("/queue/claim", frozenset({"POST"}), "claim"),
            (None, frozenset({"GET"}), "item"),
            (None, frozenset({"POST"}), "complete"),
            (None, frozenset({"POST"}), "fail"),
        ]
        calls = []
        flows = _method_matrix_flows(
            _correct_requester(calls, defined=_defined_pairs(routes)),
            routes=routes,
            unknown_path=UNKNOWN,
            blocked_detail="setup: no id minted",
        )
        failing = {n for n, ok, _d in flows if not ok}
        blocked = {
            n
            for n in EXPECTED_JOBQUEUE_MATRIX_FLOWS
            if n.endswith(("_on_item", "_on_complete", "_on_fail"))
        }
        self.assertEqual(failing, blocked)
        for name, ok, detail in flows:
            if name in blocked:
                self.assertEqual(detail, "setup: no id minted")
        # No request was issued for a blocked cell: the calls
        # are the collection + claim cells only, negative and
        # positive alike (40 cells - 21 blocked: the 18 negative
        # item-route cells plus their 3 positive controls).
        self.assertEqual(len(calls), 40 - 21)
        self.assertTrue(all(path is not None for _m, path in calls))


# ── Single-defect fixtures ─────────────────────────────────────

# The pairing defect class behind arm #32, as a fixture: shape
# dispatch in do_GET misses the confirm branch, so a GET on a
# confirm route answers the unknown-route 404. Everything else
# about the server is the GOOD fixture.
FIXTURE_SERVER_CONFIRM_GET_404 = FIXTURE_SERVER_GOOD.replace(
    '''        if self.path == "/pairing/register" or (
            self.path.startswith("/pairing/")
            and self.path.endswith("/confirm")
        ):
            self._json(405, {"error": "method not allowed"})
            return
''',
    '''        if self.path == "/pairing/register":
            self._json(405, {"error": "method not allowed"})
            return
        if self.path.startswith("/pairing/") and self.path.endswith(
            "/confirm"
        ):
            self._json(404, {"error": "not found"})
            return
''',
)
assert FIXTURE_SERVER_CONFIRM_GET_404 != FIXTURE_SERVER_GOOD

# CONTRACT-A's pinned cell as a fixture defect: a bare POST to
# the item route answers 404 instead of 405. Everything else
# about the server is the GOOD jobqueue fixture.
FIXTURE_JOBQUEUE_SERVER_ITEM_POST_404 = FIXTURE_JOBQUEUE_SERVER_GOOD.replace(
    '''        elif shape in ("complete", "fail"):
            if self._job_id() not in type(self).jobs:
                self._json(404, {"error": "not found"})
            else:
                self._json(409, {"error": "not claimed"})
        elif shape is not None:
            self._json(405, {"error": "method not allowed"})
''',
    '''        elif shape in ("complete", "fail"):
            if self._job_id() not in type(self).jobs:
                self._json(404, {"error": "not found"})
            else:
                self._json(409, {"error": "not claimed"})
        elif shape is not None:
            self._json(404, {"error": "not found"})
''',
)
assert FIXTURE_JOBQUEUE_SERVER_ITEM_POST_404 != FIXTURE_JOBQUEUE_SERVER_GOOD

# A jobqueue server whose submit is refused outright: the sweep
# cannot mint an id, so the item-route cells must fail with the
# setup detail while the rest of the grid still runs.
FIXTURE_JOBQUEUE_SERVER_SUBMIT_400 = FIXTURE_JOBQUEUE_SERVER_GOOD.replace(
    '            self._json(201, job)',
    '            self._json(400, {"error": "invalid job"})',
)
assert FIXTURE_JOBQUEUE_SERVER_SUBMIT_400 != FIXTURE_JOBQUEUE_SERVER_GOOD


class TestSingleDefectFixtures(unittest.TestCase):
    def test_pairing_confirm_get_404_caught_by_exactly_one_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_CONFIRM_GET_404)
            results = run_contract_probes(SMOKE_CLAUSES, root)
            failures = probe_failures(results)
            self.assertEqual(
                [r.get("flow") for r in failures],
                ["method_matrix_get_on_confirm"],
            )
            self.assertIn(
                "method_matrix_get_on_confirm_status: expected 405, got 404",
                failures[0]["detail"],
            )
            self.assertEqual(failures[0]["file"], "freetalon/pairing/server.py")
            # Every pre-#24 flow — the functional flows and build
            # #23's ten matrix cells — passes this fixture: the
            # defect was invisible before the sweep.
            matrix = _matrix_records(results)
            self.assertTrue(
                all(
                    r["ok"]
                    for r in matrix
                    if r["flow"] != "method_matrix_get_on_confirm"
                )
            )

    def test_jobqueue_item_post_404_caught_by_exactly_one_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _jobqueue_tree(
                Path(tmp), FIXTURE_JOBQUEUE_SERVER_ITEM_POST_404
            )
            results = run_contract_probes(JOBQUEUE_CLAUSES, root)
            failures = probe_failures(results)
            self.assertEqual(
                [r.get("flow") for r in failures],
                ["method_matrix_post_on_item"],
            )
            self.assertIn(
                "method_matrix_post_on_item_status: expected 405, got 404",
                failures[0]["detail"],
            )
            # The sweep ran against the id the setup submit
            # minted (the fixture store's first job).
            self.assertIn("/queue/jobs/job-000001", failures[0]["detail"])
            self.assertEqual(failures[0]["file"], "freetalon/jobqueue/server.py")

    def test_jobqueue_setup_failure_fails_only_the_item_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _jobqueue_tree(Path(tmp), FIXTURE_JOBQUEUE_SERVER_SUBMIT_400)
            results = run_contract_probes(JOBQUEUE_CLAUSES, root)
            matrix = _matrix_records(results)
            self.assertEqual(
                [r["flow"] for r in matrix], EXPECTED_JOBQUEUE_MATRIX_FLOWS
            )
            failing = {r["flow"] for r in matrix if not r["ok"]}
            blocked = {
                n
                for n in EXPECTED_JOBQUEUE_MATRIX_FLOWS
                if n.endswith(("_on_item", "_on_complete", "_on_fail"))
            }
            self.assertEqual(failing, blocked)
            for record in matrix:
                if record["flow"] in blocked:
                    self.assertIn("method_matrix_setup", record["detail"])


class TestZeroFalsePositives(unittest.TestCase):
    def test_production_pairing_server_passes_the_full_sweep(self):
        repo_root = Path(__file__).resolve().parents[1]
        results = run_contract_probes(SMOKE_CLAUSES, repo_root)
        matrix = _matrix_records(results)
        self.assertEqual(
            [r["flow"] for r in matrix], EXPECTED_PAIRING_MATRIX_FLOWS
        )
        self.assertTrue(all(r["ok"] for r in matrix))
        record = _matrix_flow(results, "method_matrix_get_on_confirm")
        self.assertEqual(len(record), 1)
        self.assertTrue(record[0]["ok"])
        self.assertEqual(probe_failures(results), [])

    def test_good_jobqueue_fixture_passes_the_full_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _jobqueue_tree(Path(tmp), FIXTURE_JOBQUEUE_SERVER_GOOD)
            results = run_contract_probes(JOBQUEUE_CLAUSES, root)
            matrix = _matrix_records(results)
            self.assertEqual(
                [r["flow"] for r in matrix], EXPECTED_JOBQUEUE_MATRIX_FLOWS
            )
            self.assertTrue(all(r["ok"] for r in matrix))
            self.assertEqual(probe_failures(results), [])


if __name__ == "__main__":
    unittest.main()
