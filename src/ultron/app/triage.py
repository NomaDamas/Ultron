"""End-to-end triage MVP wiring registry, evolution, evaluation, and UI."""

from __future__ import annotations

import copy
from collections import Counter
import os
import hashlib
import json
import re
import time
import uuid
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from ultron.auth.principal import DEFAULT_LOCAL_PRINCIPAL, Principal
from ultron.composition.resolver import CompositionResolver
from ultron.hermes.adapter import AdapterRunRequest, AdapterRunResult, DeterministicFakeHermesAdapter, HermesAdapter, PinnedHermesAdapter
from ultron.hermes.runner import SubprocessHermesRunner
from ultron.hermes.tool_policy import ToolPolicyCompiler
from ultron.evaluation.benchmark import BenchmarkFixture, BenchmarkRunner, DEFAULT_CODE_TRIAGE_V0
from ultron.evaluation.harness import EvaluationHarness, EvaluationReport, FrozenVersions, GuardrailMetrics, PairedTask
from ultron.evolution.loop import EvolutionLoop, StabilityControls
from ultron.evolution.selection import SelectionOutcome, SelectionThresholds, Selector
from ultron.evolution.variation import VariationEngine, VariationPrimitive
from ultron.evolution.planner import PendingVariationApproval, VariationPlanConstraints, VariationPlanner
from ultron.feedback.channel import ConsentClass, FeedbackChannel, FeedbackEvent, FeedbackEventType, SourceReliability, TimestampSource
from ultron.feedback.aggregation import FeedbackAggregator, FeedbackSummary, canonical_rating_payload
from ultron.module.contract import load_default_contract
from ultron.hermes.module_surface_contract import ModuleSurfaceContract
from ultron.synthesis.module_synthesizer import DeterministicFakeModuleSynthesizer, LiveModelModuleSynthesizer, ModuleSynthesizer, SynthesisContext, SynthesisPolicyConstraints, validate_synthesized_module
from ultron.model_provider import HttpModelProvider
from ultron.ledger.canary_store import CanaryScopedStore, RollbackController
from ultron.ledger.side_effect_ledger import LedgerEntry, SideEffectKind, SideEffectLedger
from ultron.obs.telemetry import TelemetrySink
from ultron.module.blobs import BlobStore, BudgetPolicyBlob, PromptPack, SafetyPolicyBlob, ToolPolicyBlob, UiPanelContract

from ultron.module.model import EvidenceLabel, FitnessMetadata, HarnessModule, PersistencePolicy, PrivacyMetadata, PromotionState, TargetLens
from ultron.registry.pointer import ActivePointerStore
from ultron.registry.store import ModuleLifecycle, ModuleRegistry
from ultron.run.manifest import RunManifest
from ultron.persistence.db import Database
from ultron.persistence.sqlite_stores import SqliteActivePointerStore, SqliteBlobStore, SqliteEvaluatedCandidateStore, SqliteFeedbackChannel, SqliteModuleRegistry, SqliteSideEffectLedger
from ultron.run.signer import EnvKeyProvider, FixtureKeyProvider, ManifestSigner
from ultron.ui.generator import DeterministicFakeUiSpecGenerator, LiveModelUiSpecGenerator, UiGenContext, UiSpecGenerator, validate_generated_uispec
from ultron.ui.runtime import ActionType, AnimationHint, ComponentType, HarnessLifecycle, InlineGenUiEnvelope, OrbState, Region, RollbackState, RunStatus, TimelineStatus, ToolStatus, UiComponent, UiSpec


DEFAULT_SCOPE = "default-user"
DEFAULT_WORKFLOW = "code-triage"

PROMOTABLE_EVIDENCE_LABELS = {EvidenceLabel.BENCHMARK, EvidenceLabel.CAUSAL_SUFFICIENT}


class PolicyDenied(PermissionError):
    """Raised when a privileged action fails product policy without mutating state."""



class PersonalizationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    user_scope: str
    workflow_fingerprint: str
    request_class: str
    n_runs: int
    n_feedback: int
    explicit_user_corrections: int
    explicit_user_acceptances: int
    mean_rating: float | None = None
    module_usage: dict[str, int] = Field(default_factory=dict)
    dominant_evidence_labels: list[EvidenceLabel] = Field(default_factory=list)
    recent_primitive_hints: list[str] = Field(default_factory=list)
    redaction: dict[str, Any] = Field(default_factory=dict)
    summary_hash: str



class TriageApp:
    def __init__(self, adapter: HermesAdapter | None = None, ui_generator: UiSpecGenerator | None = None, module_synthesizer: ModuleSynthesizer | None = None) -> None:
        self.ui_registry: set[ComponentType] = set(ComponentType)
        self.adapter_contract = load_default_contract()
        self.blob_store = BlobStore()
        self.registry = ModuleRegistry(self.blob_store)
        self.manifest_signer: ManifestSigner | None = ManifestSigner.from_provider("fixture-dev", FixtureKeyProvider({"fixture-dev": "ultron-dev-run-manifest-key"}))

        self.pointer_store = ActivePointerStore()
        self.resolver = CompositionResolver(self.registry, self.adapter_contract)
        self.ledger = SideEffectLedger()
        self.canary_store = CanaryScopedStore()
        self.rollback_controller = RollbackController(self.registry, self.ledger, self.canary_store, self.pointer_store)
        self.variation_engine = VariationEngine(self.registry, self.adapter_contract, self.blob_store)
        self.ui_generator = ui_generator or DeterministicFakeUiSpecGenerator()
        self.module_synthesizer = module_synthesizer or DeterministicFakeModuleSynthesizer(self.blob_store, self.adapter_contract)
        self.thresholds = SelectionThresholds(min_paired_tasks=10, min_primary_improvement=0.10)
        self.selector = Selector(self.thresholds)
        self.evolution_loop = EvolutionLoop(
            self.registry,
            self.pointer_store,
            self.selector,
            StabilityControls(active_module_cap=2, diversity_floor=0, promotion_cooldown_s=0, prune_cooldown_s=0),
        )
        self.feedback_channel = FeedbackChannel()
        self.feedback_aggregator = FeedbackAggregator(self.feedback_channel)
        self.adapter = adapter or DeterministicFakeHermesAdapter()
        self.evaluation_harness = EvaluationHarness(self.selector, self.thresholds)
        self.frozen_versions = FrozenVersions(
            hermes_version="pinned-hermes-ref",
            adapter_version="ultron-adapter-mvp",
            contract_version=self.adapter_contract.hermes_commit,
            model_provider=self.adapter.provider_id,
            model_name="adapter-mediated",
            model_snapshot="adapter",
            decoding={"temperature": 0},
            ui_registry_version="g007-ui-registry",
            baseline_module_set_hash="unseeded",
        )
        self.last_manifest: RunManifest | None = None
        self.last_ui_spec: UiSpec | None = None
        self.last_candidate_hash: str | None = None
        self.last_canary_id: str | None = None
        self.run_manifests: list[RunManifest] = []
        self.evaluated_candidates: dict[str, dict[str, Any]] = {}
        self.pending_permission_expansions: list[dict[str, Any]] = []
        self.telemetry = TelemetrySink()
        self.personalization_planner = VariationPlanner()

    @property
    def pointer_key(self) -> tuple[str, str]:
        return (DEFAULT_SCOPE, DEFAULT_WORKFLOW)

    def seed_baseline(self) -> HarnessModule:
        existing_version, existing_hashes = self.pointer_store.get(self.pointer_key)
        if existing_hashes:
            return self.registry.get(existing_hashes[0]).module
        baseline_prompt_slots = {
            "triage.plan": "Read the request and produce a concise implementation plan with ordered steps and assumptions.",
            "triage.risk": "Identify safety, compatibility, and regression risks before making changes.",
            "triage.tests": "Select focused verification that covers changed behavior and important edge cases.",
        }
        baseline_tools = ["read", "search", "pytest"]
        baseline_panels = [
            f"{ComponentType.INTAKE_PANEL.value}:0",
            f"{ComponentType.PLAN_PANEL.value}:10",
            f"{ComponentType.RISK_PANEL.value}:20",
            f"{ComponentType.TEST_PANEL.value}:30",
            f"{ComponentType.FEEDBACK_PANEL.value}:40",
            f"{ComponentType.APPROVAL_PANEL.value}:50",
            f"{ComponentType.ROLLBACK_PANEL.value}:60",
        ]

        module = HarnessModule.create_with_blobs(
            self.blob_store,
            module_id="code_triage_v0",
            name="Code Triage Baseline",
            version=1,
            workflow_tags=[DEFAULT_WORKFLOW],
            target_lens=TargetLens.DEVELOPER,
            owner_scope=DEFAULT_SCOPE,
            surfaces=ModuleSurfaceContract(
                prompt_slots=list(baseline_prompt_slots),
                tools=list(baseline_tools),
                ui_panels=list(baseline_panels),
                safety={"workspace_writes": False, "external_calls": False},
                budget={"max_tool_calls": 8},
                persistence={"mode": PersistencePolicy.ISOLATED.value},
            ),
            prompt_pack=PromptPack(slots=baseline_prompt_slots, notes="Baseline code triage prompt pack."),
            tools=ToolPolicyBlob(tools=baseline_tools, rationale="Baseline read/search/test triage tools."),
            ui=UiPanelContract(panels=baseline_panels, notes="Baseline triage UI panel order."),
            safety=SafetyPolicyBlob(workspace_writes=False, external_calls=False),
            budget=BudgetPolicyBlob(max_tool_calls=8),
            persistence_policy=PersistencePolicy.ISOLATED,
            hermes_version_range="pinned",
            privacy=PrivacyMetadata(owner_scope=DEFAULT_SCOPE, data_classes=["operational"], consent_basis="seed"),
            fitness=FitnessMetadata(promotion_state=PromotionState.SEED, usage_count=1, primary_metric=1.0, last_used_at=time.time()),
        )
        entry = self.registry.register(module, ModuleLifecycle.SURVIVOR, "user")
        self.pointer_store.swap(self.pointer_key, existing_version, [entry.module.content_hash or ""])
        self.evolution_loop.mark_critical_seed(entry.module.content_hash or "")
        self._append_ledger("seed", "seed", entry.module.content_hash, None, SideEffectKind.POINTER_TRANSITION, {"active": [entry.module.content_hash], "actor": DEFAULT_LOCAL_PRINCIPAL.subject})
        self.frozen_versions = self.frozen_versions.model_copy(update={"baseline_module_set_hash": entry.module.content_hash or ""})
        return entry.module

    def current_uispec(self) -> UiSpec:
        self.seed_baseline()
        version, active = self.pointer_store.get(self.pointer_key)
        manifest = self.resolver.resolve(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "triage", active, {item.value for item in self.ui_registry})
        spec = self._generate_uispec(manifest, "triage")
        self.last_ui_spec = spec
        return spec

    def current_pointer_version(self) -> int:
        version, _ = self.pointer_store.get(self.pointer_key)
        return version

    def start_run(self, user_scope: str, workflow_fingerprint: str, request_text: str, actor: str | None = None) -> dict[str, Any]:
        self.seed_baseline()
        version, active = self.pointer_store.get((user_scope, workflow_fingerprint))
        should_bootstrap_pointer = False
        if not active and (user_scope, workflow_fingerprint) != self.pointer_key:
            _, active = self.pointer_store.get(self.pointer_key)
            should_bootstrap_pointer = True
            version = 1
        manifest = self.resolver.resolve(user_scope, workflow_fingerprint, "triage", active, {item.value for item in self.ui_registry})
        ui_spec = self._generate_uispec(manifest, "triage", {"request_text": request_text})
        run_id = uuid.uuid4().hex
        session_id = uuid.uuid4().hex
        active_module_set_id = f"{user_scope}:{workflow_fingerprint}:v{version}"
        request = self._build_adapter_request(
            manifest,
            run_id=run_id,
            session_id=session_id,
            active_module_set_id=active_module_set_id,
            candidate_module_id=None,
            canary_id=None,
            persistence_mode=PersistencePolicy.ISOLATED,
            ui_spec_hash=ui_spec.spec_hash,
            request_text=request_text,
        )
        result = self.adapter.run(request)
        self._validate_live_adapter_result(result)
        if should_bootstrap_pointer:
            self.pointer_store.swap((user_scope, workflow_fingerprint), 0, active)
        if self.manifest_signer is None:
            raise ValueError("run manifest signing requires an explicit signer")
        run_created_at = time.time()
        run_manifest = RunManifest.from_manifest_set(
            manifest,
            run_id=run_id,
            session_id=session_id,
            active_module_set_id=active_module_set_id,
            hermes_version=self.frozen_versions.hermes_version,
            adapter_version=self.frozen_versions.adapter_version,
            contract_version=self.frozen_versions.contract_version,
            model_snapshot=self._validated_model_snapshot(result),
            side_effect_ledger_id="in-memory-ledger",
            created_at=run_created_at,
            timestamp_source="server",
            persistence_mode=PersistencePolicy.ISOLATED,
            resolved_ui_spec_hash=ui_spec.spec_hash,
            actor=actor,
        ).sign(signer=self.manifest_signer)
        result_payload = result.model_dump(mode="json")
        for module_hash in manifest.ordered_module_hashes:
            self._append_ledger(run_manifest.run_id, manifest.manifest_hash or "", module_hash, None, SideEffectKind.ADAPTER_STATE, result_payload, actor=actor)
        self._update_fitness_for_modules(manifest.ordered_module_hashes, run_manifest.created_at)
        self.last_manifest = run_manifest
        self.run_manifests.append(run_manifest.model_copy(deep=True))
        self.telemetry.increment("runs_started", event="run_started", subject=actor)
        self.last_ui_spec = ui_spec
        return {"run_result": result_payload["output"], "adapter_result": result, "run_manifest": run_manifest, "ui_spec": ui_spec, "request_text": request_text}

    def build_inline_genui_envelope(self, run: dict[str, Any], canary: dict[str, Any]) -> InlineGenUiEnvelope:
        raw_manifest = run["run_manifest"]
        raw_adapter_result = run["adapter_result"]
        run_manifest = raw_manifest if hasattr(raw_manifest, "verify") else RunManifest.model_validate(raw_manifest)
        adapter_result = raw_adapter_result if hasattr(raw_adapter_result, "output") and hasattr(raw_adapter_result, "trajectory_id") else AdapterRunResult.model_validate(raw_adapter_result)
        output = adapter_result.output
        request_text = str(run.get("request_text") or "")
        ui_spec = UiSpec.model_validate(run.get("ui_spec") or self.last_ui_spec or self.current_uispec())
        candidate = canary["candidate"]
        proposal = canary.get("proposal")
        candidate_hash = candidate.content_hash or canary.get("candidate_hash") or "candidate"
        canary_id = str(canary.get("canary_id") or "") or None
        candidate_eval = self.evaluated_candidates.get(candidate_hash, {})
        report = candidate_eval.get("report")
        manifest_hash = _short_hash(run_manifest.active_module_set_hash) or "manifest"
        trajectory_id = _redacted_scalar(run_manifest.model_snapshot.get("trajectory_id") or adapter_result.trajectory_id or "trajectory", request_text, max_length=80)
        summary_lines = [_redacted_summary_line(output.get("plan"), request_text), _redacted_summary_line(output.get("risk"), request_text), _redacted_summary_line(output.get("tests"), request_text)]
        pending_permissions = len(self.pending_permission_expansions)
        rollback_state = RollbackState.READY if canary_id and self.canary_active(canary_id) else RollbackState.UNAVAILABLE
        no_poisoning_ok = _canary_no_poisoning_ok(self, canary_id)
        gated_actions = [ActionType.RUN_BENCHMARK.value, ActionType.GIVE_FEEDBACK.value]
        if self.has_promotable_evidence(candidate_hash):
            gated_actions.append(ActionType.APPROVE_PROMOTION.value)
        if rollback_state is RollbackState.READY:
            gated_actions.append(ActionType.ROLLBACK_CANARY.value)
        components = [
            UiComponent(
                type=ComponentType.ORB_STATUS,
                region=Region.MAIN,
                priority=0,
                props={"state": OrbState.IDLE, "status_text": "Run completed and inline GenUI envelope validated."},
                animation=AnimationHint(kind="pulse_glow", duration_ms=300, delay_ms=0, reduced_motion_fallback="none"),
            ),
            UiComponent(
                type=ComponentType.RUN_SUMMARY_CARD,
                region=Region.MAIN,
                priority=10,
                props={
                    "run_id": run_manifest.run_id,
                    "workflow": run_manifest.workflow_fingerprint,
                    "manifest_hash": manifest_hash,
                    "trajectory_id": trajectory_id,
                    "status": RunStatus.SUCCEEDED,
                    "summary_lines": summary_lines,
                },
                animation=AnimationHint(kind="fade_in", duration_ms=240, delay_ms=0, reduced_motion_fallback="none"),
            ),
        ]
        for offset, (tool, text) in enumerate((("plan", output.get("plan")), ("risk", output.get("risk")), ("tests", output.get("tests"))), start=1):
            components.append(
                UiComponent(
                    type=ComponentType.TOOL_RESULT_CARD,
                    region=Region.MAIN,
                    priority=10 + offset,
                    props={
                        "tool": tool,
                        "status": ToolStatus.SUCCEEDED,
                        "output_summary": [_redacted_summary_line(text, request_text)],
                        "output_redacted": True,
                        "secrets_redacted": True,
                    },
                    animation=AnimationHint(kind="slide_up", duration_ms=280, delay_ms=25 * offset, reduced_motion_fallback="fade_in"),
                )
            )
        components.extend(
            [
                UiComponent(
                    type=ComponentType.HARNESS_EVOLUTION_CARD,
                    region=Region.MAIN,
                    priority=20,
                    props={
                        "parent_hash": _redacted_scalar(_short_hash(getattr(proposal, "parent_hash", None)) or _short_hash(candidate.parent_id) or "parent", request_text, max_length=32),
                        "candidate_hash": _redacted_scalar(_short_hash(candidate_hash) or "candidate", request_text, max_length=32),
                        "primitive": _redacted_scalar(getattr(getattr(proposal, "primitive", "PROMPT_SLOT_EDIT"), "value", getattr(proposal, "primitive", "PROMPT_SLOT_EDIT")), request_text, max_length=80),
                        "lifecycle": HarnessLifecycle.CANDIDATE,
                        "rationale": _redacted_summary_line(getattr(proposal, "rationale", "Candidate harness canary created from trusted server-side run output."), request_text, max_length=240),
                        "canary_id": _redacted_scalar(canary_id, request_text, max_length=80) if canary_id else None,
                    },
                    animation=AnimationHint(kind="slide_up", duration_ms=280, delay_ms=100, reduced_motion_fallback="fade_in"),
                ),
                UiComponent(
                    type=ComponentType.EVIDENCE_STATUS_CARD,
                    region=Region.MAIN,
                    priority=30,
                    props={
                        "provenance": _redacted_summary_line(getattr(report, "provenance", None) or "pending benchmark", request_text, max_length=120),
                        "promotable": self.has_promotable_evidence(candidate_hash),
                        "evidence_label": _redacted_summary_line(getattr(getattr(report, "evidence_label", None), "value", getattr(report, "evidence_label", "pending")), request_text, max_length=80),
                        "paired_tasks": int(getattr(report, "paired_tasks", 0) or 0),
                    },
                    animation=AnimationHint(kind="fade_in", duration_ms=240, delay_ms=125, reduced_motion_fallback="none"),
                ),
                UiComponent(
                    type=ComponentType.SAFETY_STATUS_CARD,
                    region=Region.ACTIONS,
                    priority=40,
                    props={
                        "pending_permissions": pending_permissions,
                        "rollback_state": rollback_state,
                        "no_poisoning_ok": no_poisoning_ok,
                        "gated_actions": gated_actions,
                    },
                    animation=AnimationHint(kind="fade_in", duration_ms=240, delay_ms=150, reduced_motion_fallback="none"),
                ),
                UiComponent(
                    type=ComponentType.TIMELINE_STEP,
                    region=Region.MAIN,
                    priority=50,
                    props={"label": "Feedback", "status": TimelineStatus.PENDING, "detail": "Use feedback controls to tune future harness direction."},
                    animation=AnimationHint(kind="fade_in", duration_ms=240, delay_ms=175, reduced_motion_fallback="none"),
                ),
            ]
        )
        personalization_summary = self.build_personalization_summary(run_manifest.user_scope, run_manifest.workflow_fingerprint)
        components.append(
            UiComponent(
                type=ComponentType.PERSONALIZATION_SIGNAL_CARD,
                region=Region.MAIN,
                priority=45,
                props={
                    "signal_counts": {
                        "runs": personalization_summary.n_runs,
                        "feedback": personalization_summary.n_feedback,
                        "acceptances": personalization_summary.explicit_user_acceptances,
                        "corrections": personalization_summary.explicit_user_corrections,
                    },
                    "evidence_labels": [label.value for label in personalization_summary.dominant_evidence_labels],
                    "summary_hash": personalization_summary.summary_hash,
                    "rationale": f"Non-raw personalization summary {personalization_summary.summary_hash[:19]} is available for gated evolution.",
                },
                animation=AnimationHint(kind="fade_in", duration_ms=240, delay_ms=160, reduced_motion_fallback="none"),
            )
        )
        envelope = InlineGenUiEnvelope(
            envelope_id=f"inline-{run_manifest.run_id[:24]}",
            run_id=run_manifest.run_id,
            run_manifest_hash=run_manifest.active_module_set_hash,
            manifest_signature_ok=run_manifest.verify(signer=self.manifest_signer),
            active_module_set_hash=run_manifest.active_module_set_hash,
            candidate_hash=candidate_hash,
            canary_id=canary_id,
            ui_spec_hash=ui_spec.spec_hash,
            components=components,
            provenance={
                "run": run_manifest.run_id,
                "manifest": run_manifest.active_module_set_hash,
                "candidate": candidate_hash,
                "ui": ui_spec.spec_hash or "pending",
                "active_pointer_version": str(self.current_pointer_version()),
            },
            redaction={"request_text": True, "adapter_blob": True, "secrets": True, "applied": True},
            created_at=time.time(),
        )
        return envelope.finalized(self.ui_registry)

    def submit_feedback(self, run_id: str, rating: int = 1, comment: str = "", actor: str | None = None) -> FeedbackEvent:
        payload = canonical_rating_payload(rating, comment)
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        consent_class = ConsentClass.PRODUCT_IMPROVEMENT
        event = FeedbackEvent(
            event_id=uuid.uuid4().hex,
            event_type=FeedbackEventType.RATING,
            user_scope=DEFAULT_SCOPE,
            tenant_scope="local",
            session_id=self.last_manifest.session_id if self.last_manifest else "session",
            workflow_fingerprint=DEFAULT_WORKFLOW,
            active_module_set_id=self.last_manifest.active_module_set_id if self.last_manifest else "active",
            active_module_set_hash=self.last_manifest.active_module_set_hash if self.last_manifest else "feedback",
            module_id=None,
            candidate_id=self.last_candidate_hash,
            primitive_id=None,
            run_id=run_id,
            hermes_trace_id=self.last_manifest.model_snapshot.get("trajectory_id") if self.last_manifest else None,
            ui_component_id=ComponentType.FEEDBACK_PANEL.value,
            timestamp=time.time(),
            timestamp_source=TimestampSource.SERVER,
            consent_class=consent_class,
            source_reliability=SourceReliability.EXPLICIT_USER,
            redaction_status="redacted",
            retention_rule="30d",
            payload_hash=payload_hash,
            payload_schema=f"rating:v1:{payload['rating']}",
        )
        stored = self.feedback_channel.ingest(event)
        self._append_ledger(run_id, self.last_manifest.active_module_set_hash if self.last_manifest else "feedback", None, None, SideEffectKind.FEEDBACK_EVENT, stored.model_dump(mode="json"), actor=actor)
        if stored.candidate_id:
            self._update_fitness_for_modules([stored.candidate_id], stored.timestamp, feedback_summary=self.feedback_summary(stored.candidate_id))
        return stored

    def build_personalization_summary(self, user_scope: str, workflow_fingerprint: str) -> PersonalizationSummary:
        self.seed_baseline()
        scoped_runs = [
            run for run in self.run_manifests
            if run.active_module_set_id.startswith(f"{user_scope}:{workflow_fingerprint}:") or run.workflow_fingerprint == workflow_fingerprint
        ]
        module_usage: dict[str, int] = {}
        for run in scoped_runs:
            for module_hash in run.ordered_module_hashes:
                try:
                    module = self.registry.get(module_hash).module
                except KeyError:
                    continue
                module_usage[module.module_id] = min(1000, module_usage.get(module.module_id, 0) + 1)
        if not module_usage:
            _, active = self.pointer_store.get((user_scope, workflow_fingerprint))
            if not active and (user_scope, workflow_fingerprint) != self.pointer_key:
                _, active = self.pointer_store.get(self.pointer_key)
            for module_hash in active:
                try:
                    module = self.registry.get(module_hash).module
                except KeyError:
                    continue
                module_usage[module.module_id] = min(1000, module.fitness.usage_count)
        feedback_events = [
            event for event in self.feedback_channel._events
            if event.user_scope == user_scope and event.workflow_fingerprint == workflow_fingerprint
        ]
        ratings = [_rating_from_schema(event.payload_schema) for event in feedback_events]
        ratings = [rating for rating in ratings if rating is not None]
        labels: list[EvidenceLabel] = []
        primitive_counts: dict[str, int] = {}
        for entry in self._registry_entries():
            module = entry.module
            if workflow_fingerprint not in module.workflow_tags:
                continue
            for label in module.fitness.evidence_labels:
                labels.append(label)
        for run in scoped_runs[-8:]:
            if run.variation_primitive_id:
                primitive_counts[run.variation_primitive_id] = primitive_counts.get(run.variation_primitive_id, 0) + 1
        dominant = [label for label, _ in sorted(Counter(labels).items(), key=lambda item: (-item[1], item[0].value))[:4]]
        hints = [name for name, _ in sorted(primitive_counts.items(), key=lambda item: (-item[1], item[0]))[:4]]
        body = {
            "user_scope": _stable_hash(user_scope),
            "workflow_fingerprint": _stable_hash(workflow_fingerprint),
            "request_class": _stable_hash(workflow_fingerprint),
            "n_runs": min(1000, len(scoped_runs)),
            "n_feedback": min(1000, len(feedback_events)),
            "explicit_user_corrections": min(1000, sum(1 for event in feedback_events if ":-" in event.payload_schema)),
            "explicit_user_acceptances": min(1000, sum(1 for event in feedback_events if _rating_from_schema(event.payload_schema) is not None and (_rating_from_schema(event.payload_schema) or 0) > 0)),
            "mean_rating": (round(sum(ratings) / len(ratings), 6) if ratings else None),
            "module_usage": dict(sorted(module_usage.items())[:8]),
            "dominant_evidence_labels": dominant,
            "recent_primitive_hints": hints,
            "redaction": {"raw_request_text": True, "raw_feedback_comments": True, "secrets": True, "hashed_scope": True},
        }
        body["summary_hash"] = _canonical_sha256(body)
        return PersonalizationSummary.model_validate(body)

    def personalization_observability(self, user_scope: str, workflow_fingerprint: str) -> dict[str, Any]:
        summary = self.build_personalization_summary(user_scope, workflow_fingerprint)
        proposal = None
        candidate_hash = self.last_candidate_hash
        if candidate_hash:
            evaluated = self.evaluated_candidates.get(candidate_hash, {})
            report = evaluated.get("report")
            try:
                entry = self.registry.get(candidate_hash)
                lifecycle = entry.lifecycle.value.lower()
                primitive = getattr(entry.module, "variation_primitive_id", None) or (getattr(report, "primitive", None) if report is not None else None) or "unknown"
            except KeyError:
                lifecycle = "unknown"
                primitive = getattr(report, "primitive", "unknown") if report is not None else "unknown"
            proposal = {
                "primitive": str(getattr(primitive, "value", primitive)),
                "rationale": f"Last stored candidate derived from redacted summary {summary.summary_hash[:19]}.",
                "candidate_short_hash": _short_hash(candidate_hash),
                "lifecycle": lifecycle,
                "canary_id": _short_hash(self.last_canary_id) if self.last_canary_id and self.canary_active(self.last_canary_id) else None,
                "promotable": self.has_promotable_evidence(candidate_hash),
            }
        return {
            "summary": summary.model_dump(mode="json"),
            "causal_trail": {
                "aggregates": {
                    "signal_counts": {
                        "runs": summary.n_runs,
                        "feedback": summary.n_feedback,
                        "acceptances": summary.explicit_user_acceptances,
                        "corrections": summary.explicit_user_corrections,
                    },
                    "summary_hash": summary.summary_hash,
                    "evidence_labels": [label.value for label in summary.dominant_evidence_labels],
                    "module_usage": dict(summary.module_usage),
                    "recent_primitive_hints": list(summary.recent_primitive_hints),
                },
                "last_proposal": proposal,
                "approval_state": "canary" if proposal and proposal.get("canary_id") else "pending-approval" if proposal else "none",
            },
        }

    def personalize(self, user_scope: str, workflow_fingerprint: str) -> dict[str, Any] | PendingVariationApproval:
        summary = self.build_personalization_summary(user_scope, workflow_fingerprint)
        _, active = self.pointer_store.get((user_scope, workflow_fingerprint))
        if not active and (user_scope, workflow_fingerprint) != self.pointer_key:
            _, active = self.pointer_store.get(self.pointer_key)
        parent_hash = active[-1]
        parent = self.registry.get(parent_hash).module
        constraints = VariationPlanConstraints(
            max_variants=1,
            existing_variants=0,
            indicated_tools=["write"] if summary.explicit_user_corrections >= 3 and summary.mean_rating is not None and summary.mean_rating < 0 else [],
        )
        feedback_summary = FeedbackSummary(
            candidate_hash=parent_hash,
            n_events=summary.n_feedback,
            explicit_user_corrections=summary.explicit_user_corrections,
            explicit_user_acceptances=summary.explicit_user_acceptances,
            mean_rating=summary.mean_rating,
            preference_signal=summary.n_feedback > 0,
            evidence_label=EvidenceLabel.PREFERENCE if summary.n_feedback > 0 else EvidenceLabel.INSUFFICIENT,
        )
        planned = self.personalization_planner.plan(parent, feedback_summary, {"primary_metric": summary.mean_rating or 0.0}, constraints)
        if isinstance(planned, PendingVariationApproval):
            return planned
        proposal = self._summary_personalization_proposal(parent, summary, planned)
        candidate = self.variation_engine.apply(proposal)
        candidate_hash = candidate.content_hash or ""
        canary_id = f"canary-{candidate_hash[:12]}"
        self.evolution_loop.register_candidate(candidate_hash)
        self.canary_store.write(canary_id, "adapter_state", "candidate_hash", candidate_hash)
        self.rollback_controller.track_pointer_candidate(
            canary_id,
            (user_scope, workflow_fingerprint),
            self.pointer_store.get((user_scope, workflow_fingerprint))[0],
            active,
            list(active) + [candidate_hash],
            run_id="personalization-canary-pointer",
            module_set_hash=candidate_hash,
            actor=DEFAULT_LOCAL_PRINCIPAL.subject,
        )
        self.last_candidate_hash = candidate_hash
        self.last_canary_id = canary_id
        return {"summary": summary, "proposal": proposal, "candidate": candidate, "canary_id": canary_id, "promotable": self.has_promotable_evidence(candidate_hash)}

    def _summary_personalization_proposal(self, parent: HarnessModule, summary: PersonalizationSummary, planned: Any) -> Any:
        panels = list(parent.surfaces.ui_panels)
        if panels:
            index = summary.n_runs % len(panels)
            panel = panels[index]
            rationale = f"summary:{summary.summary_hash[:12]} runs={summary.n_runs} feedback={summary.n_feedback} panel_index={index}"
            return planned.model_copy(update={"primitive": VariationPrimitive.UI_PANEL_PRIORITY, "change": {"ui_panel_contract_hash": panel}, "rationale": rationale})
        max_tool_calls = int((parent.surfaces.budget or {}).get("max_tool_calls", 1))
        rationale = f"summary:{summary.summary_hash[:12]} runs={summary.n_runs} feedback={summary.n_feedback} budget_tighten"
        return planned.model_copy(update={"primitive": VariationPrimitive.BUDGET_TIGHTEN, "change": {"budget": {"max_tool_calls": max(1, max_tool_calls - 1)}}, "rationale": rationale})

    def propose_and_canary(self, primitive: VariationPrimitive | str, change: dict[str, Any], request_text: str = "candidate triage", actor: str | None = None) -> dict[str, Any]:
        audit_actor = actor or DEFAULT_LOCAL_PRINCIPAL.subject
        self.seed_baseline()
        version, active = self.pointer_store.get(self.pointer_key)
        parent_hash = active[-1]
        proposal = self.variation_engine.propose(parent_hash, VariationPrimitive(primitive), change)
        staging_registry = copy.deepcopy(self.registry)
        staging_engine = VariationEngine(staging_registry, self.adapter_contract, staging_registry.blob_store)
        candidate = staging_engine.apply(proposal)
        candidate_hash = candidate.content_hash or ""
        canary_id = f"canary-{candidate_hash[:12]}"
        candidate_active = list(active) + [candidate_hash]
        manifest = CompositionResolver(staging_registry, self.adapter_contract).resolve(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "triage", candidate_active, {item.value for item in self.ui_registry})
        ui_spec = self._generate_uispec(manifest, "triage", {"request_text": request_text, "candidate_hash": candidate_hash})
        run_id = uuid.uuid4().hex
        session_id = uuid.uuid4().hex
        active_module_set_id = f"{DEFAULT_SCOPE}:{DEFAULT_WORKFLOW}:canary"
        request = self._build_adapter_request(
            manifest,
            run_id=run_id,
            session_id=session_id,
            active_module_set_id=active_module_set_id,
            candidate_module_id=candidate_hash,
            canary_id=canary_id,
            persistence_mode=PersistencePolicy.ISOLATED,
            ui_spec_hash=ui_spec.spec_hash,
            request_text=request_text,
        )
        result = self.adapter.run(request)
        self._validate_live_adapter_result(result)
        self.registry.register(candidate, ModuleLifecycle.CANDIDATE, "canary", human_approved_additive=proposal.human_approved)
        self.evolution_loop.register_candidate(candidate_hash)
        self.canary_store.write(canary_id, "adapter_state", "candidate_hash", candidate_hash)
        self.canary_store.write(canary_id, "memory", "request", request_text)
        self.rollback_controller.track_pointer_candidate(
            canary_id,
            self.pointer_key,
            version,
            active,
            candidate_active,
            run_id="canary-pointer",
            module_set_hash=candidate_hash,
            actor=audit_actor,
        )
        if self.manifest_signer is None:
            raise ValueError("run manifest signing requires an explicit signer")
        run_manifest = RunManifest.from_manifest_set(
            manifest,
            run_id=run_id,
            session_id=session_id,
            active_module_set_id=active_module_set_id,
            hermes_version=self.frozen_versions.hermes_version,
            adapter_version=self.frozen_versions.adapter_version,
            contract_version=self.frozen_versions.contract_version,
            model_snapshot=self._validated_model_snapshot(result),
            side_effect_ledger_id="in-memory-ledger",
            created_at=time.time(),
            timestamp_source="server",
            persistence_mode=PersistencePolicy.ISOLATED,
            candidate_module_id=candidate_hash,
            variation_primitive_id=proposal.primitive.value,
            canary_id=canary_id,
            resolved_ui_spec_hash=ui_spec.spec_hash,
            actor=audit_actor,
        ).sign(signer=self.manifest_signer)
        result_payload = result.model_dump(mode="json")
        self._append_ledger(run_manifest.run_id, manifest.manifest_hash or "", candidate_hash, canary_id, SideEffectKind.ADAPTER_STATE, result_payload, actor=audit_actor)
        self.run_manifests.append(run_manifest.model_copy(deep=True))
        self.last_candidate_hash = candidate_hash
        self.last_canary_id = canary_id
        return {"candidate": candidate, "proposal": proposal, "canary_id": canary_id, "candidate_run": result_payload["output"], "adapter_result": result, "run_manifest": run_manifest, "ui_spec": ui_spec, "mutation_diff": change}

    def evaluate_and_decide(
        self,
        candidate_hash: str,
        paired_tasks: list[PairedTask],
        canary_id: str | None = None,
        guardrails_before: GuardrailMetrics | None = None,
        guardrails_after: GuardrailMetrics | None = None,
    ) -> dict[str, Any]:
        canary_id = canary_id or self.last_canary_id or f"canary-{candidate_hash[:12]}"
        before = guardrails_before or GuardrailMetrics()
        after = guardrails_after or GuardrailMetrics()
        report = self.evaluation_harness.evaluate_paired(
            candidate_hash,
            "PROMPT_SLOT_EDIT",
            self.frozen_versions,
            paired_tasks,
            before,
            after,
        )
        outcome = self.selector.evaluate(
            candidate_hash,
            1.0,
            1.0 + report.mean_primary_delta,
            report.paired_tasks,
            before.model_dump(),
            after.model_dump(),
        )
        self.evaluated_candidates[candidate_hash] = {"report": report, "outcome": outcome, "canary_id": canary_id}
        self._update_fitness_for_modules([candidate_hash], time.time(), benchmark_report=report, feedback_summary=self.feedback_summary(candidate_hash))
        if not report.promotable:
            self.evolution_loop.mark_rollback(candidate_hash)
            self.registry.set_lifecycle(candidate_hash, ModuleLifecycle.DECAYING)
        return {"report": report, "outcome": outcome, "promotable": report.promotable, "canary_id": canary_id}

    def _known_canary_for_candidate(self, candidate_hash: str, canary_id: str | None = None) -> str | None:
        if canary_id and self.canary_store.read(canary_id, "adapter_state", "candidate_hash") == candidate_hash:
            return canary_id
        evaluated = self.evaluated_candidates.get(candidate_hash, {})
        evaluated_canary = evaluated.get("canary_id")
        if isinstance(evaluated_canary, str) and self.canary_store.read(evaluated_canary, "adapter_state", "candidate_hash") == candidate_hash:
            return evaluated_canary
        if self.last_candidate_hash == candidate_hash and self.last_canary_id and self.canary_store.read(self.last_canary_id, "adapter_state", "candidate_hash") == candidate_hash:
            return self.last_canary_id
        derived = f"canary-{candidate_hash[:12]}"
        if self.canary_store.read(derived, "adapter_state", "candidate_hash") == candidate_hash:
            return derived
        return None

    def _resolve_benchmark_canary(self, candidate_hash: str, provided_canary_id: str | None = None) -> str:
        known_canary = self._known_canary_for_candidate(candidate_hash, provided_canary_id)
        if provided_canary_id:
            if known_canary != provided_canary_id:
                raise PermissionError("candidate benchmark canary is not known for candidate")
            return provided_canary_id
        if known_canary is None:
            raise PermissionError("candidate benchmark requires a known canary")
        return known_canary

    def benchmark_and_decide(self, candidate_hash: str, fixture: BenchmarkFixture | None = None, canary_id: str | None = None, actor: str | None = None) -> dict[str, Any]:
        self.seed_baseline()
        try:
            self.registry.get(candidate_hash)
        except KeyError as exc:
            raise PolicyDenied("candidate module is not registered") from exc
        resolved_canary_id = self._resolve_benchmark_canary(candidate_hash, canary_id)
        fixture = fixture or DEFAULT_CODE_TRIAGE_V0
        _, active = self.pointer_store.get(self.pointer_key)
        baseline_hashes = [hash_value for hash_value in active if hash_value != candidate_hash]
        if not baseline_hashes:
            baseline_hashes = list(active)
        candidate_hashes = list(baseline_hashes) + [candidate_hash]
        runner = BenchmarkRunner(self.adapter, self._benchmark_request)
        paired_tasks = runner.run_paired(baseline_hashes, candidate_hashes, fixture)
        for result in runner.results:
            self._validate_live_adapter_result(result)
        decision = self.evaluate_and_decide(
            candidate_hash,
            paired_tasks,
            resolved_canary_id,
            runner.guardrails_before,
            runner.guardrails_after,
        )
        trajectory_ids_by_task: dict[str, list[str]] = {task.task_id: [] for task in fixture.tasks}
        for task, baseline_result, candidate_result in zip(fixture.tasks, runner.results[0::2], runner.results[1::2], strict=True):
            trajectory_ids_by_task[task.task_id] = [baseline_result.trajectory_id, candidate_result.trajectory_id]
        report = decision["report"].model_copy(
            update={
                "provenance": "benchmark_runner",
                "benchmark_fixture_id": fixture.name,
                "benchmark_task_trajectory_ids": trajectory_ids_by_task,
            }
        )
        decision["report"] = report
        self.evaluated_candidates[candidate_hash] = {"report": report, "outcome": decision["outcome"], "canary_id": decision["canary_id"]}
        self._update_fitness_for_modules([candidate_hash], time.time(), benchmark_report=report, feedback_summary=self.feedback_summary(candidate_hash))
        self.telemetry.increment("benchmarks_run", event="benchmark_run", subject=actor)
        self._append_ledger("benchmark", candidate_hash, candidate_hash, resolved_canary_id, SideEffectKind.TELEMETRY, {"action": "RUN_BENCHMARK", "candidate_hash": candidate_hash}, actor=actor)
        return decision

    def _benchmark_request(self, module_hashes: list[str], task: Any, side: str) -> AdapterRunRequest:
        manifest = self.resolver.resolve(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "triage", module_hashes, {item.value for item in self.ui_registry})
        return self._build_adapter_request(
            manifest,
            run_id=uuid.uuid5(uuid.NAMESPACE_URL, f"benchmark:{side}:{task.task_id}:{','.join(module_hashes)}").hex,
            session_id=uuid.uuid5(uuid.NAMESPACE_URL, f"benchmark-session:{side}:{task.task_id}:{','.join(module_hashes)}").hex,
            active_module_set_id=f"{DEFAULT_SCOPE}:{DEFAULT_WORKFLOW}:benchmark:{side}",
            candidate_module_id=module_hashes[-1] if side == "candidate" and len(module_hashes) > 1 else None,
            canary_id=None,
            persistence_mode=PersistencePolicy.ISOLATED,
            ui_spec_hash=None,
            request_text=task.request_text,
        )

    def has_promotable_evidence(self, candidate_hash: str) -> bool:
        stored = self.evaluated_candidates.get(candidate_hash)
        if stored is None:
            return False
        report = stored["report"]
        outcome = stored["outcome"]
        return bool(
            report.provenance == "benchmark_runner"
            and bool(report.benchmark_fixture_id)
            and len(report.benchmark_task_trajectory_ids) == report.paired_tasks
            and all(ids and all(isinstance(item, str) and item.strip() for item in ids) for ids in report.benchmark_task_trajectory_ids.values())
            and report.evidence_label in PROMOTABLE_EVIDENCE_LABELS
            and report.promotable
            and outcome.promotable
        )

    def approve_promotion(self, candidate_hash: str, expected_pointer_version: int, actor: str | None = None) -> dict[str, Any]:
        audit_actor = actor or DEFAULT_LOCAL_PRINCIPAL.subject
        stored = self.evaluated_candidates.get(candidate_hash)
        if stored is None:
            raise PolicyDenied("candidate has no stored evaluation evidence")
        report: EvaluationReport = stored["report"]
        outcome: SelectionOutcome = stored["outcome"]
        if not self.has_promotable_evidence(candidate_hash):
            raise PolicyDenied("candidate evaluation evidence is not promotable")
        try:
            self.registry.get(candidate_hash)
        except KeyError as exc:
            raise PolicyDenied("candidate module is not registered") from exc
        if isinstance(self.pointer_store, SqliteActivePointerStore) and isinstance(self.registry, SqliteModuleRegistry) and isinstance(self.ledger, SqliteSideEffectLedger):
            from ultron.persistence.unit_of_work import PromotionUnitOfWork
            active_module_cap = self.evolution_loop.controls.active_module_cap
            _, active = self.pointer_store.get(self.pointer_key)
            PromotionUnitOfWork(self.pointer_store.db, self.registry, self.pointer_store, self.ledger).promote(
                candidate_hash,
                expected_pointer_version,
                list(active),
                evidence_id=report.frozen_versions_hash,
                actor=audit_actor,
                key=self.pointer_key,
                active_module_cap=active_module_cap,
            )
            retained = True
        else:
            retained = self.evolution_loop.retain(candidate_hash, outcome, DEFAULT_SCOPE, DEFAULT_WORKFLOW, expected_pointer_version)
        if retained:
            self.telemetry.increment("promotions", event="promotion", subject=audit_actor)
            self._append_ledger("promotion", candidate_hash, candidate_hash, stored.get("canary_id"), SideEffectKind.POINTER_TRANSITION, {"action": "APPROVE_PROMOTION", "candidate_hash": candidate_hash, "actor": audit_actor}, actor=audit_actor)
        return {"report": report, "outcome": outcome, "promoted": retained, "canary_id": stored.get("canary_id")}

    def synthesize_candidate(self, request_text: str, parent_hash: str | None = None) -> dict[str, Any]:
        self.seed_baseline()
        version, active = self.pointer_store.get(self.pointer_key)
        parent_hash = parent_hash or active[-1]
        parent = self.registry.get(parent_hash).module
        context = SynthesisContext(
            request_text=request_text,
            workflow_fingerprint=DEFAULT_WORKFLOW,
            parent_module=parent,
            feedback_summary=self.feedback_summary(parent_hash),
            eval_summary=self.evaluated_candidates.get(parent_hash),
            policy_constraints=SynthesisPolicyConstraints(allowed_surfaces=parent.surfaces, no_permission_expansion=True),
        )
        candidate = validate_synthesized_module(
            self.module_synthesizer.synthesize(context),
            self.adapter_contract,
            parent=parent,
            registry=self.registry,
        )
        candidate_hash = candidate.content_hash or ""
        canary_id = f"canary-{candidate_hash[:12]}"
        self.registry.register(candidate, ModuleLifecycle.CANDIDATE, "canary", human_approved_additive=False)
        self.evolution_loop.register_candidate(candidate_hash)
        self.canary_store.write(canary_id, "adapter_state", "candidate_hash", candidate_hash)
        self.canary_store.write(canary_id, "memory", "request", request_text)
        self.rollback_controller.track_pointer_candidate(
            canary_id,
            self.pointer_key,
            version,
            active,
            list(active) + [candidate_hash],
            run_id="synthesis-canary-pointer",
            module_set_hash=candidate_hash,
            actor=DEFAULT_LOCAL_PRINCIPAL.subject,
        )
        self.last_candidate_hash = candidate_hash
        self.last_canary_id = canary_id
        return {"candidate": candidate, "canary_id": canary_id, "registered": True, "promotable": self.has_promotable_evidence(candidate_hash)}

    def active_modules(self) -> list[dict[str, Any]]:
        _, active = self.pointer_store.get(self.pointer_key)
        modules: list[dict[str, Any]] = []
        for module_hash in active:
            entry = self.registry.get(module_hash)
            modules.append(self._toolbelt_module(entry.module))
        return modules

    def modules_by_lifecycle(self) -> dict[str, list[dict[str, Any]]]:
        lifecycle_names = [item.value.lower() for item in ModuleLifecycle]
        grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in lifecycle_names}
        for entry in self._registry_entries():
            grouped[entry.lifecycle.value.lower()].append(self._ecology_module(entry.module))
        for modules in grouped.values():
            modules.sort(key=lambda item: (item["module_id"], item["version"], item["content_hash"]))
        return grouped

    def lineage_view(self) -> list[dict[str, str | None]]:
        return [
            {"parent_id": _short_hash(entry.module.parent_id), "child_id": _short_hash(entry.module.content_hash), "module_id": entry.module.module_id}
            for entry in self._registry_entries()
            if entry.module.parent_id is not None
        ]

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        runs = self.run_manifests[-max(0, limit):]
        return [self._run_summary(run) for run in reversed(runs)]

    def recent_ledger(self, limit: int = 20) -> list[dict[str, Any]]:
        entries = self._ledger_entries()[-max(0, limit):]
        return [self._ledger_summary(entry) for entry in reversed(entries)]

    def safety_status(self) -> dict[str, Any]:
        return {
            "pending_permission_expansions": [self._safe_permission_request(request) for request in self.pending_permission_expansions],
            "last_canary_id": _short_hash(self.last_canary_id),
            "last_candidate_hash": _short_hash(self.last_candidate_hash),
            "active_pointer_version": self.current_pointer_version(),
        }

    def _registry_entries(self) -> list[Any]:
        if isinstance(self.registry, SqliteModuleRegistry):
            rows = self.registry.db.conn.execute("SELECT content_hash FROM modules ORDER BY version, content_hash").fetchall()
            return [self.registry.get(row["content_hash"]) for row in rows]
        return [entry.model_copy(deep=True) for entry in self.registry._entries.values()]

    def _ledger_entries(self) -> list[LedgerEntry]:
        if isinstance(self.ledger, SqliteSideEffectLedger):
            quarantined = self.ledger._quarantined_entry_ids()
            rows = self.ledger.db.conn.execute("SELECT * FROM ledger ORDER BY created_at, entry_id").fetchall()
            return [self.ledger._from_row(row, row["entry_id"] in quarantined).model_copy(deep=True) for row in rows]
        quarantined = self.ledger._quarantined_entry_ids()
        return [entry.model_copy(update={"quarantined": entry.entry_id in quarantined}, deep=True) for entry in self.ledger._entries if entry.kind is not SideEffectKind.QUARANTINE]

    def _toolbelt_module(self, module: HarnessModule) -> dict[str, Any]:
        return {
            "name": module.name,
            "module_id": module.module_id,
            "version": module.version,
            "target_lens": module.target_lens.value,
            "workflow_tags": list(module.workflow_tags),
            "fitness": {
                "usage_count": module.fitness.usage_count,
                "promotion_state": module.fitness.promotion_state.value,
                "primary_metric": module.fitness.primary_metric,
            },
        }

    def _ecology_module(self, module: HarnessModule) -> dict[str, Any]:
        return {
            "module_id": module.module_id,
            "version": module.version,
            "content_hash": _short_hash(module.content_hash),
            "parent_id": _short_hash(module.parent_id),
            "fitness": {
                "usage_count": module.fitness.usage_count,
                "promotion_state": module.fitness.promotion_state.value,
                "primary_metric": module.fitness.primary_metric,
                "decay_score": module.fitness.decay_score,
            },
        }

    def _run_summary(self, manifest: RunManifest) -> dict[str, Any]:
        return {
            "run_id": _short_hash(manifest.run_id),
            "workflow": manifest.workflow_fingerprint,
            "active_module_set_hash": _short_hash(manifest.active_module_set_hash),
            "model_snapshot": {
                "provider": manifest.model_snapshot.get("provider"),
                "name": manifest.model_snapshot.get("name"),
            },
            "created_at": manifest.created_at,
            "trajectory_id": manifest.model_snapshot.get("trajectory_id"),
        }

    def _ledger_summary(self, entry: LedgerEntry) -> dict[str, Any]:
        return {
            "entry_id": _short_hash(entry.entry_id),
            "kind": entry.kind.value,
            "module_hash": _short_hash(entry.module_hash),
            "canary_id": _short_hash(entry.canary_id),
            "actor": entry.actor,
            "created_at": entry.created_at,
            "quarantined": entry.quarantined,
        }

    def _safe_permission_request(self, request: dict[str, Any]) -> dict[str, Any]:
        payload = dict(request.get("payload", {}))
        return {
            "request_id": _short_hash(str(request.get("request_id") or "")),
            "status": _redacted_scalar(request.get("status"), max_length=40),
            "tool_summary": _redacted_scalar(payload.get("tool") or "not requested", max_length=80),
            "reason_summary": _redacted_summary_line(payload.get("reason") or "No reason supplied", max_length=180),
            "payload_redacted": True,
        }

    def canary_active(self, canary_id: str) -> bool:
        return bool(canary_id and self.canary_store.read(canary_id, "adapter_state", "candidate_hash"))

    def module_is_pruned(self, module_hash: str) -> bool:
        if not module_hash:
            return False
        try:
            return self.registry.get(module_hash).lifecycle is ModuleLifecycle.PRUNED
        except KeyError:
            return False

    def record_permission_expansion_request(self, payload: dict[str, Any], actor: str | None = None) -> dict[str, Any]:
        request = {"request_id": uuid.uuid4().hex, "status": "pending_human_approval", "payload": dict(payload)}
        self.pending_permission_expansions.append(request)
        self.telemetry.increment("permission_requests", event="permission_request", subject=actor)
        self._append_ledger(request["request_id"], "permission-expansion", None, None, SideEffectKind.TELEMETRY, {"action": "REQUEST_PERMISSION_EXPANSION", "request_id": request["request_id"]}, actor=actor)
        return request

    def feedback_summary(self, candidate_hash: str) -> FeedbackSummary:
        return self.feedback_aggregator.summarize(candidate_hash)

    def run_atrophy_scan(self, now: float, actor: str | None = None) -> dict[str, Any]:
        self.seed_baseline()
        version, active = self.pointer_store.get(self.pointer_key)
        eligible = self.evolution_loop.atrophy_scan(active, now)
        audit_actor = actor or DEFAULT_LOCAL_PRINCIPAL.subject
        pruned: list[str] = []
        for module_hash in eligible:
            current_version, current_active = self.pointer_store.get(self.pointer_key)
            if module_hash not in current_active:
                continue
            new_active = [hash_value for hash_value in current_active if hash_value != module_hash]
            if len(new_active) < self.evolution_loop.controls.diversity_floor:
                continue
            if isinstance(self.pointer_store, SqliteActivePointerStore) and isinstance(self.registry, SqliteModuleRegistry) and isinstance(self.ledger, SqliteSideEffectLedger):
                from ultron.persistence.unit_of_work import PromotionUnitOfWork
                PromotionUnitOfWork(self.pointer_store.db, self.registry, self.pointer_store, self.ledger).prune(
                    module_hash,
                    current_version,
                    new_active,
                    f"atrophy-{int(now)}",
                    audit_actor,
                    key=self.pointer_key,
                    is_critical_seed=module_hash in self.evolution_loop._critical_seeds,
                    approved=False,
                    diversity_floor=self.evolution_loop.controls.diversity_floor,
                    current_active_hashes=current_active,
                )
                pruned.append(module_hash)
            elif self.evolution_loop.prune(module_hash):
                self._append_ledger(f"atrophy-{int(now)}", module_hash, module_hash, None, SideEffectKind.POINTER_TRANSITION, {"action": "atrophy_prune", "prior_version": current_version, "prior_hashes": current_active, "new_hashes": new_active, "actor": audit_actor}, actor=audit_actor)
                pruned.append(module_hash)
        if pruned:
            self.telemetry.increment("prunes", amount=len(pruned), event="prune", subject=actor)
        final_version, final_active = self.pointer_store.get(self.pointer_key)
        return {"eligible": eligible, "pruned": pruned, "prior_version": version, "new_version": final_version, "active": final_active}

    def _update_fitness_for_modules(
        self,
        module_hashes: list[str],
        timestamp: float,
        *,
        benchmark_report: EvaluationReport | None = None,
        feedback_summary: FeedbackSummary | None = None,
    ) -> None:
        for module_hash in module_hashes:
            entry = self.registry.get(module_hash)
            fitness = entry.module.fitness
            labels = list(fitness.evidence_labels)
            if benchmark_report is not None and benchmark_report.evidence_label not in labels:
                labels.append(benchmark_report.evidence_label)
            if feedback_summary is not None and feedback_summary.evidence_label is EvidenceLabel.PREFERENCE and EvidenceLabel.PREFERENCE not in labels:
                labels.append(EvidenceLabel.PREFERENCE)
            primary_metric = fitness.primary_metric
            if benchmark_report is not None:
                primary_metric = benchmark_report.mean_primary_delta
            elif feedback_summary is not None and feedback_summary.mean_rating is not None:
                primary_metric = feedback_summary.mean_rating
            decay_score = _deterministic_decay_score(primary_metric, feedback_summary)
            updated_module = entry.module.model_copy(
                update={
                    "fitness": fitness.model_copy(
                        update={
                            "usage_count": fitness.usage_count + 1,
                            "last_used_at": timestamp,
                            "primary_metric": primary_metric,
                            "decay_score": decay_score,
                            "evidence_labels": labels,
                        }
                    )
                },
                deep=True,
            )
            self._store_fitness_update(module_hash, updated_module)

    def _store_fitness_update(self, module_hash: str, updated_module: HarnessModule) -> None:
        if isinstance(self.registry, SqliteModuleRegistry):
            with self.registry.db.tx() as cur:
                cur.execute("UPDATE modules SET module_json = ? WHERE content_hash = ?", (updated_module.model_dump_json(), module_hash))
                if cur.rowcount != 1:
                    raise KeyError(module_hash)
        else:
            existing = self.registry._entries[module_hash]
            updated = existing.model_copy(update={"module": updated_module}, deep=True)
            self.registry._entries[module_hash] = updated
            self.registry._registration_returns[module_hash] = updated.model_copy(deep=True)

    def atrophy_and_restore(self, module_hash: str | None = None, actor: str | None = None) -> dict[str, Any]:
        self.seed_baseline()
        version, active = self.pointer_store.get(self.pointer_key)
        target = module_hash or (active[-1] if active else None)
        if target is None:
            raise ValueError("no active module to prune")
        if isinstance(self.pointer_store, SqliteActivePointerStore) and isinstance(self.registry, SqliteModuleRegistry) and isinstance(self.ledger, SqliteSideEffectLedger):
            from ultron.persistence.unit_of_work import PromotionUnitOfWork
            uow = PromotionUnitOfWork(self.pointer_store.db, self.registry, self.pointer_store, self.ledger)
            pruned_active = [h for h in active if h != target]
            pruned = uow.prune(
                target,
                version,
                pruned_active,
                uuid.uuid4().hex,
                actor or DEFAULT_LOCAL_PRINCIPAL.subject,
                key=self.pointer_key,
                is_critical_seed=target in self.evolution_loop._critical_seeds,
                approved=True,
                diversity_floor=self.evolution_loop.controls.diversity_floor,
                current_active_hashes=active,
            ) is not None
            restore_version, restore_active = self.pointer_store.get(self.pointer_key)
            restored_active = list(restore_active)
            if target not in restored_active:
                restored_active.append(target)
            pruned_hashes: list[str] = []
            while len(restored_active) > self.evolution_loop.controls.active_module_cap:
                evict = restored_active[0] if restored_active[0] != target else restored_active[1]
                restored_active.remove(evict)
                pruned_hashes.append(evict)
            restored = uow.restore(target, restore_version, restored_active, uuid.uuid4().hex, actor or DEFAULT_LOCAL_PRINCIPAL.subject, key=self.pointer_key, pruned_hashes=pruned_hashes) is not None
        else:
            audit_actor = actor or DEFAULT_LOCAL_PRINCIPAL.subject
            pruned_prior_version, pruned_prior_active = self.pointer_store.get(self.pointer_key)
            pruned = self.evolution_loop.prune(target, is_critical_seed=(target == active[0]), approved=True)
            pruned_new_version, pruned_new_active = self.pointer_store.get(self.pointer_key)
            if pruned:
                self._append_ledger(
                    uuid.uuid4().hex,
                    target,
                    target,
                    None,
                    SideEffectKind.POINTER_TRANSITION,
                    {
                        "action": "prune",
                        "prior_version": pruned_prior_version,
                        "new_version": pruned_new_version,
                        "prior_hashes": pruned_prior_active,
                        "new_hashes": pruned_new_active,
                        "evicted_hashes": [],
                        "actor": audit_actor,
                        "scope_key": list(self.pointer_key),
                    },
                    actor=audit_actor,
                )
            restore_version, restore_prior_active = self.pointer_store.get(self.pointer_key)
            restored = self.evolution_loop.restore(target, DEFAULT_SCOPE, DEFAULT_WORKFLOW, restore_version)
            restore_new_version, restore_new_active = self.pointer_store.get(self.pointer_key)
            if restored:
                evicted_hashes = [hash_value for hash_value in restore_prior_active if hash_value not in restore_new_active and hash_value != target]
                self._append_ledger(
                    uuid.uuid4().hex,
                    target,
                    target,
                    None,
                    SideEffectKind.POINTER_TRANSITION,
                    {
                        "action": "restore",
                        "prior_version": restore_version,
                        "new_version": restore_new_version,
                        "prior_hashes": restore_prior_active,
                        "new_hashes": restore_new_active,
                        "evicted_hashes": evicted_hashes,
                        "actor": audit_actor,
                        "scope_key": list(self.pointer_key),
                    },
                    actor=audit_actor,
                )
        if pruned:
            self.telemetry.increment("prunes", event="prune", subject=actor)
        if restored:
            self.telemetry.increment("restores", event="restore", subject=actor)
        return {"module_hash": target, "pruned": pruned, "restored": restored}

    def _generate_uispec(self, manifest: Any, request_class: str, run_output_summary: dict[str, Any] | None = None) -> UiSpec:
        generated = self.ui_generator.generate(
            UiGenContext(
                module_set_manifest=manifest,
                request_class=request_class,
                run_output_summary=run_output_summary or {},
                allowed_registry=sorted(self.ui_registry, key=lambda item: item.value),
            )
        )
        return validate_generated_uispec(generated, self.ui_registry)
    def _build_adapter_request(
        self,
        manifest: Any,
        *,
        run_id: str,
        session_id: str,
        active_module_set_id: str,
        candidate_module_id: str | None,
        canary_id: str | None,
        persistence_mode: PersistencePolicy,
        ui_spec_hash: str | None,
        request_text: str,
    ) -> AdapterRunRequest:
        compiled_tools = ToolPolicyCompiler.compile(manifest.resolved_tool_allowlist)
        return AdapterRunRequest(
            run_id=run_id,
            session_id=session_id,
            user_scope=manifest.user_scope,
            workflow_fingerprint=manifest.workflow_fingerprint,
            active_module_set_id=active_module_set_id,
            active_module_set_hash=manifest.manifest_hash or manifest.compute_manifest_hash(),
            ordered_module_hashes=list(manifest.ordered_module_hashes),
            candidate_module_id=candidate_module_id,
            canary_id=canary_id,
            persistence_mode=persistence_mode,
            isolated_root=f"/tmp/ultron/{session_id}" if persistence_mode is PersistencePolicy.ISOLATED else None,
            resolved_prompt_order=list(manifest.resolved_prompt_order),
            resolved_tool_allowlist=list(compiled_tools.hermes_tools),
            resolved_skill_refs=list(manifest.resolved_skill_refs),
            budget_policy=dict(manifest.budget_policy),
            safety_policy=dict(manifest.safety_policy),
            ui_spec_hash=ui_spec_hash,
            request_text=request_text,
        )

    def _validated_model_snapshot(self, result: AdapterRunResult) -> dict[str, Any]:
        self._validate_live_adapter_result(result)
        snapshot = dict(result.model_snapshot)
        snapshot["provider"] = result.model_provider
        snapshot["name"] = result.model_name
        snapshot["trajectory_id"] = result.trajectory_id
        return snapshot

    def _validate_live_adapter_result(self, result: AdapterRunResult) -> None:
        if not self.adapter.is_live:
            return
        denylist = ("stub", "fake")
        snapshot = result.model_snapshot
        provider_fields = [
            result.model_provider,
            snapshot.get("provider", ""),
            snapshot.get("runner_provider", ""),
        ]
        for provider in provider_fields:
            if any(marker in str(provider).lower() for marker in denylist):
                raise ValueError("live Hermes adapter returned denied stub/fake provider")
        name_fields = [result.model_name, snapshot.get("name", ""), snapshot.get("runner_name", "")]
        for name in name_fields:
            if any(marker in str(name).lower() for marker in denylist):
                raise ValueError("live Hermes adapter returned denied stub/fake model name")
        if snapshot.get("stub") or snapshot.get("is_stub") or snapshot.get("fake"):
            raise ValueError("live Hermes adapter returned stub/fake snapshot marker")
        if result.model_provider != self.adapter.provider_id:
            raise ValueError("live Hermes adapter provider mismatch")

    def _append_ledger(self, run_id: str, module_set_hash: str, module_hash: str | None, canary_id: str | None, kind: SideEffectKind, payload: dict[str, Any], actor: str | None = None) -> None:
        audit_actor = actor or DEFAULT_LOCAL_PRINCIPAL.subject if kind in {SideEffectKind.POINTER_TRANSITION, SideEffectKind.CANDIDATE_LIFECYCLE, SideEffectKind.QUARANTINE} else actor
        if audit_actor and "actor" in payload and not payload.get("actor"):
            payload = {**payload, "actor": audit_actor}
        self.ledger.append(LedgerEntry(run_id=run_id, module_set_hash=module_set_hash, module_hash=module_hash, canary_id=canary_id, kind=kind, payload=payload, actor=audit_actor))


def _short_hash(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest()[:12] if value else None

def _stable_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=lambda item: item.value if hasattr(item, "value") else str(item)).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _rating_from_schema(schema: str) -> int | None:
    try:
        return int(str(schema).rsplit(":", 1)[-1])
    except ValueError:
        return None

def _deterministic_decay_score(primary_metric: float | None, feedback_summary: FeedbackSummary | None = None) -> float:
    feedback_penalty = 0.0
    if feedback_summary is not None and feedback_summary.mean_rating is not None and feedback_summary.mean_rating < 0:
        feedback_penalty = min(1.0, abs(feedback_summary.mean_rating))
    metric_penalty = 0.0
    if primary_metric is not None and primary_metric < 0:
        metric_penalty = min(1.0, abs(primary_metric))
    return max(metric_penalty, feedback_penalty)


def build_triage_app_from_env(config_service: "ConfigService | None" = None) -> TriageApp:
    # Resolve config first so .env and runtime secret-store settings are honored
    # before any env-driven wiring or provider construction.
    from ultron.config import build_config_service

    config = config_service or build_config_service()
    adapter_name = os.getenv("ULTRON_ADAPTER", "fake")
    ui_name = os.getenv("ULTRON_UI_GENERATOR", "fake")
    synth_name = os.getenv("ULTRON_MODULE_SYNTH", "fake")
    adapter: HermesAdapter
    if adapter_name == "fake":
        adapter = DeterministicFakeHermesAdapter()
    elif adapter_name == "pinned-hermes":
        adapter = PinnedHermesAdapter(SubprocessHermesRunner())
    else:
        raise ValueError(f"unknown ULTRON_ADAPTER: {adapter_name}")
    provider = None
    if ui_name == "model" or synth_name == "model":
        llm_cfg = config.provider_config("llm")
        provider = HttpModelProvider(base_url=llm_cfg.base_url, api_key=llm_cfg.api_key, model_name=llm_cfg.model_name)
    if ui_name == "fake":
        ui_generator: UiSpecGenerator = DeterministicFakeUiSpecGenerator()
    elif ui_name == "model":
        ui_generator = LiveModelUiSpecGenerator(provider)
    else:
        raise ValueError(f"unknown ULTRON_UI_GENERATOR: {ui_name}")
    app = TriageApp(adapter=adapter, ui_generator=ui_generator)
    if synth_name == "fake":
        app.module_synthesizer = DeterministicFakeModuleSynthesizer(app.blob_store, app.adapter_contract)
    elif synth_name == "model":
        app.module_synthesizer = LiveModelModuleSynthesizer(provider)
    else:
        raise ValueError(f"unknown ULTRON_MODULE_SYNTH: {synth_name}")
    return app

def build_durable_triage_app(db_path: str, *, signer: ManifestSigner | None = None, key_id: str = "prod") -> TriageApp:
    """Build a TriageApp backed by SQLite stores with explicit production signing."""
    return _build_durable_triage_app(db_path, signer=signer or ManifestSigner.from_provider(key_id, EnvKeyProvider()))


def build_durable_triage_app_for_tests(db_path: str, *, signer: ManifestSigner | None = None) -> TriageApp:
    """Build a durable app with an explicit fixture signer for tests and local fixtures only."""
    fixture_signer = signer or ManifestSigner.from_provider("fixture-dev", FixtureKeyProvider({"fixture-dev": "ultron-dev-run-manifest-key"}))
    return _build_durable_triage_app(db_path, signer=fixture_signer)


def _build_durable_triage_app(db_path: str, *, signer: ManifestSigner) -> TriageApp:
    app = TriageApp()
    db = Database(db_path)
    blob_store = SqliteBlobStore(db)
    registry = SqliteModuleRegistry(db, blob_store)
    pointer_store = SqliteActivePointerStore(db)
    ledger = SqliteSideEffectLedger(db)
    app.db = db
    app.blob_store = blob_store
    app.registry = registry
    app.pointer_store = pointer_store
    app.resolver = CompositionResolver(registry, app.adapter_contract)
    app.ledger = ledger
    app.canary_store = CanaryScopedStore()
    app.rollback_controller = RollbackController(registry, ledger, app.canary_store, pointer_store)
    app.variation_engine = VariationEngine(registry, app.adapter_contract, blob_store)
    app.evolution_loop = EvolutionLoop(
        registry,
        pointer_store,
        app.selector,
        StabilityControls(active_module_cap=2, diversity_floor=0, promotion_cooldown_s=0, prune_cooldown_s=0),
    )
    app.feedback_channel = SqliteFeedbackChannel(db)
    app.feedback_aggregator = FeedbackAggregator(app.feedback_channel)
    app.evaluated_candidates = SqliteEvaluatedCandidateStore(db)
    app.manifest_signer = signer
    return app


SECRET_PATTERNS = [
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9_]{8,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
]
HIGH_ENTROPY_PATTERNS = [
    re.compile(r"\b(?=[A-Za-z0-9._~+/=-]{20,}\b)(?=[A-Za-z0-9._~+/=-]*[A-Za-z])(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{20,}\b"),
    re.compile(r"\b[A-Za-z0-9+/=_-]{24,}\b"),
]
COMMON_REQUEST_WORDS = {
    "authentication",
    "dashboard",
    "benchmark",
    "permission",
    "personalization",
    "evaluation",
    "triage",
    "request",
    "feedback",
    "rollback",
    "restore",
}


def _request_redaction_fragments(request: str) -> list[str]:
    fragments: set[str] = {request}
    compact = request.strip()
    if len(compact) >= 80:
        fragments.add(compact[:80])
    for token in re.findall(r"[A-Za-z0-9._~+/=@:-]+", request):
        if len(token) >= 12 and (token.lower() not in COMMON_REQUEST_WORDS or _looks_secret_or_entropy(token)):
            fragments.add(token)
            fragments.update(token[:length] for length in range(12, len(token) + 1))
    return sorted(fragments, key=len, reverse=True)


def _looks_secret_or_entropy(token: str) -> bool:
    return any(pattern.search(token) for pattern in [*SECRET_PATTERNS, *HIGH_ENTROPY_PATTERNS])




def _redact(text: Any, request_text: str | None = None) -> str:
    redacted = str(text or "No summary available")
    request = str(request_text or "").strip()
    if request:
        for fragment in _request_redaction_fragments(request):
            redacted = redacted.replace(fragment, "[redacted]")
    for pattern in [*SECRET_PATTERNS, *HIGH_ENTROPY_PATTERNS]:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def _redacted_summary_line(value: Any, request_text: str | None = None, max_length: int = 180) -> str:
    return _bounded_summary_line(_redact(value, request_text), max_length=max_length)


def _redacted_scalar(value: Any, request_text: str | None = None, max_length: int = 80) -> str:
    return _bounded_summary_line(_redact(value, request_text), max_length=max_length)

def _bounded_summary_line(value: Any, max_length: int = 180) -> str:
    if isinstance(value, list):
        text = "; ".join(str(item) for item in value[:3])
    elif isinstance(value, dict):
        text = "; ".join(f"{key}: {value[key]}" for key in sorted(value)[:3])
    else:
        text = str(value or "No summary available")
    text = " ".join(text.split())
    text = text.replace("<", "‹").replace(">", "›")
    if not text:
        text = "No summary available"
    return text[: max_length - 1] + "…" if len(text) > max_length else text


def _canary_no_poisoning_ok(app: TriageApp, canary_id: str | None) -> bool:
    if not canary_id:
        return True
    return all(not app.canary_store.read_namespace(canary_id, namespace) for namespace in ("skills", "ui_cache", "pointer"))
