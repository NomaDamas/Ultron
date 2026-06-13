"""End-to-end triage MVP wiring registry, evolution, evaluation, and UI."""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any

from ultron.composition.resolver import CompositionResolver
from ultron.hermes.adapter import AdapterRunRequest, AdapterRunResult, DeterministicFakeHermesAdapter, HermesAdapter
from ultron.hermes.tool_policy import ToolPolicyCompiler
from ultron.evaluation.harness import EvaluationHarness, EvaluationReport, FrozenVersions, GuardrailMetrics, PairedTask
from ultron.evolution.loop import EvolutionLoop, StabilityControls
from ultron.evolution.selection import SelectionOutcome, SelectionThresholds, Selector
from ultron.evolution.variation import VariationEngine, VariationPrimitive
from ultron.feedback.channel import ConsentClass, FeedbackChannel, FeedbackEvent, FeedbackEventType, SourceReliability, TimestampSource
from ultron.module.contract import load_default_contract
from ultron.hermes.module_surface_contract import ModuleSurfaceContract
from ultron.ledger.canary_store import CanaryScopedStore, RollbackController
from ultron.ledger.side_effect_ledger import LedgerEntry, SideEffectKind, SideEffectLedger
from ultron.module.model import EvidenceLabel, FitnessMetadata, HarnessModule, PersistencePolicy, PrivacyMetadata, PromotionState, TargetLens
from ultron.registry.pointer import ActivePointerStore
from ultron.registry.store import ModuleLifecycle, ModuleRegistry
from ultron.run.manifest import RunManifest
from ultron.ui.runtime import ComponentType, UiSpec, build_uispec_from_manifest


DEFAULT_SCOPE = "default-user"
DEFAULT_WORKFLOW = "code-triage"

PROMOTABLE_EVIDENCE_LABELS = {EvidenceLabel.BENCHMARK, EvidenceLabel.CAUSAL_SUFFICIENT}


class PolicyDenied(PermissionError):
    """Raised when a privileged action fails product policy without mutating state."""




class TriageApp:
    def __init__(self, adapter: HermesAdapter | None = None) -> None:
        self.ui_registry: set[ComponentType] = set(ComponentType)
        self.adapter_contract = load_default_contract()
        self.registry = ModuleRegistry()
        self.pointer_store = ActivePointerStore()
        self.resolver = CompositionResolver(self.registry, self.adapter_contract)
        self.ledger = SideEffectLedger()
        self.canary_store = CanaryScopedStore()
        self.rollback_controller = RollbackController(self.registry, self.ledger, self.canary_store, self.pointer_store)
        self.variation_engine = VariationEngine(self.registry, self.adapter_contract)
        self.thresholds = SelectionThresholds(min_paired_tasks=10, min_primary_improvement=0.10)
        self.selector = Selector(self.thresholds)
        self.evolution_loop = EvolutionLoop(
            self.registry,
            self.pointer_store,
            self.selector,
            StabilityControls(active_module_cap=2, diversity_floor=0, promotion_cooldown_s=0, prune_cooldown_s=0),
        )
        self.feedback_channel = FeedbackChannel()
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
        self.evaluated_candidates: dict[str, dict[str, Any]] = {}
        self.pending_permission_expansions: list[dict[str, Any]] = []

    @property
    def pointer_key(self) -> tuple[str, str]:
        return (DEFAULT_SCOPE, DEFAULT_WORKFLOW)

    def seed_baseline(self) -> HarnessModule:
        existing_version, existing_hashes = self.pointer_store.get(self.pointer_key)
        if existing_hashes:
            return self.registry.get(existing_hashes[0]).module
        module = HarnessModule.create(
            module_id="code_triage_v0",
            name="Code Triage Baseline",
            version=1,
            workflow_tags=[DEFAULT_WORKFLOW],
            target_lens=TargetLens.DEVELOPER,
            owner_scope=DEFAULT_SCOPE,
            surfaces=ModuleSurfaceContract(
                prompt_slots=["triage.plan", "triage.risk", "triage.tests"],
                tools=["read", "search", "pytest"],
                ui_panels=[
                    f"{ComponentType.INTAKE_PANEL.value}:0",
                    f"{ComponentType.PLAN_PANEL.value}:10",
                    f"{ComponentType.RISK_PANEL.value}:20",
                    f"{ComponentType.TEST_PANEL.value}:30",
                    f"{ComponentType.FEEDBACK_PANEL.value}:40",
                    f"{ComponentType.APPROVAL_PANEL.value}:50",
                    f"{ComponentType.ROLLBACK_PANEL.value}:60",
                ],
                safety={"workspace_writes": False, "external_calls": False},
                budget={"max_tool_calls": 8},
                persistence={"mode": PersistencePolicy.ISOLATED.value},
            ),
            prompt_pack_hash="baseline-prompt-pack-g007",
            tool_allowlist_hash="baseline-tools-g007",
            ui_panel_contract_hash="baseline-ui-panels-g007",
            safety_policy_hash="baseline-safety-g007",
            budget_policy_hash="baseline-budget-g007",
            persistence_policy=PersistencePolicy.ISOLATED,
            hermes_version_range="pinned",
            privacy=PrivacyMetadata(owner_scope=DEFAULT_SCOPE, data_classes=["operational"], consent_basis="seed"),
            fitness=FitnessMetadata(promotion_state=PromotionState.SEED, usage_count=1, primary_metric=1.0, last_used_at=time.time()),
        )
        entry = self.registry.register(module, ModuleLifecycle.SURVIVOR, "user")
        self.pointer_store.swap(self.pointer_key, existing_version, [entry.module.content_hash or ""])
        self.evolution_loop.mark_critical_seed(entry.module.content_hash or "")
        self._append_ledger("seed", "seed", entry.module.content_hash, None, SideEffectKind.POINTER_TRANSITION, {"active": [entry.module.content_hash]})
        self.frozen_versions = self.frozen_versions.model_copy(update={"baseline_module_set_hash": entry.module.content_hash or ""})
        return entry.module

    def current_uispec(self) -> UiSpec:
        self.seed_baseline()
        version, active = self.pointer_store.get(self.pointer_key)
        manifest = self.resolver.resolve(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "triage", active, {item.value for item in self.ui_registry})
        spec = build_uispec_from_manifest(manifest, self.ui_registry)
        self.last_ui_spec = spec
        return spec

    def current_pointer_version(self) -> int:
        version, _ = self.pointer_store.get(self.pointer_key)
        return version

    def start_run(self, user_scope: str, workflow_fingerprint: str, request_text: str) -> dict[str, Any]:
        self.seed_baseline()
        version, active = self.pointer_store.get((user_scope, workflow_fingerprint))
        should_bootstrap_pointer = False
        if not active and (user_scope, workflow_fingerprint) != self.pointer_key:
            _, active = self.pointer_store.get(self.pointer_key)
            should_bootstrap_pointer = True
            version = 1
        manifest = self.resolver.resolve(user_scope, workflow_fingerprint, "triage", active, {item.value for item in self.ui_registry})
        ui_spec = build_uispec_from_manifest(manifest, self.ui_registry)
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
            resolved_ui_spec_hash=ui_spec.spec_hash,
        ).sign()
        result_payload = result.model_dump(mode="json")
        for module_hash in manifest.ordered_module_hashes:
            self._append_ledger(run_manifest.run_id, manifest.manifest_hash or "", module_hash, None, SideEffectKind.ADAPTER_STATE, result_payload)
        self.last_manifest = run_manifest
        self.last_ui_spec = ui_spec
        return {"run_result": result_payload["output"], "adapter_result": result, "run_manifest": run_manifest, "ui_spec": ui_spec}

    def submit_feedback(self, run_id: str, rating: int = 1, comment: str = "") -> FeedbackEvent:
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
            consent_class=ConsentClass.OPERATIONAL,
            source_reliability=SourceReliability.EXPLICIT_USER,
            redaction_status="none",
            retention_rule="ephemeral",
            payload_hash=str(hash((rating, comment))),
            payload_schema="rating:v1",
        )
        stored = self.feedback_channel.ingest(event)
        self._append_ledger(run_id, self.last_manifest.active_module_set_hash if self.last_manifest else "feedback", None, None, SideEffectKind.FEEDBACK_EVENT, stored.model_dump(mode="json"))
        return stored

    def propose_and_canary(self, primitive: VariationPrimitive | str, change: dict[str, Any], request_text: str = "candidate triage") -> dict[str, Any]:
        self.seed_baseline()
        version, active = self.pointer_store.get(self.pointer_key)
        parent_hash = active[-1]
        proposal = self.variation_engine.propose(parent_hash, VariationPrimitive(primitive), change)
        staging_registry = copy.deepcopy(self.registry)
        staging_engine = VariationEngine(staging_registry, self.adapter_contract)
        candidate = staging_engine.apply(proposal)
        candidate_hash = candidate.content_hash or ""
        canary_id = f"canary-{candidate_hash[:12]}"
        candidate_active = list(active) + [candidate_hash]
        manifest = CompositionResolver(staging_registry, self.adapter_contract).resolve(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "triage", candidate_active, {item.value for item in self.ui_registry})
        ui_spec = build_uispec_from_manifest(manifest, self.ui_registry)
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
        )
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
        ).sign()
        result_payload = result.model_dump(mode="json")
        self._append_ledger(run_manifest.run_id, manifest.manifest_hash or "", candidate_hash, canary_id, SideEffectKind.ADAPTER_STATE, result_payload)
        self.last_candidate_hash = candidate_hash
        self.last_canary_id = canary_id
        return {"candidate": candidate, "proposal": proposal, "canary_id": canary_id, "candidate_run": result_payload["output"], "adapter_result": result, "run_manifest": run_manifest, "ui_spec": ui_spec, "mutation_diff": change}

    def evaluate_and_decide(self, candidate_hash: str, paired_tasks: list[PairedTask], canary_id: str | None = None) -> dict[str, Any]:
        canary_id = canary_id or self.last_canary_id or f"canary-{candidate_hash[:12]}"
        report = self.evaluation_harness.evaluate_paired(
            candidate_hash,
            "PROMPT_SLOT_EDIT",
            self.frozen_versions,
            paired_tasks,
            GuardrailMetrics(),
            GuardrailMetrics(),
        )
        outcome = self.selector.evaluate(candidate_hash, 1.0, 1.0 + report.mean_primary_delta, report.paired_tasks, {}, {})
        self.evaluated_candidates[candidate_hash] = {"report": report, "outcome": outcome, "canary_id": canary_id}
        if not report.promotable:
            self.evolution_loop.mark_rollback(candidate_hash)
            self.registry.set_lifecycle(candidate_hash, ModuleLifecycle.DECAYING)
        return {"report": report, "outcome": outcome, "promotable": report.promotable, "canary_id": canary_id}

    def has_promotable_evidence(self, candidate_hash: str) -> bool:
        stored = self.evaluated_candidates.get(candidate_hash)
        if stored is None:
            return False
        report = stored["report"]
        outcome = stored["outcome"]
        return bool(
            report.evidence_label in PROMOTABLE_EVIDENCE_LABELS
            and report.promotable
            and outcome.promotable
        )

    def approve_promotion(self, candidate_hash: str, expected_pointer_version: int) -> dict[str, Any]:
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
        retained = self.evolution_loop.retain(candidate_hash, outcome, DEFAULT_SCOPE, DEFAULT_WORKFLOW, expected_pointer_version)
        return {"report": report, "outcome": outcome, "promoted": retained, "canary_id": stored.get("canary_id")}

    def canary_active(self, canary_id: str) -> bool:
        return bool(canary_id and self.canary_store.read(canary_id, "adapter_state", "candidate_hash"))

    def module_is_pruned(self, module_hash: str) -> bool:
        if not module_hash:
            return False
        try:
            return self.registry.get(module_hash).lifecycle is ModuleLifecycle.PRUNED
        except KeyError:
            return False

    def record_permission_expansion_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = {"request_id": uuid.uuid4().hex, "status": "pending_human_approval", "payload": dict(payload)}
        self.pending_permission_expansions.append(request)
        return request

    def atrophy_and_restore(self, module_hash: str | None = None) -> dict[str, Any]:
        self.seed_baseline()
        version, active = self.pointer_store.get(self.pointer_key)
        target = module_hash or (active[-1] if active else None)
        if target is None:
            raise ValueError("no active module to prune")
        pruned = self.evolution_loop.prune(target, is_critical_seed=(target == active[0]), approved=True)
        restore_version, _ = self.pointer_store.get(self.pointer_key)
        restored = self.evolution_loop.restore(target, DEFAULT_SCOPE, DEFAULT_WORKFLOW, restore_version)
        return {"module_hash": target, "pruned": pruned, "restored": restored}

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
        denylist = {"stub", "fake", "fake-deterministic"}
        snapshot = result.model_snapshot
        snapshot_provider = str(snapshot.get("provider", "")).lower()
        result_provider = result.model_provider.lower()
        if snapshot_provider in denylist or result_provider in denylist:
            raise ValueError("live Hermes adapter returned denied stub/fake provider")
        snapshot_name = str(snapshot.get("name", "")).lower()
        result_name = result.model_name.lower()
        if "stub" in snapshot_name or "fake" in snapshot_name:
            raise ValueError("live Hermes adapter returned denied stub/fake snapshot name")
        if "stub" in result_name or "fake" in result_name:
            raise ValueError("live Hermes adapter returned denied stub/fake model name")
        if snapshot.get("stub") or snapshot.get("is_stub") or snapshot.get("fake"):
            raise ValueError("live Hermes adapter returned stub/fake snapshot marker")
        if result.model_provider != self.adapter.provider_id:
            raise ValueError("live Hermes adapter provider mismatch")

    def _append_ledger(self, run_id: str, module_set_hash: str, module_hash: str | None, canary_id: str | None, kind: SideEffectKind, payload: dict[str, Any]) -> None:
        self.ledger.append(LedgerEntry(run_id=run_id, module_set_hash=module_set_hash, module_hash=module_hash, canary_id=canary_id, kind=kind, payload=payload))
