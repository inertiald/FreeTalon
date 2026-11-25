"""Campaign build #16 (2026-11-24): probe status-code fidelity.

Arm #25 broke the 26-27 plateau (31/35) and extinguished the
transport/grounding failure classes — the pairing server's flows
complete. Its three remaining server misses were all status-code
exactness against CONTRACT.md, and the build #6/#12 smoke probe
saw none of them:

1. register success answered 200, the contract wants 201 for a
   new code — arm #25's plan itself removed the is_new->201
   branch, and the probe's `status in (200, 201)` check read
   the regression as a pass;
2. confirm with an empty bundle answered 200, the contract
   wants 400 (invalid bundle);
3. a wrong method on an UNKNOWN route answered 405, the
   contract wants 404 — the combination no flow exercised
   (build #12 had noted the gap for the method/route matrix).

The fix extends _pairing_smoke_flows to the contract's status
table, checked exactly: register is pinned to 201 for the
probe's fresh code and joined by register_idempotent (200, the
same row's other half), confirm_bad_credentials (400 on an
empty bundle, valid token, while the record is pending),
poll_claimed (410 with the claimed body — the poll table's
last unexercised row), and wrong_method_unknown_route (404).
A status failure's detail carries the machine-readable fact
("<flow>_status: expected X, got Y"); probe details already
reach the re-plan brief verbatim (planner renders
brief["contract_probe_failures"] records as
"- {clause_id} [{probe}]: {detail}") and the repair seams via
probe_suspect_map, so no new plumbing was needed — the fact
now exists to be carried.

Every expectation quotes CONTRACT.md (freetalon/pairing):
- HTTP API table: register "`201` new, `200` idempotent";
  confirm "`200` -> public view", errors "`400` invalid
  bundle, `401` unauthorized, `404` unknown, `409` already
  confirmed, `410` expired or claimed".
- Poll table: pending 404, unknown 404, confirmed 200,
  claimed 410, expired 410.
- Routing: "Unknown routes -> `404 {"error": "not found"}`;
  a known path with the wrong method -> `405`."

No GPU is touched: the probe tests boot fixture servers on
loopback, exactly as builds #6/#12 did.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator import planner as planner_module
from freetalon.orchestrator.contract_clauses import (
    probe_failures,
    run_contract_probes,
)
from tests.test_campaign_build6 import (
    FIXTURE_SERVER_GOOD,
    SMOKE_CLAUSES,
    _fixture_tree,
    _smoke_records,
)

# Arm #25's register defect: the creation answer is the
# idempotent one — a fresh registration answers 200.
FIXTURE_SERVER_REGISTER_200 = FIXTURE_SERVER_GOOD.replace(
    'self._json(201, {"code": code, "status": "pending"})',
    'self._json(200, {"code": code, "status": "pending"})',
)
assert FIXTURE_SERVER_REGISTER_200 != FIXTURE_SERVER_GOOD

# Arm #25's confirm defect: the bundle is never validated, so
# an empty bundle confirms (200) instead of answering 400.
FIXTURE_SERVER_CONFIRM_ANY_BUNDLE = FIXTURE_SERVER_GOOD.replace(
    '''            bundle = self._body().get("credentials")
            if not bundle:
                self._json(400, {"error": "invalid bundle"})
                return
            record["credentials"] = bundle
''',
    '''            record["credentials"] = self._body().get("credentials")
''',
)
assert FIXTURE_SERVER_CONFIRM_ANY_BUNDLE != FIXTURE_SERVER_GOOD

# Arm #25's routing defect: every non-GET/POST method answers
# 405 whatever the path — the wrong-method answer leaks onto
# unknown routes, where the contract owes a 404.
FIXTURE_SERVER_405_EVERYWHERE = FIXTURE_SERVER_GOOD.replace(
    '''        if self.path.startswith("/pairing"):
            self._json(405, {"error": "method not allowed"})
        else:
            self._json(404, {"error": "not found"})
''',
    '''        if self.path.startswith("/pairing"):
            self._json(405, {"error": "method not allowed"})
        else:
            self._json(405, {"error": "method not allowed"})
''',
)
assert FIXTURE_SERVER_405_EVERYWHERE != FIXTURE_SERVER_GOOD

EXPECTED_FLOW_ORDER = [
    "register",
    "register_idempotent",
    "poll_pending",
    "confirm_bad_credentials",
    "confirm",
    "poll_claim",
    "poll_claimed",
    "wrong_method",
    "unknown_route",
    "wrong_method_unknown_route",
]


def _flow(results, name, clause_id="C09-http-api-server-py"):
    return [
        r for r in _smoke_records(results, clause_id) if r.get("flow") == name
    ]


class TestStatusCodeFidelity(unittest.TestCase):
    def test_good_server_passes_all_ten_flows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_GOOD)
            results = run_contract_probes(SMOKE_CLAUSES, root)
            self.assertEqual(probe_failures(results), [])
            flows = _smoke_records(results)
            self.assertEqual(
                [r["flow"] for r in flows], EXPECTED_FLOW_ORDER
            )

    def test_register_200_fails_with_the_expected_got_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_REGISTER_200)
            results = run_contract_probes(SMOKE_CLAUSES, root)
            register = _flow(results, "register")
            self.assertEqual(len(register), 1)
            self.assertFalse(register[0]["ok"])
            self.assertIn(
                "register_status: expected 201, got 200",
                register[0]["detail"],
            )
            self.assertIn(register[0], probe_failures(results))
            # The idempotent half of the row still passes: the
            # repeat registration answers the same 200, which is
            # correct THERE — the defect is exactly the missing
            # creation/refresh distinction.
            idempotent = _flow(results, "register_idempotent")
            self.assertEqual(len(idempotent), 1)
            self.assertTrue(idempotent[0]["ok"])

    def test_register_failure_record_reaches_the_replan_prompt(self):
        # The channel is the brief's verbatim render: the exact
        # expected/got fact the planner sees, not a generic
        # clause failure.
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_REGISTER_200)
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
            self.assertIn("Contract probe failures (system, verbatim)", prompt)
            self.assertIn(
                "register_status: expected 201, got 200", prompt
            )

    def test_confirm_empty_bundle_200_fails_with_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_CONFIRM_ANY_BUNDLE)
            results = run_contract_probes(SMOKE_CLAUSES, root)
            bad = _flow(results, "confirm_bad_credentials")
            self.assertEqual(len(bad), 1)
            self.assertFalse(bad[0]["ok"])
            self.assertIn(
                "confirm_bad_credentials_status: expected 400, got 200",
                bad[0]["detail"],
            )
            self.assertIn(bad[0], probe_failures(results))
            # The defect is the only failure: the good confirm
            # that follows still lands, and the claim still
            # returns the real bundle.
            self.assertEqual(
                [r["flow"] for r in probe_failures(results)],
                ["confirm_bad_credentials"],
            )
            self.assertTrue(_flow(results, "confirm")[0]["ok"])
            self.assertTrue(_flow(results, "poll_claim")[0]["ok"])

    def test_wrong_method_on_unknown_route_405_fails_with_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_tree(Path(tmp), FIXTURE_SERVER_405_EVERYWHERE)
            results = run_contract_probes(SMOKE_CLAUSES, root)
            flow = _flow(results, "wrong_method_unknown_route")
            self.assertEqual(len(flow), 1)
            self.assertFalse(flow[0]["ok"])
            self.assertIn(
                "wrong_method_unknown_route_status: expected 404, got 405",
                flow[0]["detail"],
            )
            self.assertIn(flow[0], probe_failures(results))
            # The two halves of the routing rule the probe
            # already had still pass against this fixture: the
            # known path's wrong method IS 405, and a GET on the
            # unknown path IS 404 — only the combination was
            # unobserved.
            self.assertTrue(_flow(results, "wrong_method")[0]["ok"])
            self.assertTrue(_flow(results, "unknown_route")[0]["ok"])
            self.assertEqual(
                [r["flow"] for r in probe_failures(results)],
                ["wrong_method_unknown_route"],
            )


if __name__ == "__main__":
    unittest.main()
