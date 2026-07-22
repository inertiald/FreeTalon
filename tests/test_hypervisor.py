"""Tests for freetalon/orchestrator/claws/hypervisor.py.

ADR 0000 Task 3.1 — Libvirt environment management with whitelist validation,
host-capacity checks, dry-run domain XML rendering, and deferred dependency
loading for libvirt-python.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from freetalon.audit import AuditLogger
from freetalon.hardware import HostCapabilities
from freetalon.orchestrator.claws import hypervisor as hypervisor_mod
from freetalon.orchestrator.claws.hypervisor import (
    ALLOWED_BASE_IMAGES,
    DomainPlan,
    LIBVIRT_UNAVAILABLE_MESSAGE,
    provision_domain,
    render_domain_plan,
    sanitize_domain_request,
    teardown_domain,
    validate_against_host,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "worker-vm-01",
        "vcpus": 4,
        "memory_mib": 8192,
        "disk_gib": 64,
        "base_image": "debian-12",
    }
    payload.update(overrides)
    return payload


def _temp_audit() -> tuple[AuditLogger, Path]:
    tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
    tmp.close()
    path = Path(tmp.name)
    return AuditLogger(path=path), path


def _read_audit(path: Path) -> list[dict[str, object]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ---------------------------------------------------------------------------
# sanitize_domain_request — whitelist validation
# ---------------------------------------------------------------------------


class SanitizeDomainRequestTests(unittest.TestCase):
    def test_accepts_valid_request_and_drops_unknown_keys(self) -> None:
        clean = sanitize_domain_request(
            _make_valid_payload(
                base_image=ALLOWED_BASE_IMAGES["ubuntu-22.04"],
                ignored="drop-me",
            )
        )
        self.assertEqual(clean["name"], "worker-vm-01")
        self.assertEqual(clean["vcpus"], 4)
        self.assertEqual(clean["memory_mib"], 8192)
        self.assertEqual(clean["disk_gib"], 64)
        self.assertEqual(clean["base_image"], "ubuntu-22.04")
        self.assertEqual(
            clean["base_image_path"],
            ALLOWED_BASE_IMAGES["ubuntu-22.04"],
        )
        self.assertNotIn("ignored", clean)

    def test_rejects_unsafe_name(self) -> None:
        for bad_name in ("../../evil", "vm;shutdown", "bad/name", "bad name"):
            with self.subTest(bad_name=bad_name):
                with self.assertRaises(ValueError):
                    sanitize_domain_request(_make_valid_payload(name=bad_name))

    def test_rejects_vcpus_out_of_range(self) -> None:
        for value in (0, 257, "many"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    sanitize_domain_request(_make_valid_payload(vcpus=value))

    def test_rejects_memory_out_of_range(self) -> None:
        for value in (127, 1_048_577, "huge"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    sanitize_domain_request(_make_valid_payload(memory_mib=value))

    def test_rejects_disk_out_of_range(self) -> None:
        for value in (0, 4_097, "oversized"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    sanitize_domain_request(_make_valid_payload(disk_gib=value))

    def test_rejects_disallowed_base_image(self) -> None:
        for value in ("/tmp/random.qcow2", "alpine", "../../etc/passwd"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    sanitize_domain_request(_make_valid_payload(base_image=value))


# ---------------------------------------------------------------------------
# validate_against_host — capacity checks
# ---------------------------------------------------------------------------


class ValidateAgainstHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clean = sanitize_domain_request(_make_valid_payload())
        self.host = HostCapabilities(
            cpu_count=8,
            memory_mib=16_384,
            gpu_available=False,
            acceleration_libs=(),
            rdma_available=False,
            nccl_available=False,
            gpu_count=0,
        )

    def test_rejects_too_many_vcpus(self) -> None:
        with self.assertRaisesRegex(ValueError, "requested vcpus"):
            validate_against_host({**self.clean, "vcpus": 9}, host=self.host)

    def test_rejects_too_much_memory(self) -> None:
        with self.assertRaisesRegex(ValueError, "requested memory_mib"):
            validate_against_host({**self.clean, "memory_mib": 20_000}, host=self.host)

    def test_accepts_within_capacity(self) -> None:
        capabilities = validate_against_host(self.clean, host=self.host)
        self.assertEqual(capabilities, self.host)


# ---------------------------------------------------------------------------
# render_domain_plan — dry run only
# ---------------------------------------------------------------------------


class RenderDomainPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audit, self.audit_path = _temp_audit()
        self.clean = sanitize_domain_request(_make_valid_payload())

    def tearDown(self) -> None:
        self.audit_path.unlink(missing_ok=True)

    def test_render_plan_is_well_formed_without_libvirt(self) -> None:
        sys.modules.pop("libvirt", None)
        plan = render_domain_plan(self.clean, audit=self.audit)
        self.assertIsInstance(plan, DomainPlan)
        self.assertNotIn("libvirt", sys.modules)

        root = ET.fromstring(plan.domain_xml)
        self.assertEqual(root.tag, "domain")
        self.assertEqual(root.attrib["type"], "kvm")
        self.assertEqual(root.findtext("name"), "worker-vm-01")
        self.assertEqual(root.findtext("memory"), str(self.clean["memory_mib"]))
        self.assertEqual(root.findtext("vcpu"), str(self.clean["vcpus"]))
        self.assertEqual(
            root.find("./devices/disk/source").attrib["file"],
            ALLOWED_BASE_IMAGES["debian-12"],
        )

    def test_render_plan_logs_audit_event(self) -> None:
        plan = render_domain_plan(self.clean, audit=self.audit)
        events = _read_audit(self.audit_path)
        self.assertEqual(events[-1]["event"], "hypervisor.plan.rendered")
        self.assertEqual(events[-1]["plan_id"], plan.plan_id)


# ---------------------------------------------------------------------------
# provision / teardown — deferred libvirt dependency guard
# ---------------------------------------------------------------------------


class DeferredLibvirtDependencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audit, self.audit_path = _temp_audit()
        self.host = HostCapabilities(
            cpu_count=16,
            memory_mib=65_536,
            gpu_available=False,
            acceleration_libs=(),
            rdma_available=False,
            nccl_available=False,
            gpu_count=0,
        )

    def tearDown(self) -> None:
        self.audit_path.unlink(missing_ok=True)

    def test_provision_raises_clear_error_when_libvirt_unavailable(self) -> None:
        with patch.object(
            hypervisor_mod.importlib,
            "import_module",
            side_effect=ImportError("No module named 'libvirt'"),
        ):
            with self.assertRaisesRegex(RuntimeError, "approved-dependency-baseline"):
                provision_domain(
                    _make_valid_payload(),
                    audit=self.audit,
                    host=self.host,
                )

    def test_teardown_raises_clear_error_when_libvirt_unavailable(self) -> None:
        with patch.object(
            hypervisor_mod.importlib,
            "import_module",
            side_effect=ImportError("No module named 'libvirt'"),
        ):
            with self.assertRaisesRegex(RuntimeError, "approved-dependency-baseline"):
                teardown_domain("worker-vm-01", audit=self.audit)

    def test_unavailable_message_matches_constant(self) -> None:
        with patch.object(
            hypervisor_mod.importlib,
            "import_module",
            side_effect=ImportError("No module named 'libvirt'"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                provision_domain(
                    _make_valid_payload(),
                    audit=self.audit,
                    host=self.host,
                )
        self.assertEqual(str(ctx.exception), LIBVIRT_UNAVAILABLE_MESSAGE)


# ---------------------------------------------------------------------------
# audit events — plan and rejection paths
# ---------------------------------------------------------------------------


class AuditLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audit, self.audit_path = _temp_audit()
        self.small_host = HostCapabilities(
            cpu_count=2,
            memory_mib=2048,
            gpu_available=False,
            acceleration_libs=(),
            rdma_available=False,
            nccl_available=False,
            gpu_count=0,
        )

    def tearDown(self) -> None:
        self.audit_path.unlink(missing_ok=True)

    def test_rejected_provision_writes_requested_and_rejected_events(self) -> None:
        with self.assertRaises(ValueError):
            provision_domain(
                _make_valid_payload(vcpus=8, memory_mib=4096),
                audit=self.audit,
                host=self.small_host,
            )

        events = _read_audit(self.audit_path)
        event_names = [event["event"] for event in events]
        self.assertIn("hypervisor.provision.requested", event_names)
        self.assertIn("hypervisor.provision.rejected", event_names)
        self.assertNotIn("hypervisor.provision.executed", event_names)


if __name__ == "__main__":
    unittest.main()
