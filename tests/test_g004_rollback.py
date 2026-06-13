import pytest
from ultron.ledger.canary_store import CanaryScopedStore, RollbackController
from ultron.ledger.side_effect_ledger import LedgerEntry, SideEffectKind, SideEffectLedger
from ultron.registry.pointer import ActivePointerStore


def _entry(canary_id="canary-1", run_id="run-1", kind=SideEffectKind.HERMES_MEMORY):
    return LedgerEntry(
        run_id=run_id,
        module_set_hash="set-hash",
        module_hash="module-hash",
        canary_id=canary_id,
        kind=kind,
        payload={"key": "value"},
        created_at=1.0,
    )


def test_side_effect_ledger_is_append_only_and_quarantine_preserves_audit():
    ledger = SideEffectLedger()
    first_id = ledger.append(_entry(kind=SideEffectKind.HERMES_MEMORY))
    second_id = ledger.append(_entry(kind=SideEffectKind.TELEMETRY))
    baseline_id = ledger.append(_entry(canary_id=None, kind=SideEffectKind.TELEMETRY))

    assert [entry.entry_id for entry in ledger.entries_for_canary("canary-1")] == [first_id, second_id]

    quarantined = ledger.mark_quarantined("canary-1", actor="tester")

    assert quarantined == [first_id, second_id]
    assert [entry.entry_id for entry in ledger.entries_for_canary("canary-1")] == [first_id, second_id]
    assert all(entry.quarantined for entry in ledger.entries_for_canary("canary-1"))
    assert [entry.entry_id for entry in ledger.promotable_entries()] == [baseline_id]


def test_rollback_drops_isolated_canary_state_and_prevents_later_baseline_poisoning():
    ledger = SideEffectLedger()
    store = CanaryScopedStore()
    pointer = ActivePointerStore()
    controller = RollbackController(ledger=ledger, canary_store=store, pointer_store=pointer)
    canary_id = "canary-clean"

    for namespace, kind in [
        ("memory", SideEffectKind.HERMES_MEMORY),
        ("skills", SideEffectKind.HERMES_SKILL),
        ("ui_cache", SideEffectKind.UISPEC_CACHE),
        ("adapter_state", SideEffectKind.ADAPTER_STATE),
    ]:
        store.write(canary_id, namespace, "secret", f"{namespace}-candidate")
        ledger.append(_entry(canary_id=canary_id, kind=kind))

    controller.baseline_write("memory", "baseline", "safe")
    report = controller.rollback(canary_id, actor="tester")

    assert set(report.dropped_namespaces) == {"memory", "skills", "ui_cache", "adapter_state"}
    assert len(report.quarantined_entry_ids) == 4
    controller.assert_no_poisoning(canary_id)
    assert controller.baseline_read("memory", "secret") is None
    assert controller.baseline_read("skills", "secret") is None
    assert controller.baseline_read("ui_cache", "secret") is None
    assert controller.baseline_read("adapter_state", "secret") is None
    assert controller.baseline_read("memory", "baseline") == "safe"
    assert all(entry.canary_id != canary_id for entry in ledger.promotable_entries())

    later_controller = RollbackController(ledger=ledger, canary_store=store, pointer_store=pointer)
    assert later_controller.baseline_read("memory", "secret") is None
    for namespace in ("memory", "skills", "ui_cache", "adapter_state", "pointer"):
        assert store.read(canary_id, namespace, "secret") is None


def test_pointer_rollback_reverts_candidate_with_cas_and_stale_state_cannot_win():
    ledger = SideEffectLedger()
    store = CanaryScopedStore()
    pointer = ActivePointerStore()
    controller = RollbackController(ledger=ledger, canary_store=store, pointer_store=pointer)
    key = ("tenant/user", "wf")
    canary_id = "canary-pointer"

    prior_version, prior_hashes = pointer.get(key)
    assert prior_version == 0
    pointer.swap(key, prior_version, ["candidate-hash"])
    controller.track_pointer_candidate(canary_id, key, prior_version, prior_hashes, ["candidate-hash"])

    report = controller.rollback(canary_id, actor="tester")

    assert report.pointer_reverted is True
    assert pointer.get(key) == (2, [])
    controller.assert_no_poisoning(canary_id)

    try:
        pointer.swap(key, 1, ["stale-candidate"])
    except ValueError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("stale pointer state advanced")
    assert pointer.get(key) == (2, [])


def test_fresh_controller_rolls_back_pointer_from_durable_ledger_transition():
    ledger = SideEffectLedger()
    store = CanaryScopedStore()
    pointer = ActivePointerStore()
    original = RollbackController(ledger=ledger, canary_store=store, pointer_store=pointer)
    key = ("tenant/user", "wf")
    canary_id = "canary-fresh"

    prior_version, prior_hashes = pointer.get(key)
    candidate_version = pointer.swap(key, prior_version, ["candidate-hash"])
    original.track_pointer_candidate(
        canary_id,
        key,
        prior_version,
        prior_hashes,
        ["candidate-hash"],
        candidate_version=candidate_version,
    )

    fresh = RollbackController(ledger=ledger, canary_store=store, pointer_store=pointer)
    report = fresh.rollback(canary_id, actor="tester")

    assert report.pointer_reverted is True
    assert pointer.get(key) == (2, [])
    fresh.assert_no_poisoning(canary_id)


def test_untracked_pointer_transition_is_detected_from_ledger_and_rollback_fails_closed_without_prior():
    ledger = SideEffectLedger()
    store = CanaryScopedStore()
    pointer = ActivePointerStore()
    controller = RollbackController(ledger=ledger, canary_store=store, pointer_store=pointer)
    key = ("tenant/user", "wf")
    canary_id = "canary-untracked"

    candidate_version = pointer.swap(key, 0, ["candidate-hash"])
    ledger.append(
        LedgerEntry(
            run_id="run-1",
            module_set_hash="set-hash",
            canary_id=canary_id,
            kind=SideEffectKind.POINTER_TRANSITION,
            payload={
                "key": list(key),
                "prior_hashes": [],
                "candidate_version": candidate_version,
                "candidate_hashes": ["candidate-hash"],
            },
        )
    )
    ledger.mark_quarantined(canary_id, actor="tester")

    with pytest.raises(AssertionError, match="active pointer"):
        controller.assert_no_poisoning(canary_id)
    with pytest.raises(RuntimeError, match="prior version"):
        controller.rollback(canary_id, actor="tester")


def test_pointer_rollback_raises_when_cas_revert_fails():
    class FailingRevertPointerStore(ActivePointerStore):
        fail_revert = False

        def swap(self, key, expected_version, new_hashes):
            if self.fail_revert and new_hashes == ["baseline-hash"]:
                raise ValueError("forced CAS failure")
            return super().swap(key, expected_version, new_hashes)

    ledger = SideEffectLedger()
    store = CanaryScopedStore()
    pointer = FailingRevertPointerStore()
    controller = RollbackController(ledger=ledger, canary_store=store, pointer_store=pointer)
    key = ("tenant/user", "wf")
    canary_id = "canary-cas"

    baseline_version = pointer.swap(key, 0, ["baseline-hash"])
    baseline_hashes = pointer.get(key)[1]
    candidate_version = pointer.swap(key, baseline_version, ["candidate-hash"])
    pointer.fail_revert = True
    controller.track_pointer_candidate(
        canary_id,
        key,
        baseline_version,
        baseline_hashes,
        ["candidate-hash"],
        candidate_version=candidate_version,
    )

    with pytest.raises(RuntimeError, match="failed to rollback"):
        controller.rollback(canary_id, actor="tester")
    with pytest.raises(AssertionError, match="active pointer"):
        controller.assert_no_poisoning(canary_id)


def test_stale_reference_from_read_cannot_reach_dropped_canary_state_after_rollback():
    ledger = SideEffectLedger()
    store = CanaryScopedStore()
    controller = RollbackController(ledger=ledger, canary_store=store)
    canary_id = "canary-reference"
    original = {"nested": {"secret": "candidate"}}

    store.write(canary_id, "memory", "secret", original)
    original["nested"]["secret"] = "mutated-after-write"
    stale = store.read(canary_id, "memory", "secret")
    namespace = store.read_namespace(canary_id, "memory")
    stale["nested"]["secret"] = "mutated-stale"
    namespace["secret"]["nested"]["secret"] = "mutated-namespace"

    controller.rollback(canary_id, actor="tester")

    assert stale == {"nested": {"secret": "mutated-stale"}}
    assert namespace == {"secret": {"nested": {"secret": "mutated-namespace"}}}
    assert store.read(canary_id, "memory", "secret") is None
    assert store.read_namespace(canary_id, "memory") == {}


def test_multiple_pointer_transitions_for_one_canary_revert_to_original_baseline():
    ledger = SideEffectLedger()
    store = CanaryScopedStore()
    pointer = ActivePointerStore()
    controller = RollbackController(ledger=ledger, canary_store=store, pointer_store=pointer)
    key = ("tenant/user", "wf")
    canary_id = "canary-multiple"

    baseline_version = pointer.swap(key, 0, ["baseline-hash"])
    baseline_hashes = pointer.get(key)[1]
    candidate_one_version = pointer.swap(key, baseline_version, ["candidate-one"])
    controller.track_pointer_candidate(
        canary_id,
        key,
        baseline_version,
        baseline_hashes,
        ["candidate-one"],
        candidate_version=candidate_one_version,
    )
    candidate_two_version = pointer.swap(key, candidate_one_version, ["candidate-two"])
    controller.track_pointer_candidate(
        canary_id,
        key,
        candidate_one_version,
        ["candidate-one"],
        ["candidate-two"],
        candidate_version=candidate_two_version,
    )

    fresh = RollbackController(ledger=ledger, canary_store=store, pointer_store=pointer)
    report = fresh.rollback(canary_id, actor="tester")

    assert report.pointer_reverted is True
    assert pointer.get(key) == (5, ["baseline-hash"])
    fresh.assert_no_poisoning(canary_id)
