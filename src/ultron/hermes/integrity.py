"""Fail-closed integrity verification for the vendored Hermes reference."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ultron.hermes.pin import HERMES_PINNED_COMMIT, VENDOR_REF_PATH

MANIFEST_PATH = Path(__file__).with_name("hermes_vendor_integrity.json")


class VendorIntegrityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_commit: str = HERMES_PINNED_COMMIT
    critical_files: dict[str, str] = Field(default_factory=dict)


class VendorIntegrityStatus(BaseModel):
    status: str
    expected_commit: str
    checked_files: list[str] = Field(default_factory=list)


class VendorIntegrityError(RuntimeError):
    """Raised when a present vendored tree drifts from its integrity manifest."""


def load_integrity_manifest(path: Path = MANIFEST_PATH) -> VendorIntegrityManifest:
    return VendorIntegrityManifest.model_validate_json(path.read_text(encoding="utf-8"))


def verify_vendor_integrity(vendor_path: str | Path = VENDOR_REF_PATH) -> VendorIntegrityStatus:
    root = Path(vendor_path)
    manifest = load_integrity_manifest()
    if not root.is_dir():
        return VendorIntegrityStatus(status="vendor-absent", expected_commit=manifest.expected_commit)
    if manifest.expected_commit != HERMES_PINNED_COMMIT:
        raise VendorIntegrityError("integrity manifest commit does not match pinned Hermes commit")
    mismatches: list[str] = []
    checked: list[str] = []
    for relative, expected_hash in sorted(manifest.critical_files.items()):
        file_path = root / relative
        if not file_path.is_file():
            mismatches.append(f"missing:{relative}")
            continue
        actual_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        checked.append(relative)
        if actual_hash != expected_hash:
            mismatches.append(f"sha256:{relative}")
    if mismatches:
        raise VendorIntegrityError("vendored Hermes integrity drift: " + ", ".join(mismatches))
    return VendorIntegrityStatus(status="verified", expected_commit=manifest.expected_commit, checked_files=checked)


def write_integrity_manifest(vendor_path: str | Path, output_path: str | Path = MANIFEST_PATH) -> VendorIntegrityManifest:
    root = Path(vendor_path)
    critical_files = [
        "toolsets.py",
        "agent/conversation_loop.py",
        "agent/iteration_budget.py",
        "agent/trajectory.py",
        "agent/prompt_builder.py",
    ]
    hashes = {relative: hashlib.sha256((root / relative).read_bytes()).hexdigest() for relative in critical_files if (root / relative).is_file()}
    manifest = VendorIntegrityManifest(expected_commit=HERMES_PINNED_COMMIT, critical_files=hashes)
    Path(output_path).write_text(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
