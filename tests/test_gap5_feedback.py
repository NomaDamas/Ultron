import hashlib
import json
import time

import pytest

from ultron.app.triage import PolicyDenied, TriageApp
from ultron.evolution.loop import StabilityControls
from ultron.feedback.aggregation import canonical_rating_payload
from ultron.feedback.channel import SourceReliability, ConsentClass
from ultron.module.model import EvidenceLabel, FitnessMetadata, PromotionState
from ultron.registry.store import ModuleLifecycle


def _candidate_app():
    app = TriageApp()
    app.seed_baseline()
    result = app.propose_and_canary("PROMPT_SLOT_EDIT", {"prompt_slots": ["triage.plan", "triage.risk", "triage.tests", "triage.gap5"]})
    return app, result["candidate"].content_hash, result


def test_submit_feedback_uses_stable_canonical_sha256_payload_hash():
    app, candidate_hash, result = _candidate_app()
    app.last_candidate_hash = candidate_hash

    first = app.submit_feedback(result["run_manifest"].run_id, rating=1, comment=" useful ")
    second = app.submit_feedback(result["run_manifest"].run_id, rating=1, comment="useful")
    third = app.submit_feedback(result["run_manifest"].run_id, rating=-1, comment="useful")

    expected = hashlib.sha256(json.dumps(canonical_rating_payload(1, " useful "), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert first.payload_hash == expected
    assert second.payload_hash == expected
    assert len(first.payload_hash) == 64
    assert int(first.payload_hash, 16) >= 0
    assert third.payload_hash != first.payload_hash
    assert first.retention_rule == "30d"
    assert first.consent_class is ConsentClass.PRODUCT_IMPROVEMENT


def test_aggregation_counts_user_preference_and_excludes_model_generated_and_purged():
    app, candidate_hash, result = _candidate_app()
    app.last_candidate_hash = candidate_hash
    user_event = app.submit_feedback(result["run_manifest"].run_id, rating=1, comment="yes")
    model_event = user_event.model_copy(
        update={
            "event_id": "model-generated",
            "source_reliability": SourceReliability.MODEL_GENERATED,
            "payload_hash": "0" * 64,
            "payload_schema": "rating:v1:1",
        }
    )
    expired_event = user_event.model_copy(
        update={
            "event_id": "expired",
            "timestamp": 1.0,
            "payload_hash": "1" * 64,
            "payload_schema": "rating:v1:1",
        }
    )
    app.feedback_channel.ingest(model_event)
    app.feedback_channel.ingest(expired_event)
    app.feedback_channel.purge_expired(31 * 24 * 60 * 60 + 2)

    summary = app.feedback_summary(candidate_hash)

    assert summary.preference_signal is True
    assert summary.evidence_label is EvidenceLabel.PREFERENCE
    assert summary.n_events == 1
    assert summary.mean_rating == 1.0
    assert summary.reliability_breakdown == {SourceReliability.EXPLICIT_USER.value: 1}


def test_strong_feedback_without_benchmark_provenance_is_not_promotable():
    app, candidate_hash, result = _candidate_app()
    app.last_candidate_hash = candidate_hash
    for _ in range(3):
        app.submit_feedback(result["run_manifest"].run_id, rating=1, comment="ship it")

    before = app.pointer_store.get(app.pointer_key)
    assert app.feedback_summary(candidate_hash).evidence_label is EvidenceLabel.PREFERENCE
    assert app.has_promotable_evidence(candidate_hash) is False
    with pytest.raises(PolicyDenied):
        app.approve_promotion(candidate_hash, before[0])
    assert app.pointer_store.get(app.pointer_key) == before


def test_fitness_updates_after_start_run_and_benchmark_keep_content_identity_stable():
    app, candidate_hash, _ = _candidate_app()
    baseline_hash = app.pointer_store.get(app.pointer_key)[1][0]
    before_hash = app.registry.get(baseline_hash).module.content_hash
    before_usage = app.registry.get(baseline_hash).module.fitness.usage_count

    run = app.start_run("default-user", "code-triage", "hello")
    after_run = app.registry.get(baseline_hash).module

    assert after_run.content_hash == before_hash
    assert after_run.fitness.usage_count == before_usage + 1
    assert after_run.fitness.last_used_at == run["run_manifest"].created_at

    candidate_before_hash = app.registry.get(candidate_hash).module.content_hash
    decision = app.benchmark_and_decide(candidate_hash)
    after_benchmark = app.registry.get(candidate_hash).module

    assert after_benchmark.content_hash == candidate_before_hash
    assert after_benchmark.fitness.usage_count >= 1
    assert after_benchmark.fitness.primary_metric == decision["report"].mean_primary_delta
    assert after_benchmark.fitness.decay_score == 0.0


def test_run_atrophy_scan_prunes_reversibly_without_floor_or_critical_seed_breach():
    app = TriageApp()
    app.evolution_loop.controls = StabilityControls(active_module_cap=4, diversity_floor=1, promotion_cooldown_s=0, prune_cooldown_s=0)
    app.seed_baseline()
    candidates = []
    for idx in range(3):
        result = app.propose_and_canary("PROMPT_SLOT_EDIT", {"prompt_slots": ["triage.plan", "triage.risk", "triage.tests", f"triage.gap5.{idx}"]})
        candidate = result["candidate"].model_copy(update={"fitness": FitnessMetadata(primary_metric=-1.0, usage_count=0, last_used_at=1.0, decay_score=1.0, promotion_state=PromotionState.CANDIDATE)}, deep=True)
        app._store_fitness_update(candidate.content_hash, candidate)
        app.registry.set_lifecycle(candidate.content_hash, ModuleLifecycle.SURVIVOR)
        version, active = app.pointer_store.get(app.pointer_key)
        app.pointer_store.swap(app.pointer_key, version, active + [candidate.content_hash])
        candidates.append(candidate.content_hash)

    critical = app.pointer_store.get(app.pointer_key)[1][0]
    app.evolution_loop.mark_critical_seed(critical)
    result = app.run_atrophy_scan(time.time() + 100)
    _, active_after = app.pointer_store.get(app.pointer_key)

    assert critical in active_after
    assert len(active_after) >= app.evolution_loop.controls.diversity_floor
    assert candidates[0] in result["pruned"]
    assert app.registry.get(candidates[0]).lifecycle is ModuleLifecycle.PRUNED
    restore_version, _ = app.pointer_store.get(app.pointer_key)
    assert app.evolution_loop.restore(candidates[0], "default-user", "code-triage", restore_version) is True
    assert candidates[0] in app.pointer_store.get(app.pointer_key)[1]
