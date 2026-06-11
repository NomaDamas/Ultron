from ultron.app.triage import DEFAULT_SCOPE, DEFAULT_WORKFLOW, TriageApp
from ultron.evaluation.harness import PairedTask
from ultron.evolution.variation import VariationPrimitive
from ultron.registry.store import ModuleLifecycle


def test_full_triage_loop_seed_run_canary_promote_rollback_atrophy_restore():
    app = TriageApp()
    baseline = app.seed_baseline()
    version, active = app.pointer_store.get(app.pointer_key)
    assert version == 1
    assert active == [baseline.content_hash]

    run = app.start_run(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "Fix a flaky test")
    assert run["run_manifest"].verify()
    assert app.ledger.entries_for_run(run["run_manifest"].run_id)
    assert run["ui_spec"].components

    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "candidate-good"})
    candidate_hash = canary["candidate"].content_hash
    assert app.canary_store.read_namespace(canary["canary_id"], "memory")
    assert app.registry.get(candidate_hash).lifecycle == ModuleLifecycle.CANDIDATE

    decision = app.evaluate_and_decide(
        candidate_hash,
        [PairedTask(task_id=f"good-{i}", baseline_metric=1.0, candidate_metric=1.2) for i in range(10)],
        canary["canary_id"],
    )
    assert decision["promoted"] is True
    promoted_version, promoted_active = app.pointer_store.get(app.pointer_key)
    assert promoted_version == 2
    assert candidate_hash in promoted_active

    bad = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "candidate-bad"})
    bad_hash = bad["candidate"].content_hash
    bad_decision = app.evaluate_and_decide(
        bad_hash,
        [PairedTask(task_id=f"bad-{i}", baseline_metric=1.0, candidate_metric=0.95) for i in range(10)],
        bad["canary_id"],
    )
    assert bad_decision["promoted"] is False
    assert bad_decision["rollback"] is not None
    app.rollback_controller.assert_no_poisoning(bad["canary_id"])
    assert bad_hash not in app.pointer_store.get(app.pointer_key)[1]

    atrophy = app.atrophy_and_restore(candidate_hash)
    assert atrophy["pruned"] is True
    assert atrophy["restored"] is True
    assert app.registry.get(candidate_hash).lifecycle == ModuleLifecycle.SURVIVOR
