from ultron.composition.manifest import ModuleSetManifest
from ultron.module.model import PersistencePolicy
from ultron.run.manifest import RunManifest


def _module_set():
    return ModuleSetManifest(
        user_scope="tenant/user",
        workflow_fingerprint="wf:chat",
        request_class="chat",
        ordered_module_hashes=["h1", "h2"],
        resolved_prompt_order=["base", "tenant"],
        resolved_tool_allowlist=["read", "write"],
        resolved_ui_panels=["summary"],
        disabled_modules=[],
        conflicts=[],
        safety_policy={"allow_external": False, "max_tokens": 500},
        budget_policy={"usd": 1.5},
        rationale="test",
    ).finalized()


def _manifest():
    return RunManifest.from_manifest_set(
        _module_set(),
        run_id="run-1",
        session_id="session-1",
        active_module_set_id="set-1",
        hermes_version="hermes-ee1a744",
        adapter_version="adapter-1",
        contract_version="contract-1",
        model_snapshot={"provider": "p", "name": "n", "version": "v", "decoding": {"temperature": 0}},
        side_effect_ledger_id="ledger-1",
        created_at=123.456,
        timestamp_source="fixture",
        persistence_mode=PersistencePolicy.ISOLATED,
        candidate_module_id="candidate-1",
        variation_primitive_id="primitive-1",
        canary_id="canary-1",
        resolved_skill_refs=["skill-a"],
        resolved_topology_hash="topology-hash",
        resolved_ui_spec_hash="ui-hash",
        workspace_snapshot_id="workspace-1",
        external_call_policy_id="external-deny",
    )


def test_run_manifest_sign_verify_and_tamper_detection():
    signed = _manifest().sign("secret")

    assert signed.verify("secret") is True
    assert signed.verify("wrong") is False
    assert signed.model_copy(update={"resolved_tool_allowlist": ["read"]}).verify("secret") is False
    assert signed.model_copy(update={"model_snapshot": {"provider": "evil"}}).verify("secret") is False
    assert signed.model_copy(update={"ordered_module_hashes": ["h2", "h1"]}).verify("secret") is False
    assert signed.model_copy(update={"created_at": 124.0}).verify("secret") is False


def test_run_manifest_signature_is_deterministic_for_identical_effective_state():
    first = _manifest().sign("secret")
    second = _manifest().sign("secret")

    assert first.signature == second.signature
    assert first.canonical_payload() == second.canonical_payload()
    assert "signature" not in first.canonical_payload()
