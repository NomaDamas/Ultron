import pytest

from ultron.app.triage import DEFAULT_SCOPE, DEFAULT_WORKFLOW, TriageApp
from ultron.evolution.variation import VariationPrimitive
from ultron.hermes.adapter import AdapterRunResult


class RejectingLiveAdapter:
    is_live = True
    provider_id = "live-provider"

    def __init__(self, *, provider="live-provider", model="clean-model", snapshot=None):
        self.provider = provider
        self.model = model
        self.snapshot = snapshot or {"provider": provider, "name": "clean-model"}
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return AdapterRunResult(
            session_id=request.session_id,
            trajectory_id="live-traj",
            trajectory_path=None,
            model_provider=self.provider,
            model_name=self.model,
            model_snapshot=dict(self.snapshot),
            output={"plan": [], "risks": [], "tests": []},
            tool_calls=0,
            measured_guardrails={},
            outcome_label="ok",
        )


@pytest.mark.parametrize(
    "adapter",
    [
        RejectingLiveAdapter(provider="stub", snapshot={"provider": "live-provider", "name": "clean-model"}),
        RejectingLiveAdapter(provider="fake-deterministic", snapshot={"provider": "live-provider", "name": "clean-model"}),
        RejectingLiveAdapter(model="stub-x", snapshot={"provider": "live-provider", "name": "clean-model"}),
        RejectingLiveAdapter(model="fake-run", snapshot={"provider": "live-provider", "name": "clean-model"}),
        RejectingLiveAdapter(snapshot={"provider": "live-provider", "name": "clean-model", "is_stub": True}),
        RejectingLiveAdapter(provider="other-live", snapshot={"provider": "other-live", "name": "clean-model"}),
    ],
)
def test_live_guard_rejects_before_start_run_manifest_or_ledger(adapter):
    app = TriageApp(adapter=adapter)
    with pytest.raises(ValueError):
        app.start_run(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "reject")
    assert len(adapter.requests) == 1
    assert app.last_manifest is None
    assert not app.ledger.entries_for_run(adapter.requests[0].run_id)


@pytest.mark.parametrize(
    "adapter",
    [
        RejectingLiveAdapter(provider="stub", snapshot={"provider": "live-provider", "name": "clean-model"}),
        RejectingLiveAdapter(provider="fake-deterministic", snapshot={"provider": "live-provider", "name": "clean-model"}),
        RejectingLiveAdapter(model="stub-x", snapshot={"provider": "live-provider", "name": "clean-model"}),
        RejectingLiveAdapter(model="fake-run", snapshot={"provider": "live-provider", "name": "clean-model"}),
        RejectingLiveAdapter(snapshot={"provider": "live-provider", "name": "clean-model", "is_stub": True}),
        RejectingLiveAdapter(provider="other-live", snapshot={"provider": "other-live", "name": "clean-model"}),
    ],
)
def test_live_guard_rejects_before_canary_real_mutations(adapter):
    app = TriageApp(adapter=adapter)
    app.seed_baseline()
    before_version, before_active = app.pointer_store.get(app.pointer_key)
    with pytest.raises(ValueError):
        app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "blocked"})
    assert len(adapter.requests) == 1
    assert app.pointer_store.get(app.pointer_key) == (before_version, before_active)
    assert app.last_manifest is None
    assert app.last_candidate_hash is None
    assert not app.ledger.entries_for_run(adapter.requests[0].run_id)
    assert app.canary_store.read_namespace(adapter.requests[0].canary_id, "adapter_state") == {}
    with pytest.raises(KeyError):
        app.registry.get(adapter.requests[0].candidate_module_id)


def test_non_live_default_e2e_unaffected():
    app = TriageApp()
    run = app.start_run(DEFAULT_SCOPE, DEFAULT_WORKFLOW, "non-live ok")
    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "non-live-candidate"})
    assert run["run_manifest"].verify()
    assert canary["run_manifest"].verify()
    assert app.canary_active(canary["canary_id"])
