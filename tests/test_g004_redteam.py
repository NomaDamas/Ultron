import pytest

from ultron.composition.manifest import ModuleSetManifest
from ultron.ledger.canary_store import CANARY_NAMESPACES, CanaryScopedStore, RollbackController
from ultron.ledger.side_effect_ledger import LedgerEntry, SideEffectKind, SideEffectLedger
from ultron.module.model import PersistencePolicy
from ultron.registry.pointer import ActivePointerStore
from ultron.run.manifest import RunManifest
from ultron.run.signer import FixtureKeyProvider, ManifestSigner


SIGNER = ManifestSigner.from_provider("redteam", FixtureKeyProvider({"redteam": "redteam-secret"}))
WRONG_SIGNER = ManifestSigner.from_provider("wrong-redteam", FixtureKeyProvider({"wrong-redteam": "wrong-redteam-secret"}))


ATTACK_NAMESPACES = ("memory", "skills", "ui_cache", "adapter_state")


def _module_set() -> ModuleSetManifest:
    return ModuleSetManifest(
        user_scope="tenant/redteam",
        workflow_fingerprint="wf:g004",
        request_class="chat",
        ordered_module_hashes=["hash-a", "hash-b", "hash-c"],
        resolved_prompt_order=["base", "tenant", "candidate"],
        resolved_tool_allowlist=["read", "write", "bash"],
        resolved_ui_panels=["summary", "debug"],
        disabled_modules=[],
        conflicts=[],
        safety_policy={"allow_external": False, "max_tokens": 777},
        budget_policy={"usd": 2.5, "hard_cap": True},
        rationale="g004 red-team fixture",
    ).finalized()


def _manifest() -> RunManifest:
    return RunManifest.from_manifest_set(
        _module_set(),
        run_id="run-redteam",
        session_id="session-redteam",
        active_module_set_id="set-redteam",
        hermes_version="hermes-redteam",
        adapter_version="adapter-redteam",
        contract_version="contract-redteam",
        model_snapshot={
            "provider": "fixture-provider",
            "name": "fixture-model",
            "version": "2026-06-11",
            "decoding": {"temperature": 0.1, "top_p": 0.9},
        },
        side_effect_ledger_id="ledger-redteam",
        created_at=1_779_999_999.125,
        timestamp_source="fixture-clock",
        persistence_mode=PersistencePolicy.ISOLATED,
        candidate_module_id="candidate-redteam",
        variation_primitive_id="primitive-redteam",
        canary_id="canary-redteam",
        resolved_skill_refs=["skill-alpha", "skill-beta"],
        resolved_topology_hash="topology-redteam",
        resolved_ui_spec_hash="ui-redteam",
        workspace_snapshot_id="workspace-redteam",
        external_call_policy_id="external-deny",
    )


def _entry(
    *,
    canary_id: str | None,
    run_id: str,
    kind: SideEffectKind,
    payload: dict | None = None,
) -> LedgerEntry:
    return LedgerEntry(
        run_id=run_id,
        module_set_hash=f"set-{run_id}",
        module_hash=f"module-{run_id}",
        canary_id=canary_id,
        kind=kind,
        payload=payload or {"run": run_id, "canary": canary_id, "kind": kind.value},
        created_at=float(len(run_id) + len(kind.value)),
    )


def test_run_manifest_signature_rejects_field_tampering_wrong_key_and_is_deterministic():
    signed = _manifest().sign(signer=SIGNER)

    tampered_cases = {
        "created_at": signed.model_copy(update={"created_at": signed.created_at + 0.001}),
        "nested_model_snapshot_value": signed.model_copy(
            update={
                "model_snapshot": {
                    **signed.model_snapshot,
                    "decoding": {**signed.model_snapshot["decoding"], "temperature": 0.7},
                }
            },
            deep=True,
        ),
        "ordered_module_hash": signed.model_copy(
            update={"ordered_module_hashes": ["hash-a", "hash-evil", "hash-c"]}
        ),
        "resolved_tool_allowlist_entry": signed.model_copy(
            update={"resolved_tool_allowlist": ["read", "write", "exfiltrate"]}
        ),
        "persistence_mode": signed.model_copy(update={"persistence_mode": PersistencePolicy.NORMAL}),
    }

    assert signed.verify(signer=SIGNER) is True
    assert signed.verify(signer=WRONG_SIGNER) is False
    for test_id, tampered in tampered_cases.items():
        assert tampered.verify(signer=SIGNER) is False, test_id

    assert _manifest().sign(signer=SIGNER).signature == signed.signature
    assert _manifest().sign(signer=SIGNER).canonical_payload() == signed.canonical_payload()


def test_side_effect_ledger_keeps_all_entries_and_quarantine_only_blocks_promotion():
    ledger = SideEffectLedger()
    entries = [
        _entry(canary_id="canary-a", run_id="run-1", kind=SideEffectKind.HERMES_MEMORY),
        _entry(canary_id="canary-a", run_id="run-1", kind=SideEffectKind.HERMES_SKILL),
        _entry(canary_id="canary-b", run_id="run-2", kind=SideEffectKind.UISPEC_CACHE),
        _entry(canary_id="canary-b", run_id="run-2", kind=SideEffectKind.ADAPTER_STATE),
        _entry(canary_id=None, run_id="run-baseline", kind=SideEffectKind.TELEMETRY),
        _entry(canary_id="canary-a", run_id="run-3", kind=SideEffectKind.EXTERNAL_CALL),
    ]
    entry_ids = [ledger.append(entry) for entry in entries]

    assert [entry.entry_id for entry in ledger.entries_for_run("run-1")] == entry_ids[:2]
    assert [entry.entry_id for entry in ledger.entries_for_run("run-2")] == entry_ids[2:4]
    assert [entry.entry_id for entry in ledger.entries_for_canary("canary-a")] == [
        entry_ids[0],
        entry_ids[1],
        entry_ids[5],
    ]

    quarantined = ledger.mark_quarantined("canary-a", actor="tester")

    assert quarantined == [entry_ids[0], entry_ids[1], entry_ids[5]]
    assert [entry.entry_id for entry in ledger.entries_for_canary("canary-a")] == [
        entry_ids[0],
        entry_ids[1],
        entry_ids[5],
    ]
    assert [entry.entry_id for entry in ledger.entries_for_run("run-1")] == entry_ids[:2]
    assert all(entry.quarantined for entry in ledger.entries_for_canary("canary-a"))
    assert [entry.entry_id for entry in ledger.promotable_entries()] == entry_ids[2:5]
    assert {entry.canary_id for entry in ledger.promotable_entries()} == {"canary-b", None}


def test_rollback_hammers_no_poisoning_paths_aliases_promotable_entries_and_second_run():
    canary_id = "canary-poison"
    ledger = SideEffectLedger()
    store = CanaryScopedStore()
    pointer = ActivePointerStore()
    controller = RollbackController(ledger=ledger, canary_store=store, pointer_store=pointer)
    keys_by_namespace = {
        "memory": "memory-secret",
        "skills": "skill-secret",
        "ui_cache": "ui-secret",
        "adapter_state": "adapter-secret",
    }
    kinds_by_namespace = {
        "memory": SideEffectKind.HERMES_MEMORY,
        "skills": SideEffectKind.HERMES_SKILL,
        "ui_cache": SideEffectKind.UISPEC_CACHE,
        "adapter_state": SideEffectKind.ADAPTER_STATE,
    }

    aliases = {}
    namespace_aliases = {}
    for namespace, key in keys_by_namespace.items():
        candidate_value = {"canary": canary_id, "namespace": namespace, "payload": ["poison"]}
        store.write(canary_id, namespace, key, candidate_value)
        aliases[namespace] = store.read(canary_id, namespace, key)
        namespace_aliases[namespace] = store.read_namespace(canary_id, namespace)
        ledger.append(
            _entry(
                canary_id=canary_id,
                run_id=f"run-{namespace}",
                kind=kinds_by_namespace[namespace],
                payload={"namespace": namespace, "key": key},
            )
        )
        controller.baseline_write(namespace, f"baseline-{key}", {"safe": namespace})

    report = controller.rollback(canary_id, actor="tester")

    assert set(report.dropped_namespaces) == set(ATTACK_NAMESPACES)
    assert len(report.quarantined_entry_ids) == len(ATTACK_NAMESPACES)
    controller.assert_no_poisoning(canary_id)

    for namespace, key in keys_by_namespace.items():
        assert store.read(canary_id, namespace, key) is None
        assert store.read_namespace(canary_id, namespace) == {}
        assert controller.baseline_read(namespace, key) is None
        assert controller.baseline_read(namespace, f"baseline-{key}") == {"safe": namespace}

    assert store.read_namespace(canary_id, "pointer") == {}
    assert all(entry.canary_id != canary_id for entry in ledger.promotable_entries())

    second_run = RollbackController(ledger=ledger, canary_store=store, pointer_store=pointer)
    second_run.assert_no_poisoning(canary_id)
    for namespace, key in keys_by_namespace.items():
        assert second_run.baseline_read(namespace, key) is None
        assert store.read(canary_id, namespace, key) is None

    for namespace, alias in aliases.items():
        alias["payload"].append("resurrected")
        alias["new"] = "mutated-after-rollback"
        namespace_aliases[namespace][keys_by_namespace[namespace]]["payload"].append("namespace-resurrected")
        assert store.read(canary_id, namespace, keys_by_namespace[namespace]) is None
        assert second_run.baseline_read(namespace, keys_by_namespace[namespace]) is None

    controller.assert_no_poisoning(canary_id)
    second_run.assert_no_poisoning(canary_id)


@pytest.mark.parametrize("namespace", CANARY_NAMESPACES)
def test_direct_canary_store_reads_are_empty_for_all_namespaces_after_rollback(namespace):
    canary_id = "canary-direct-empty"
    store = CanaryScopedStore()
    controller = RollbackController(canary_store=store)
    if namespace != "pointer":
        store.write(canary_id, namespace, "secret", {"poison": namespace})

    controller.rollback(canary_id, actor="tester")

    assert store.read(canary_id, namespace, "secret") is None
    assert store.read_namespace(canary_id, namespace) == {}


def test_pointer_rollback_reverts_candidate_via_cas_and_rejects_stale_candidate_swap():
    canary_id = "canary-pointer-redteam"
    ledger = SideEffectLedger()
    store = CanaryScopedStore()
    pointer = ActivePointerStore()
    controller = RollbackController(ledger=ledger, canary_store=store, pointer_store=pointer)
    key = ("tenant/redteam", "wf:g004")

    prior_version, prior_hashes = pointer.get(key)
    candidate_version = pointer.swap(key, prior_version, ["candidate-v1"])
    assert candidate_version == prior_version + 1
    controller.track_pointer_candidate(canary_id, key, prior_version, prior_hashes, ["candidate-v1"])
    ledger.append(_entry(canary_id=canary_id, run_id="run-pointer", kind=SideEffectKind.POINTER_TRANSITION))

    report = controller.rollback(canary_id, actor="tester")

    assert report.pointer_reverted is True
    assert pointer.get(key) == (candidate_version + 1, prior_hashes)
    controller.assert_no_poisoning(canary_id)

    with pytest.raises(ValueError, match="stale"):
        pointer.swap(key, candidate_version, ["stale-candidate"])
    assert pointer.get(key) == (candidate_version + 1, prior_hashes)
