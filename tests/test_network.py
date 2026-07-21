"""Tests for freetalon/orchestrator/claws/network.py.

ADR 0000 Task 1.3 — Netmiko Configuration Module with HITL gate.

These tests are fully hermetic: they do not require netmiko to be installed,
they make no real network connections, and they run deterministically.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from freetalon.audit import AuditLogger
from freetalon.orchestrator.claws.network import (
    ALLOWED_DEVICE_TYPES,
    Approval,
    ApprovalError,
    ConfigPlan,
    render_plan,
    sanitize_network_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_valid_payload(**overrides: object) -> dict:
    base = {
        "host": "192.168.1.1",
        "device_type": "cisco_ios",
        "port": 22,
        "config_lines": ["interface GigabitEthernet0/1", "shutdown"],
    }
    base.update(overrides)
    return base


def _temp_audit() -> tuple[AuditLogger, Path]:
    tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
    tmp.close()
    path = Path(tmp.name)
    return AuditLogger(path=path), path


def _read_audit(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ---------------------------------------------------------------------------
# sanitize_network_config — input validation
# ---------------------------------------------------------------------------


class SanitizeNetworkConfigTests(unittest.TestCase):
    # --- host ---

    def test_valid_ipv4(self) -> None:
        clean = sanitize_network_config(_make_valid_payload(host="10.0.0.1"))
        self.assertEqual(clean["host"], "10.0.0.1")

    def test_valid_hostname(self) -> None:
        clean = sanitize_network_config(_make_valid_payload(host="switch-core-01"))
        self.assertEqual(clean["host"], "switch-core-01")

    def test_rejects_empty_host(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_network_config(_make_valid_payload(host=""))

    def test_rejects_host_with_special_chars(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_network_config(_make_valid_payload(host="../../etc/passwd"))

    def test_rejects_host_with_spaces(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_network_config(_make_valid_payload(host="192.168.1.1 evil"))

    def test_rejects_host_with_semicolon(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_network_config(_make_valid_payload(host="192.168.1.1;rm -rf /"))

    # --- device_type ---

    def test_valid_device_types(self) -> None:
        for dt in ALLOWED_DEVICE_TYPES:
            clean = sanitize_network_config(_make_valid_payload(device_type=dt))
            self.assertEqual(clean["device_type"], dt)

    def test_rejects_disallowed_device_type(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_network_config(_make_valid_payload(device_type="linux"))

    def test_rejects_empty_device_type(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_network_config(_make_valid_payload(device_type=""))

    def test_rejects_unknown_device_type(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_network_config(_make_valid_payload(device_type="evil_os"))

    # --- port ---

    def test_valid_port(self) -> None:
        clean = sanitize_network_config(_make_valid_payload(port=2222))
        self.assertEqual(clean["port"], 2222)

    def test_default_port_22(self) -> None:
        payload = _make_valid_payload()
        del payload["port"]
        clean = sanitize_network_config(payload)
        self.assertEqual(clean["port"], 22)

    def test_rejects_port_zero(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_network_config(_make_valid_payload(port=0))

    def test_rejects_port_too_large(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_network_config(_make_valid_payload(port=65536))

    def test_rejects_negative_port(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_network_config(_make_valid_payload(port=-1))

    def test_rejects_non_integer_port(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_network_config(_make_valid_payload(port="not-a-port"))

    # --- config_lines ---

    def test_valid_config_lines(self) -> None:
        clean = sanitize_network_config(_make_valid_payload())
        self.assertEqual(len(clean["config_lines"]), 2)

    def test_rejects_empty_config_lines(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_network_config(_make_valid_payload(config_lines=[]))

    def test_rejects_non_list_config_lines(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_network_config(_make_valid_payload(config_lines="no vlans"))

    def test_rejects_too_many_config_lines(self) -> None:
        lines = [f"interface Gi0/{i}" for i in range(101)]
        with self.assertRaises(ValueError):
            sanitize_network_config(_make_valid_payload(config_lines=lines))

    def test_rejects_oversized_config_line(self) -> None:
        long_line = "A" * 513
        with self.assertRaises(ValueError):
            sanitize_network_config(_make_valid_payload(config_lines=[long_line]))

    def test_rejects_config_line_with_null_bytes(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_network_config(_make_valid_payload(config_lines=["valid", "bad\x00line"]))

    # --- unknown keys are dropped ---

    def test_unknown_keys_dropped(self) -> None:
        payload = _make_valid_payload()
        payload["evil"] = "injected"
        clean = sanitize_network_config(payload)
        self.assertNotIn("evil", clean)


# ---------------------------------------------------------------------------
# render_plan — dry-run, no network connection
# ---------------------------------------------------------------------------


class RenderPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audit, self.audit_path = _temp_audit()
        self.clean = sanitize_network_config(_make_valid_payload())

    def tearDown(self) -> None:
        self.audit_path.unlink(missing_ok=True)

    def test_render_returns_config_plan(self) -> None:
        plan = render_plan(self.clean, audit=self.audit)
        self.assertIsInstance(plan, ConfigPlan)

    def test_render_plan_has_correct_fields(self) -> None:
        plan = render_plan(self.clean, audit=self.audit)
        self.assertEqual(plan.host, "192.168.1.1")
        self.assertEqual(plan.device_type, "cisco_ios")
        self.assertEqual(plan.port, 22)
        self.assertEqual(plan.config_lines, ["interface GigabitEthernet0/1", "shutdown"])

    def test_render_plan_produces_non_empty_plan_id(self) -> None:
        plan = render_plan(self.clean, audit=self.audit)
        self.assertEqual(len(plan.plan_id), 64)  # SHA-256 hex digest

    def test_render_plan_is_deterministic(self) -> None:
        plan1 = render_plan(self.clean, audit=self.audit)
        plan2 = render_plan(self.clean, audit=self.audit)
        self.assertEqual(plan1.plan_id, plan2.plan_id)

    def test_render_plan_logs_audit_event(self) -> None:
        plan = render_plan(self.clean, audit=self.audit)
        events = _read_audit(self.audit_path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "network.plan.rendered")
        self.assertEqual(events[0]["plan_id"], plan.plan_id)
        self.assertEqual(events[0]["host"], "192.168.1.1")

    def test_render_plan_does_not_import_netmiko(self) -> None:
        """render_plan must work even without netmiko installed."""
        import sys
        # Temporarily hide netmiko if somehow present
        netmiko_mod = sys.modules.pop("netmiko", None)
        try:
            plan = render_plan(self.clean, audit=self.audit)
            self.assertIsInstance(plan, ConfigPlan)
        finally:
            if netmiko_mod is not None:
                sys.modules["netmiko"] = netmiko_mod


# ---------------------------------------------------------------------------
# apply_config — HITL denial (no netmiko required)
# ---------------------------------------------------------------------------


class ApplyConfigDeniedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audit, self.audit_path = _temp_audit()
        self.clean = sanitize_network_config(_make_valid_payload())
        self.plan = render_plan(self.clean, audit=self.audit)

    def tearDown(self) -> None:
        self.audit_path.unlink(missing_ok=True)

    def test_apply_without_approval_raises(self) -> None:
        from freetalon.orchestrator.claws.network import apply_config

        bad_approval = Approval(plan_id="definitely-wrong-hash")
        with self.assertRaises(ApprovalError):
            apply_config(
                self.clean,
                plan=self.plan,
                approval=bad_approval,
                audit=self.audit,
            )

    def test_apply_wrong_plan_id_raises_approval_error(self) -> None:
        from freetalon.orchestrator.claws.network import apply_config

        bad_approval = Approval(plan_id="a" * 64)
        with self.assertRaises(ApprovalError):
            apply_config(
                self.clean,
                plan=self.plan,
                approval=bad_approval,
                audit=self.audit,
            )

    def test_apply_denied_writes_audit_event(self) -> None:
        from freetalon.orchestrator.claws.network import apply_config

        bad_approval = Approval(plan_id="wrong-id")
        try:
            apply_config(
                self.clean,
                plan=self.plan,
                approval=bad_approval,
                audit=self.audit,
            )
        except ApprovalError:
            pass

        events = _read_audit(self.audit_path)
        denied_events = [e for e in events if e["event"] == "network.apply.denied"]
        self.assertEqual(len(denied_events), 1)
        self.assertEqual(denied_events[0]["plan_id"], self.plan.plan_id)
        self.assertEqual(denied_events[0]["provided_approval_id"], "wrong-id")


# ---------------------------------------------------------------------------
# apply_config — gate ordering: HITL passes, deferred-dependency trips
# ---------------------------------------------------------------------------


class ApplyConfigDeferredDependencyTests(unittest.TestCase):
    """Confirm that when HITL passes but netmiko is missing, we get RuntimeError.

    This test verifies correct gate ordering:
      1. HITL check (Approval matches → passes)
      2. Netmiko availability check (not installed → raises RuntimeError)
    """

    def setUp(self) -> None:
        self.audit, self.audit_path = _temp_audit()
        self.clean = sanitize_network_config(_make_valid_payload())
        self.plan = render_plan(self.clean, audit=self.audit)

    def tearDown(self) -> None:
        self.audit_path.unlink(missing_ok=True)

    def test_valid_approval_but_netmiko_missing_raises_runtime_error(self) -> None:
        import sys
        from freetalon.orchestrator.claws.network import apply_config

        # Ensure netmiko is absent for this test
        netmiko_mod = sys.modules.pop("netmiko", None)
        sys.modules["netmiko"] = None  # type: ignore[assignment]
        try:
            valid_approval = Approval(plan_id=self.plan.plan_id)
            with self.assertRaises((RuntimeError, ImportError)):
                apply_config(
                    self.clean,
                    plan=self.plan,
                    approval=valid_approval,
                    audit=self.audit,
                )
        finally:
            if netmiko_mod is not None:
                sys.modules["netmiko"] = netmiko_mod
            else:
                sys.modules.pop("netmiko", None)

    def test_gate_order_hitl_before_dependency(self) -> None:
        """Wrong approval → ApprovalError, not RuntimeError, even without netmiko."""
        import sys
        from freetalon.orchestrator.claws.network import apply_config

        netmiko_mod = sys.modules.pop("netmiko", None)
        sys.modules["netmiko"] = None  # type: ignore[assignment]
        try:
            bad_approval = Approval(plan_id="wrong-hash")
            # Must raise ApprovalError (HITL gate) not RuntimeError (dependency)
            with self.assertRaises(ApprovalError):
                apply_config(
                    self.clean,
                    plan=self.plan,
                    approval=bad_approval,
                    audit=self.audit,
                )
        finally:
            if netmiko_mod is not None:
                sys.modules["netmiko"] = netmiko_mod
            else:
                sys.modules.pop("netmiko", None)

    def test_valid_approval_logs_approved_event_before_dependency_check(self) -> None:
        """audit network.apply.approved is written even if netmiko is missing."""
        import sys
        from freetalon.orchestrator.claws.network import apply_config

        netmiko_mod = sys.modules.pop("netmiko", None)
        sys.modules["netmiko"] = None  # type: ignore[assignment]
        try:
            valid_approval = Approval(plan_id=self.plan.plan_id)
            try:
                apply_config(
                    self.clean,
                    plan=self.plan,
                    approval=valid_approval,
                    audit=self.audit,
                )
            except (RuntimeError, ImportError):
                pass
        finally:
            if netmiko_mod is not None:
                sys.modules["netmiko"] = netmiko_mod
            else:
                sys.modules.pop("netmiko", None)

        events = _read_audit(self.audit_path)
        approved_events = [e for e in events if e["event"] == "network.apply.approved"]
        self.assertEqual(len(approved_events), 1)
        self.assertEqual(approved_events[0]["plan_id"], self.plan.plan_id)


# ---------------------------------------------------------------------------
# ConfigPlan — plan_id stability
# ---------------------------------------------------------------------------


class ConfigPlanTests(unittest.TestCase):
    def test_plan_id_changes_with_different_content(self) -> None:
        plan_a = ConfigPlan(
            host="10.0.0.1",
            device_type="cisco_ios",
            port=22,
            config_lines=["no shutdown"],
        )
        plan_b = ConfigPlan(
            host="10.0.0.2",
            device_type="cisco_ios",
            port=22,
            config_lines=["no shutdown"],
        )
        self.assertNotEqual(plan_a.plan_id, plan_b.plan_id)

    def test_plan_as_dict_contains_expected_keys(self) -> None:
        plan = ConfigPlan(
            host="10.0.0.1",
            device_type="arista_eos",
            port=443,
            config_lines=["vlan 100"],
        )
        d = plan.as_dict()
        self.assertIn("host", d)
        self.assertIn("device_type", d)
        self.assertIn("port", d)
        self.assertIn("config_lines", d)
        self.assertIn("plan_id", d)


if __name__ == "__main__":
    unittest.main()
