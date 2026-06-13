"""Hermes adapter seam and deterministic CI fake."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ultron.module.model import PersistencePolicy


class LiveHermesUnavailable(RuntimeError):
    """Raised when a real pinned Hermes execution is requested in this sandbox."""


class AdapterRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    run_id: str
    session_id: str
    user_scope: str
    workflow_fingerprint: str
    active_module_set_id: str
    active_module_set_hash: str
    ordered_module_hashes: list[str]
    candidate_module_id: str | None = None
    canary_id: str | None = None
    persistence_mode: PersistencePolicy
    isolated_root: str | None = None
    resolved_prompt_order: list[str]
    resolved_tool_allowlist: list[str] = Field(description="Hermes-native tool names compiled once by TriageApp._build_adapter_request.")
    resolved_skill_refs: list[str]
    budget_policy: dict[str, Any]
    safety_policy: dict[str, Any]
    ui_spec_hash: str | None = None
    request_text: str


class AdapterRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    trajectory_id: str
    trajectory_path: str | None = None
    model_provider: str
    model_name: str
    model_snapshot: dict[str, Any]
    output: dict[str, Any]
    tool_calls: int
    measured_guardrails: dict[str, Any]
    outcome_label: str


class HermesAdapter(Protocol):
    @property
    def is_live(self) -> bool: ...

    @property
    def provider_id(self) -> str: ...

    def run(self, request: AdapterRunRequest) -> AdapterRunResult: ...


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _request_sha(request: AdapterRunRequest) -> str:
    return hashlib.sha256(_canonical_json(request.model_dump(mode="json")).encode("utf-8")).hexdigest()


class DeterministicFakeHermesAdapter:
    @property
    def is_live(self) -> bool:
        return False

    @property
    def provider_id(self) -> str:
        return "fake-deterministic"

    def run(self, request: AdapterRunRequest) -> AdapterRunResult:
        canonical_sha = _request_sha(request)
        trajectory_id = hashlib.sha256(
            f"{request.run_id}:{request.active_module_set_hash}".encode("utf-8")
        ).hexdigest()
        request_summary = (request.request_text.strip() or "triage request")[:80]
        max_tool_calls = request.budget_policy.get("max_tool_calls", 0)
        try:
            max_tool_calls_int = max(0, int(max_tool_calls))
        except (TypeError, ValueError):
            max_tool_calls_int = 0
        tool_calls = min(len(request.resolved_tool_allowlist), max_tool_calls_int)
        module_token = ",".join(hash_value[:12] for hash_value in request.ordered_module_hashes)
        output = {
            "plan": [
                f"Clarify scope for: {request_summary}",
                f"Apply focused change across modules: {module_token}",
                "Run targeted tests",
            ],
            "risk": [],
            "tests": ["pytest tests/ -q", "server create_app boot"],
            "request_sha": canonical_sha,
            "manifest_hash": request.active_module_set_hash,
            "run_id": request.run_id,
            "module_hashes": list(request.ordered_module_hashes),
            "skill_refs": list(request.resolved_skill_refs),
        }
        if request.candidate_module_id:
            output["risk"] = "stale pointer must be checked before promotion"
            output["actionable_reference"] = "src/ultron/app/triage.py::benchmark_and_decide"
            output["issue_reference"] = request_summary
        return AdapterRunResult(
            session_id=request.session_id,
            trajectory_id=trajectory_id,
            trajectory_path=f"fake://trajectory/{trajectory_id}",
            model_provider=self.provider_id,
            model_name="deterministic-ci-fake",
            model_snapshot={
                "provider": self.provider_id,
                "name": "deterministic-ci-fake",
                "request_sha": canonical_sha,
                "module_set_hash": request.active_module_set_hash,
            },
            output=output,
            tool_calls=tool_calls,
            measured_guardrails={
                "external_calls": False,
                "workspace_writes": bool(request.safety_policy.get("workspace_writes", False)),
                "unknown_tools": [],
                "max_tool_calls": max_tool_calls_int,
            },
            outcome_label="deterministic_success",
        )


@dataclass(frozen=True)
class HermesInvocationPlan:
    request_text: str
    prompt_slot_injections: dict[str, list[str]]
    hermes_tool_allowlist: list[str]
    iteration_budget: dict[str, Any]
    skill_refs: list[str]
    isolated_home_path: str | None
    isolated_workspace_path: str | None
    trajectory_tags: dict[str, str | None]


class PinnedHermesAdapter:
    @property
    def is_live(self) -> bool:
        return True

    @property
    def provider_id(self) -> str:
        return "hermes-pinned-ee1a744"

    def build_invocation_plan(self, request: AdapterRunRequest) -> HermesInvocationPlan:
        hermes_tool_allowlist = list(request.resolved_tool_allowlist)
        isolated_home_path: str | None = None
        isolated_workspace_path: str | None = None
        if request.isolated_root:
            root = PurePosixPath(request.isolated_root)
            isolated_home_path = str(root / "home")
            isolated_workspace_path = str(root / "workspace")
        return HermesInvocationPlan(
            request_text=request.request_text,
            prompt_slot_injections={
                "HERMES.md": list(request.resolved_prompt_order),
                "SOUL.md": ["preserved-core"],
                "context": [request.user_scope, request.workflow_fingerprint, request.active_module_set_hash],
                "skills": list(request.resolved_skill_refs),
            },
            hermes_tool_allowlist=hermes_tool_allowlist,
            iteration_budget={
                "max_tool_calls": request.budget_policy.get("max_tool_calls"),
                "policy": dict(request.budget_policy),
            },
            skill_refs=list(request.resolved_skill_refs),
            isolated_home_path=isolated_home_path,
            isolated_workspace_path=isolated_workspace_path,
            trajectory_tags={
                "run_id": request.run_id,
                "session_id": request.session_id,
                "active_module_set_id": request.active_module_set_id,
                "active_module_set_hash": request.active_module_set_hash,
                "candidate_module_id": request.candidate_module_id,
                "canary_id": request.canary_id,
            },
        )

    def run(self, request: AdapterRunRequest) -> AdapterRunResult:
        self.build_invocation_plan(request)
        raise LiveHermesUnavailable("Pinned Hermes execution is unavailable in this sandbox")
