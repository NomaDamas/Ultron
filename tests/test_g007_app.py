from ultron.app.triage import DEFAULT_SCOPE, DEFAULT_WORKFLOW, TriageApp
from ultron.evaluation.harness import GuardrailMetrics, PairedTask
from ultron.evolution.variation import VariationPrimitive
from ultron.module.blobs import BlobKind, PromptPack
from ultron.registry.store import ModuleLifecycle


def test_full_triage_loop_seed_run_canary_promote_rollback_atrophy_restore():
    app = TriageApp()
    baseline = app.seed_baseline()
    version, active = app.pointer_store.get(app.pointer_key)
    assert version == 1
    assert active == [baseline.content_hash]

    run = app.start_run(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "Fix a flaky test")
    assert run["run_manifest"].verify(signer=app.manifest_signer)
    assert app.ledger.entries_for_run(run["run_manifest"].run_id)
    assert run["ui_spec"].components

    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "candidate-good"})
    candidate_hash = canary["candidate"].content_hash
    assert app.canary_store.read_namespace(canary["canary_id"], "memory")
    assert app.registry.get(candidate_hash).lifecycle == ModuleLifecycle.CANDIDATE
    envelope = app.build_inline_genui_envelope(run, canary)
    assert envelope.manifest_signature_ok is True
    assert envelope.candidate_hash == candidate_hash
    assert envelope.components
    assert canary["candidate"].prompt_pack_hash is not None
    prompt_blob = app.blob_store.get(BlobKind.PROMPT_PACK, canary["candidate"].prompt_pack_hash)
    assert isinstance(prompt_blob, PromptPack)
    assert prompt_blob.content_hash() == canary["candidate"].prompt_pack_hash

    decision = app.benchmark_and_decide(candidate_hash, canary_id=canary["canary_id"])
    assert decision["promotable"] is True
    assert decision["report"].promotable is True
    approved = app.approve_promotion(candidate_hash, app.current_pointer_version())
    assert approved["promoted"] is True
    promoted_version, promoted_active = app.pointer_store.get(app.pointer_key)
    assert promoted_version == 2
    assert candidate_hash in promoted_active

    bad = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "candidate-bad"})
    bad_hash = bad["candidate"].content_hash
    bad_decision = app.evaluate_and_decide(
        bad_hash,
        [PairedTask(task_id=f"bad-{i}", baseline_metric=1.0, candidate_metric=0.95) for i in range(10)],
        bad["canary_id"],
        GuardrailMetrics(),
        GuardrailMetrics(),
    )
    assert bad_decision["promotable"] is False
    rollback = app.rollback_controller.rollback(bad["canary_id"], actor="tester")
    assert rollback is not None
    app.rollback_controller.assert_no_poisoning(bad["canary_id"])
    assert bad_hash not in app.pointer_store.get(app.pointer_key)[1]

    atrophy = app.atrophy_and_restore(candidate_hash)
    assert atrophy["pruned"] is True
    assert atrophy["restored"] is True
    assert app.registry.get(candidate_hash).lifecycle == ModuleLifecycle.SURVIVOR
