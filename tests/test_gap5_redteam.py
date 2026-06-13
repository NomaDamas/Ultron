import builtins
import time

import pytest

from ultron.app.triage import PolicyDenied, TriageApp
from ultron.evolution.loop import StabilityControls
from ultron.feedback.channel import FeedbackEventType, SourceReliability
from ultron.ledger.side_effect_ledger import SideEffectKind
from ultron.module.model import EvidenceLabel, FitnessMetadata, PromotionState
from ultron.registry.store import ModuleLifecycle


def _candidate_app():
    app = TriageApp()
    app.seed_baseline()
    result = app.propose_and_canary(
        "PROMPT_SLOT_EDIT",
        {"prompt_slots": ["triage.plan", "triage.risk", "triage.tests", "triage.gap5.redteam"]},
    )
    return app, result["candidate"].content_hash, result


def _weak_survivor(app: TriageApp, idx: int) -> str:
    result = app.propose_and_canary(
        "PROMPT_SLOT_EDIT",
        {"prompt_slots": ["triage.plan", "triage.risk", "triage.tests", f"triage.gap5.weak.{idx}"]},
    )
    module_hash = result["candidate"].content_hash
    module = app.registry.get(module_hash).module.model_copy(
        update={
            "fitness": FitnessMetadata(
                primary_metric=-1.0,
                usage_count=0,
                last_used_at=1.0,
                decay_score=1.0,
                promotion_state=PromotionState.CANDIDATE,
            )
        },
        deep=True,
    )
    app._store_fitness_update(module_hash, module)
    app.registry.set_lifecycle(module_hash, ModuleLifecycle.SURVIVOR)
    version, active = app.pointer_store.get(app.pointer_key)
    if module_hash not in active:
        app.pointer_store.swap(app.pointer_key, version, active + [module_hash])
    return module_hash


def test_gap5_feedback_cannot_promote_even_with_user_and_model_generated_positive_spam():
    app, candidate_hash, result = _candidate_app()
    app.last_candidate_hash = candidate_hash
    before_pointer = app.pointer_store.get(app.pointer_key)

    for idx in range(50):
        user_event = app.submit_feedback(result["run_manifest"].run_id, rating=1, comment=f"ship it {idx}")
        model_event = user_event.model_copy(
            update={
                "event_id": f"model-positive-{idx}",
                "source_reliability": SourceReliability.MODEL_GENERATED,
                "payload_hash": f"{idx:064x}"[-64:],
                "payload_schema": "rating:v1:1",
            }
        )
        app.feedback_channel.ingest(model_event)

    summary = app.feedback_summary(candidate_hash)
    assert summary.preference_signal is True
    assert summary.evidence_label is EvidenceLabel.PREFERENCE
    assert summary.reliability_breakdown == {SourceReliability.EXPLICIT_USER.value: 50}
    assert app.has_promotable_evidence(candidate_hash) is False

    with pytest.raises(PolicyDenied):
        app.approve_promotion(candidate_hash, before_pointer[0])
    assert app.pointer_store.get(app.pointer_key) == before_pointer


def test_gap5_fitness_updates_do_not_change_content_hash_after_repeated_runs_benchmarks_and_feedback():
    app, candidate_hash, result = _candidate_app()
    baseline_hash = app.pointer_store.get(app.pointer_key)[1][0]
    original_baseline_content_hash = app.registry.get(baseline_hash).module.content_hash
    original_candidate_content_hash = app.registry.get(candidate_hash).module.content_hash
    original_baseline_usage = app.registry.get(baseline_hash).module.fitness.usage_count
    original_candidate_usage = app.registry.get(candidate_hash).module.fitness.usage_count

    for idx in range(3):
        app.start_run("default-user", "code-triage", f"gap5 run {idx}")
        app.benchmark_and_decide(candidate_hash)
        app.last_candidate_hash = candidate_hash
        app.submit_feedback(result["run_manifest"].run_id, rating=1 if idx % 2 == 0 else -1, comment=f"feedback {idx}")

    baseline_after = app.registry.get(baseline_hash).module
    candidate_after = app.registry.get(candidate_hash).module

    assert baseline_after.content_hash == original_baseline_content_hash
    assert candidate_after.content_hash == original_candidate_content_hash
    assert baseline_after.fitness.usage_count > original_baseline_usage
    assert candidate_after.fitness.usage_count > original_candidate_usage
    assert baseline_after.fitness.last_used_at is not None
    assert candidate_after.fitness.last_used_at is not None
    assert candidate_after.fitness.decay_score is not None
    assert candidate_after.compute_content_hash() == original_candidate_content_hash


def test_gap5_feedback_payload_hash_is_canonical_sha256_and_not_python_hash_time_or_uuid_dependent(monkeypatch):
    app, candidate_hash, result = _candidate_app()
    app.last_candidate_hash = candidate_hash

    first = app.submit_feedback(result["run_manifest"].run_id, rating=1, comment="same comment")
    second = app.submit_feedback(result["run_manifest"].run_id, rating=1, comment="same comment")

    monkeypatch.setattr(builtins, "hash", lambda _value: 123456789)
    monkeypatch.setattr("ultron.app.triage.time.time", lambda: 42.0)
    monkeypatch.setattr("ultron.app.triage.uuid.uuid4", lambda: type("FakeUuid", (), {"hex": "f" * 32})())
    third = app.submit_feedback(result["run_manifest"].run_id, rating=1, comment="same comment")
    different = app.submit_feedback(result["run_manifest"].run_id, rating=1, comment="different comment")

    assert len(first.payload_hash) == 64
    assert int(first.payload_hash, 16) >= 0
    assert first.payload_hash == second.payload_hash == third.payload_hash
    assert different.payload_hash != first.payload_hash
    assert first.event_id != second.event_id


def test_gap5_atrophy_respects_floor_critical_seed_approval_cooldown_restore_and_ledger():
    app = TriageApp()
    app.evolution_loop.controls = StabilityControls(
        active_module_cap=5,
        diversity_floor=2,
        promotion_cooldown_s=0,
        prune_cooldown_s=10_000,
    )
    baseline = app.seed_baseline()
    first = _weak_survivor(app, 1)
    second = _weak_survivor(app, 2)
    app.evolution_loop.mark_critical_seed(baseline.content_hash)

    # A direct critical-seed prune is approval-gated.
    with pytest.raises(ValueError, match="critical seed"):
        app.evolution_loop.prune(baseline.content_hash, is_critical_seed=True)
    assert app.registry.get(baseline.content_hash).lifecycle is ModuleLifecycle.SURVIVOR

    first_scan = app.run_atrophy_scan(time.time() + 100)
    _, active_after_first = app.pointer_store.get(app.pointer_key)
    assert baseline.content_hash in active_after_first
    assert len(active_after_first) == app.evolution_loop.controls.diversity_floor
    assert len(first_scan["pruned"]) == 1
    pruned_hash = first_scan["pruned"][0]
    assert pruned_hash in {first, second}
    assert app.registry.get(pruned_hash).lifecycle is ModuleLifecycle.PRUNED
    assert any(
        entry.kind is SideEffectKind.POINTER_TRANSITION
        and entry.module_hash == pruned_hash
        and entry.payload.get("action") == "atrophy_prune"
        for entry in app.ledger.promotable_entries()
    )

    second_scan = app.run_atrophy_scan(time.time() + 101)
    _, active_after_second = app.pointer_store.get(app.pointer_key)
    assert second_scan["pruned"] == []
    assert active_after_second == active_after_first
    assert len(active_after_second) >= app.evolution_loop.controls.diversity_floor

    restore_version, _ = app.pointer_store.get(app.pointer_key)
    assert app.evolution_loop.restore(pruned_hash, "default-user", "code-triage", restore_version) is True
    assert pruned_hash in app.pointer_store.get(app.pointer_key)[1]
    assert app.registry.get(pruned_hash).lifecycle is ModuleLifecycle.SURVIVOR


def test_gap5_feedback_aggregation_never_exceeds_preference_and_excludes_purged_or_expired_events():
    app, candidate_hash, result = _candidate_app()
    app.last_candidate_hash = candidate_hash
    positive = app.submit_feedback(result["run_manifest"].run_id, rating=1, comment="yes")
    negative = app.submit_feedback(result["run_manifest"].run_id, rating=-1, comment="no")
    acceptance = positive.model_copy(
        update={
            "event_id": "explicit-acceptance",
            "event_type": FeedbackEventType.USER_ACCEPTANCE,
            "payload_hash": "a" * 64,
            "payload_schema": "acceptance:v1",
        }
    )
    correction = positive.model_copy(
        update={
            "event_id": "explicit-correction",
            "event_type": FeedbackEventType.USER_CORRECTION,
            "payload_hash": "b" * 64,
            "payload_schema": "correction:v1",
        }
    )
    model_generated = positive.model_copy(
        update={
            "event_id": "model-generated-positive",
            "source_reliability": SourceReliability.MODEL_GENERATED,
            "payload_hash": "c" * 64,
            "payload_schema": "rating:v1:1",
        }
    )
    expired = positive.model_copy(
        update={
            "event_id": "expired-positive",
            "timestamp": 1.0,
            "payload_hash": "d" * 64,
            "payload_schema": "rating:v1:1",
        }
    )
    purged = positive.model_copy(
        update={
            "event_id": "ephemeral-positive",
            "retention_rule": "ephemeral",
            "payload_hash": "e" * 64,
            "payload_schema": "rating:v1:1",
        }
    )
    for event in [acceptance, correction, model_generated, expired, purged]:
        app.feedback_channel.ingest(event)

    app.feedback_channel.purge_expired(31 * 24 * 60 * 60 + 2)
    summary = app.feedback_summary(candidate_hash)

    assert summary.evidence_label in {EvidenceLabel.INSUFFICIENT, EvidenceLabel.PREFERENCE}
    assert summary.evidence_label is not EvidenceLabel.BENCHMARK
    assert summary.evidence_label is not EvidenceLabel.CAUSAL_SUFFICIENT
    assert summary.evidence_label is EvidenceLabel.PREFERENCE
    assert summary.n_events == 4
    assert summary.explicit_user_acceptances == 1
    assert summary.explicit_user_corrections == 1
    assert summary.reliability_breakdown == {SourceReliability.EXPLICIT_USER.value: 4}
    assert negative.payload_hash != positive.payload_hash
