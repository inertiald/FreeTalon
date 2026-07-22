"""Libvirt-based hypervisor utility for isolated KVM task domains.

ADR references
--------------
- **ADR 0000 Task 3.1** — create ``freetalon/orchestrator/claws/hypervisor.py``
  to validate resource requests, render a dry-run domain plan, and provision /
  teardown isolated libvirt domains for task execution.
- **ADR 0001 security-boundary** — raw payloads are validated and sanitized
  against an explicit whitelist *before* any libvirt-specific logic runs.
  Unknown keys are dropped, numeric ranges are bounded, and audit fields are
  redacted with :func:`~freetalon.security.redact_secret` if sensitive values
  ever appear.
- **Supply-chain policy** (``docs/approved-dependency-baseline.md``) —
  ``libvirt-python`` is not yet vendored or pinned in the approved dependency
  baseline. It is therefore imported lazily inside the execution paths only,
  while the sanitize / validate / dry-run planning logic remains fully usable
  and testable without libvirt installed.

Unlike ADR 0000 Task 1.3's Netmiko claw, this module does **not** require a
Human-In-The-Loop approval token: the repo owner explicitly scoped Task 3.1 to
bounded, validated, and fully audit-logged provisioning without an
interactive confirmation gate.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from xml.etree import ElementTree as ET

from freetalon.audit import AuditLogger
from freetalon.hardware import HostCapabilities, detect_host_capabilities
from freetalon.security import SAFE_TEXT_PATTERN, redact_secret

MAX_DOMAIN_NAME_LENGTH = 63
MAX_VCPUS = 256
MIN_MEMORY_MIB = 128
MAX_MEMORY_MIB = 1_048_576
MIN_DISK_GIB = 1
MAX_DISK_GIB = 4_096
DEFAULT_LIBVIRT_URI = "qemu:///system"
DEFAULT_LIBVIRT_NETWORK = "default"

ALLOWED_BASE_IMAGES: dict[str, str] = {
    "debian-12": "/var/lib/libvirt/images/debian-12.qcow2",
    "ubuntu-22.04": "/var/lib/libvirt/images/ubuntu-22.04.qcow2",
}

LIBVIRT_UNAVAILABLE_MESSAGE = (
    "libvirt-python is not installed. Per the supply-chain policy in "
    "docs/approved-dependency-baseline.md, libvirt-python must be vendored "
    "(a point-in-time release copy in the project's trusted dependency "
    "store, pinned with SHA256 hashes, and added to the approved baseline) "
    "before it can be used. Do not install it from PyPI at runtime."
)

_DOMAIN_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


@dataclass(slots=True, frozen=True)
class DomainPlan:
    """Immutable dry-run description of the libvirt domain to be defined."""

    name: str
    vcpus: int
    memory_mib: int
    disk_gib: int | None
    base_image: str
    base_image_path: str
    domain_xml: str
    plan_id: str = field(init=False)

    def __post_init__(self) -> None:
        content = json.dumps(
            {
                "name": self.name,
                "vcpus": self.vcpus,
                "memory_mib": self.memory_mib,
                "disk_gib": self.disk_gib,
                "base_image": self.base_image,
                "base_image_path": self.base_image_path,
                "domain_xml": self.domain_xml,
            },
            sort_keys=True,
        )
        object.__setattr__(
            self,
            "plan_id",
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of the rendered plan."""
        return {
            "name": self.name,
            "vcpus": self.vcpus,
            "memory_mib": self.memory_mib,
            "disk_gib": self.disk_gib,
            "base_image": self.base_image,
            "base_image_path": self.base_image_path,
            "domain_xml": self.domain_xml,
            "plan_id": self.plan_id,
        }


def _sanitize_domain_name(raw_name: Any) -> str:
    name = str(raw_name).strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > MAX_DOMAIN_NAME_LENGTH:
        raise ValueError(
            f"name exceeds the maximum of {MAX_DOMAIN_NAME_LENGTH} characters"
        )
    if not SAFE_TEXT_PATTERN.fullmatch(name):
        raise ValueError("name contains unsupported characters")
    if any(token in name for token in ("/", "\\", "..")):
        raise ValueError("name must not contain path separators or traversal")
    if not _DOMAIN_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "name must start with an alphanumeric character and contain only "
            "letters, digits, dots, underscores, or hyphens"
        )
    return name


def _coerce_bounded_int(
    payload: dict[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int,
    required: bool = True,
) -> int | None:
    if key not in payload or payload.get(key) is None:
        if required:
            raise ValueError(f"{key} is required")
        return None

    raw_value = payload[key]
    if isinstance(raw_value, bool):
        raise ValueError(f"{key} must be an integer")
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _resolve_base_image(raw_base_image: Any) -> tuple[str, str]:
    base_image = str(raw_base_image).strip()
    if not base_image:
        raise ValueError("base_image is required")

    if base_image in ALLOWED_BASE_IMAGES:
        return base_image, ALLOWED_BASE_IMAGES[base_image]

    for alias, path in ALLOWED_BASE_IMAGES.items():
        if base_image == path:
            return alias, path

    raise ValueError(
        "base_image must be one of the approved aliases or volume paths: "
        f"{sorted(ALLOWED_BASE_IMAGES)}"
    )


def _audit_payload_fields(payload: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key in ("name", "vcpus", "memory_mib", "disk_gib", "base_image"):
        if key in payload:
            fields[key] = payload[key]
    for key, value in payload.items():
        key_lower = str(key).lower()
        if key_lower not in fields and any(
            marker in key_lower for marker in ("secret", "token", "password", "key")
        ):
            fields[key] = redact_secret(str(value))
    return fields


def _load_libvirt() -> Any:
    try:
        return importlib.import_module("libvirt")
    except ImportError as exc:
        raise RuntimeError(LIBVIRT_UNAVAILABLE_MESSAGE) from exc


def sanitize_domain_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and sanitize a hypervisor/domain provisioning request.

    Enforces the ADR 0001 security-boundary pattern before any libvirt or host
    resource logic runs. Unknown keys are dropped by returning an explicit
    clean dict.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")

    name = _sanitize_domain_name(payload.get("name", ""))
    vcpus = _coerce_bounded_int(payload, "vcpus", minimum=1, maximum=MAX_VCPUS)
    memory_mib = _coerce_bounded_int(
        payload,
        "memory_mib",
        minimum=MIN_MEMORY_MIB,
        maximum=MAX_MEMORY_MIB,
    )
    disk_gib = _coerce_bounded_int(
        payload,
        "disk_gib",
        minimum=MIN_DISK_GIB,
        maximum=MAX_DISK_GIB,
        required=False,
    )
    base_image, base_image_path = _resolve_base_image(payload.get("base_image", ""))

    return {
        "name": name,
        "vcpus": vcpus,
        "memory_mib": memory_mib,
        "disk_gib": disk_gib,
        "base_image": base_image,
        "base_image_path": base_image_path,
    }


def validate_against_host(
    clean_request: dict[str, Any],
    *,
    host: HostCapabilities | None = None,
    detect_host_capabilities_fn: Callable[[], HostCapabilities] = detect_host_capabilities,
) -> HostCapabilities:
    """Reject impossible CPU/RAM requests before any libvirt call.

    Mirrors the ``validate_against_host`` pattern in ``freetalon.config``: the
    first violated host-capacity constraint raises ``ValueError`` immediately.
    """
    capabilities = host or detect_host_capabilities_fn()

    requested_vcpus = int(clean_request["vcpus"])
    if requested_vcpus > capabilities.cpu_count:
        raise ValueError(
            f"requested vcpus {requested_vcpus} exceeds host cpu_count "
            f"({capabilities.cpu_count})"
        )

    requested_memory = int(clean_request["memory_mib"])
    if requested_memory > capabilities.memory_mib:
        raise ValueError(
            f"requested memory_mib {requested_memory} exceeds host memory_mib "
            f"({capabilities.memory_mib})"
        )

    return capabilities


def _build_domain_xml(clean_request: dict[str, Any]) -> str:
    domain = ET.Element("domain", {"type": "kvm"})
    maximum_memory_mib = str(clean_request["memory_mib"])
    current_memory_mib = maximum_memory_mib
    domain_vcpus = str(clean_request["vcpus"])

    ET.SubElement(domain, "name").text = clean_request["name"]
    # libvirt distinguishes the domain's maximum memory from its current
    # allocation, even when both start at the same value for a fixed-size VM.
    ET.SubElement(domain, "memory", {"unit": "MiB"}).text = maximum_memory_mib
    ET.SubElement(domain, "currentMemory", {"unit": "MiB"}).text = current_memory_mib
    ET.SubElement(domain, "vcpu", {"placement": "static"}).text = domain_vcpus

    os_element = ET.SubElement(domain, "os")
    ET.SubElement(os_element, "type", {"arch": "x86_64"}).text = "hvm"

    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")

    metadata = ET.SubElement(domain, "metadata")
    ET.SubElement(metadata, "freetalon_base_image").text = clean_request["base_image"]
    ET.SubElement(metadata, "freetalon_base_image_path").text = clean_request[
        "base_image_path"
    ]
    if clean_request.get("disk_gib") is not None:
        ET.SubElement(metadata, "freetalon_requested_disk_gib").text = str(
            clean_request["disk_gib"]
        )

    devices = ET.SubElement(domain, "devices")
    disk = ET.SubElement(devices, "disk", {"type": "file", "device": "disk"})
    ET.SubElement(disk, "driver", {"name": "qemu", "type": "qcow2"})
    ET.SubElement(disk, "source", {"file": clean_request["base_image_path"]})
    ET.SubElement(disk, "target", {"dev": "vda", "bus": "virtio"})

    interface = ET.SubElement(devices, "interface", {"type": "network"})
    ET.SubElement(interface, "source", {"network": DEFAULT_LIBVIRT_NETWORK})
    ET.SubElement(interface, "model", {"type": "virtio"})

    ET.SubElement(devices, "graphics", {"type": "none"})
    ET.SubElement(devices, "console", {"type": "pty"})

    return ET.tostring(domain, encoding="unicode")


def render_domain_plan(
    clean_request: dict[str, Any],
    *,
    audit: AuditLogger,
) -> DomainPlan:
    """Render the domain XML/spec that *would* be defined, without connecting."""
    plan = DomainPlan(
        name=clean_request["name"],
        vcpus=int(clean_request["vcpus"]),
        memory_mib=int(clean_request["memory_mib"]),
        disk_gib=(
            None
            if clean_request.get("disk_gib") is None
            else int(clean_request["disk_gib"])
        ),
        base_image=clean_request["base_image"],
        base_image_path=clean_request["base_image_path"],
        domain_xml=_build_domain_xml(clean_request),
    )
    audit.log(
        "hypervisor.plan.rendered",
        name=plan.name,
        vcpus=plan.vcpus,
        memory_mib=plan.memory_mib,
        disk_gib=plan.disk_gib,
        base_image=plan.base_image,
        plan_id=plan.plan_id,
    )
    return plan


def provision_domain(
    payload: dict[str, Any],
    *,
    audit: AuditLogger,
    uri: str = DEFAULT_LIBVIRT_URI,
    host: HostCapabilities | None = None,
    detect_host_capabilities_fn: Callable[[], HostCapabilities] = detect_host_capabilities,
) -> dict[str, Any]:
    """Validate, plan, and provision a libvirt domain.

    ``libvirt-python`` is imported lazily here only. Until the dependency is
    vendored and pinned per ``docs/approved-dependency-baseline.md``, this
    function raises a clear runtime error after all pure-Python validation and
    audit logging have completed.
    """
    audit.log("hypervisor.provision.requested", uri=uri, **_audit_payload_fields(payload))

    try:
        clean_request = sanitize_domain_request(payload)
        capabilities = validate_against_host(
            clean_request,
            host=host,
            detect_host_capabilities_fn=detect_host_capabilities_fn,
        )
    except ValueError as exc:
        audit.log(
            "hypervisor.provision.rejected",
            uri=uri,
            reason=str(exc),
            **_audit_payload_fields(payload),
        )
        raise

    plan = render_domain_plan(clean_request, audit=audit)
    libvirt = _load_libvirt()
    connection = libvirt.open(uri)
    if connection is None:
        raise RuntimeError(f"failed to open libvirt connection for URI {uri!r}")

    try:
        domain = connection.defineXML(plan.domain_xml)
        if domain is None:
            raise RuntimeError("libvirt defineXML returned no domain handle")
        domain.create()
    finally:
        connection.close()

    audit.log(
        "hypervisor.provision.executed",
        name=plan.name,
        vcpus=plan.vcpus,
        memory_mib=plan.memory_mib,
        disk_gib=plan.disk_gib,
        base_image=plan.base_image,
        plan_id=plan.plan_id,
        host_cpu_count=capabilities.cpu_count,
        host_memory_mib=capabilities.memory_mib,
        uri=uri,
    )
    return {"ok": True, "name": plan.name, "plan_id": plan.plan_id, "uri": uri}


def teardown_domain(
    name: str,
    *,
    audit: AuditLogger,
    uri: str = DEFAULT_LIBVIRT_URI,
) -> dict[str, Any]:
    """Destroy and undefine a libvirt domain by name."""
    clean_name = _sanitize_domain_name(name)
    audit.log("hypervisor.teardown", name=clean_name, uri=uri)

    libvirt = _load_libvirt()
    connection = libvirt.open(uri)
    if connection is None:
        raise RuntimeError(f"failed to open libvirt connection for URI {uri!r}")

    try:
        domain = connection.lookupByName(clean_name)
        if domain.isActive():
            domain.destroy()
        domain.undefine()
    finally:
        connection.close()

    return {"ok": True, "name": clean_name, "uri": uri}
