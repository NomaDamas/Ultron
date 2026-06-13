"""End-to-end triage MVP wiring registry, evolution, evaluation, and UI."""

from __future__ import annotations

import copy
import os
import hashlib
import json
import time
import uuid
from typing import Any

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
from ultron.ui.runtime import ComponentType, UiSpec


DEFAULT_SCOPE = "default-user"
DEFAULT_WORKFLOW = "code-triage"

PROMOTABLE_EVIDENCE_LABELS = {EvidenceLabel.BENCHMARK, EvidenceLabel.CAUSAL_SUFFICIENT}


class PolicyDenied(PermissionError):
    """Raised when a privileged action fails product policy without mutating state."""




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
        self.evaluated_candidates: dict[str, dict[str, Any]] = {}
        self.pending_permission_expansions: list[dict[str, Any]] = []
        self.telemetry = TelemetrySink()

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
        self._append_ledger("seed", "seed", entry.module.content_hash, None, SideEffectKind.POINTER_TRANSITION, {"active": [entry.module.content_hash]})
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
        self.telemetry.increment("runs_started", event="run_started", subject=actor)
        self.last_ui_spec = ui_spec
        return {"run_result": result_payload["output"], "adapter_result": result, "run_manifest": run_manifest, "ui_spec": ui_spec}

    def submit_feedback(self, run_id: str, rating: int = 1, comment: str = "") -> FeedbackEvent:
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
        self._append_ledger(run_id, self.last_manifest.active_module_set_hash if self.last_manifest else "feedback", None, None, SideEffectKind.FEEDBACK_EVENT, stored.model_dump(mode="json"))
        if stored.candidate_id:
            self._update_fitness_for_modules([stored.candidate_id], stored.timestamp, feedback_summary=self.feedback_summary(stored.candidate_id))
        return stored

    def propose_and_canary(self, primitive: VariationPrimitive | str, change: dict[str, Any], request_text: str = "candidate triage") -> dict[str, Any]:
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
            actor=None,
        ).sign(signer=self.manifest_signer)
        result_payload = result.model_dump(mode="json")
        self._append_ledger(run_manifest.run_id, manifest.manifest_hash or "", candidate_hash, canary_id, SideEffectKind.ADAPTER_STATE, result_payload)
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

    def benchmark_and_decide(self, candidate_hash: str, fixture: BenchmarkFixture | None = None, canary_id: str | None = None, actor: str | None = None) -> dict[str, Any]:
        self.seed_baseline()
        try:
            self.registry.get(candidate_hash)
        except KeyError as exc:
            raise PolicyDenied("candidate module is not registered") from exc
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
            canary_id or self.last_canary_id,
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
                actor=actor or DEFAULT_LOCAL_PRINCIPAL.subject,
                key=self.pointer_key,
                active_module_cap=active_module_cap,
            )
            retained = True
        else:
            retained = self.evolution_loop.retain(candidate_hash, outcome, DEFAULT_SCOPE, DEFAULT_WORKFLOW, expected_pointer_version)
        if retained:
            self.telemetry.increment("promotions", event="promotion", subject=actor)
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
        )
        self.last_candidate_hash = candidate_hash
        self.last_canary_id = canary_id
        return {"candidate": candidate, "canary_id": canary_id, "registered": True, "promotable": self.has_promotable_evidence(candidate_hash)}

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
        return request

    def feedback_summary(self, candidate_hash: str) -> FeedbackSummary:
        return self.feedback_aggregator.summarize(candidate_hash)

    def run_atrophy_scan(self, now: float, actor: str | None = None) -> dict[str, Any]:
        self.seed_baseline()
        version, active = self.pointer_store.get(self.pointer_key)
        eligible = self.evolution_loop.atrophy_scan(active, now)
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
                    actor or DEFAULT_LOCAL_PRINCIPAL.subject,
                    key=self.pointer_key,
                    is_critical_seed=module_hash in self.evolution_loop._critical_seeds,
                    approved=False,
                    diversity_floor=self.evolution_loop.controls.diversity_floor,
                    current_active_hashes=current_active,
                )
                pruned.append(module_hash)
            elif self.evolution_loop.prune(module_hash):
                self._append_ledger(f"atrophy-{int(now)}", module_hash, module_hash, None, SideEffectKind.POINTER_TRANSITION, {"action": "atrophy_prune", "prior_version": current_version, "prior_hashes": current_active, "new_hashes": new_active}, actor=actor)
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
            pruned = self.evolution_loop.prune(target, is_critical_seed=(target == active[0]), approved=True)
            restore_version, _ = self.pointer_store.get(self.pointer_key)
            restored = self.evolution_loop.restore(target, DEFAULT_SCOPE, DEFAULT_WORKFLOW, restore_version)
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
        self.ledger.append(LedgerEntry(run_id=run_id, module_set_hash=module_set_hash, module_hash=module_hash, canary_id=canary_id, kind=kind, payload=payload, actor=actor))


def _deterministic_decay_score(primary_metric: float | None, feedback_summary: FeedbackSummary | None = None) -> float:
    feedback_penalty = 0.0
    if feedback_summary is not None and feedback_summary.mean_rating is not None and feedback_summary.mean_rating < 0:
        feedback_penalty = min(1.0, abs(feedback_summary.mean_rating))
    metric_penalty = 0.0
    if primary_metric is not None and primary_metric < 0:
        metric_penalty = min(1.0, abs(primary_metric))
    return max(metric_penalty, feedback_penalty)


def build_triage_app_from_env() -> TriageApp:
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
    provider = HttpModelProvider() if ui_name == "model" or synth_name == "model" else None
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
