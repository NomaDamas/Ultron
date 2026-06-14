import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ultron.app.server import create_app
from ultron.evolution.variation import VariationPrimitive
from ultron.ledger.side_effect_ledger import SideEffectKind
from ultron.evolution.loop import StabilityControls
from ultron.module.model import FitnessMetadata, PromotionState
from ultron.registry.store import ModuleLifecycle

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_JS = ROOT / "src" / "ultron" / "app" / "static" / "dashboard.js"
CHAT_JS = ROOT / "src" / "ultron" / "app" / "static" / "chat.js"
RAW_REQUEST = "raw story5 request sentinel SECRET_TOKEN=abc123"
RAW_COMMENT = "raw story5 feedback sentinel password=hunter2"


def _client():
    return TestClient(create_app())


def _csrf(client):
    return client.get("/dashboard").cookies["ultron_csrf"]


def _action(client, csrf, action_type, payload=None, pointer_version=None):
    body = {"type": action_type, "payload": payload or {}, "csrf_token": csrf}
    if pointer_version is not None:
        body["active_pointer_version"] = pointer_version
    elif action_type not in {"SUBMIT_REQUEST", "GIVE_FEEDBACK"}:
        body["active_pointer_version"] = client.app.state.triage.current_pointer_version()
    return client.post("/api/action", headers={"X-CSRF-Token": csrf}, json=body)


def _ledger_actors(engine):
    return [entry.actor for entry in getattr(engine.ledger, "_entries", engine._ledger_entries())]


def _has_actor_for_kind(engine, kind):
    entries = list(getattr(engine.ledger, "_entries", engine._ledger_entries()))
    for entry in entries:
        if str(entry.kind) == str(kind) or getattr(entry.kind, "value", entry.kind) == getattr(kind, "value", kind):
            if entry.actor == "local-operator":
                return True
    return False


def test_get_personalization_read_only_redacted_and_csp():
    client = _client()
    csrf = _csrf(client)
    submit = _action(client, csrf, "SUBMIT_REQUEST", {"request_text": RAW_REQUEST})
    assert submit.status_code == 200
    run_id = submit.json()["result"]["run_manifest"]["run_id"]
    feedback = _action(client, csrf, "GIVE_FEEDBACK", {"run_id": run_id, "rating": -1, "comment": RAW_COMMENT})
    assert feedback.status_code == 200

    engine = client.app.state.triage
    before = (engine.current_pointer_version(), len(engine._registry_entries()), len(engine._ledger_entries()), engine.last_candidate_hash, engine.last_canary_id)
    response = client.get("/api/personalization")
    after = (engine.current_pointer_version(), len(engine._registry_entries()), len(engine._ledger_entries()), engine.last_candidate_hash, engine.last_canary_id)

    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert after == before
    payload = response.json()
    assert payload["summary"]["summary_hash"].startswith("sha256:")
    assert payload["summary"]["redaction"] == {
        "raw_request_text": True,
        "raw_feedback_comments": True,
        "secrets": True,
        "hashed_scope": True,
    }
    trail = payload["causal_trail"]
    assert trail["aggregates"]["signal_counts"]["runs"] >= 1
    assert "module_usage" in trail["aggregates"]
    assert "evidence_labels" in trail["aggregates"]
    assert trail["last_proposal"]["candidate_short_hash"] == before[3][:12]
    assert trail["last_proposal"]["rationale"]
    assert trail["approval_state"] in {"pending-approval", "canary"}
    dumped = json.dumps(payload)
    for forbidden in [RAW_REQUEST, RAW_COMMENT, "SECRET_TOKEN", "abc123", "password", "hunter2"]:
        assert forbidden not in dumped


def test_dashboard_fetches_personalization_and_chat_stays_chat_only():
    dashboard = DASHBOARD_JS.read_text()
    chat = CHAT_JS.read_text()
    assert "/api/personalization" in dashboard
    assert "Personalization / Self-evolution" in dashboard
    assert "createElement" in dashboard
    assert "textContent" in dashboard
    assert "/api/personalization" not in chat
    assert "Personalization / Self-evolution" not in chat


MUTATING_ACTIONS = [
    "SUBMIT_REQUEST",
    "GIVE_FEEDBACK",
    "RUN_BENCHMARK",
    "APPROVE_PROMOTION",
    "ROLLBACK_CANARY",
    "RESTORE_MODULE",
    "REQUEST_PERMISSION_EXPANSION",
]


@pytest.mark.parametrize("action_type", MUTATING_ACTIONS)
def test_every_mutating_action_requires_session_csrf_and_records_actor(action_type):
    no_session = _client()
    assert no_session.post("/api/action", json={"type": action_type, "payload": {}, "active_pointer_version": 1}).status_code == 401

    client = _client()
    csrf = _csrf(client)
    engine = client.app.state.triage
    assert client.post("/api/action", json={"type": action_type, "payload": {}, "active_pointer_version": engine.current_pointer_version()}).status_code == 403

    payload = {}
    pointer_version = engine.current_pointer_version()
    expected_kind = SideEffectKind.TELEMETRY
    if action_type == "SUBMIT_REQUEST":
        payload = {"request_text": "story5 actor submit"}
        expected_kind = SideEffectKind.ADAPTER_STATE
    elif action_type == "GIVE_FEEDBACK":
        submit = _action(client, csrf, "SUBMIT_REQUEST", {"request_text": "story5 feedback seed"})
        payload = {"run_id": submit.json()["result"]["run_manifest"]["run_id"], "rating": 1, "comment": "ok"}
        expected_kind = SideEffectKind.FEEDBACK_EVENT
    elif action_type in {"RUN_BENCHMARK", "APPROVE_PROMOTION"}:
        submit = _action(client, csrf, "SUBMIT_REQUEST", {"request_text": "story5 benchmark seed"})
        candidate_hash = submit.json()["candidate"]["content_hash"]
        canary_id = submit.json()["canary_id"]
        payload = {"candidate_hash": candidate_hash, "canary_id": canary_id}
        if action_type == "APPROVE_PROMOTION":
            bench = _action(client, csrf, "RUN_BENCHMARK", payload)
            assert bench.status_code == 200
            pointer_version = engine.current_pointer_version()
            expected_kind = SideEffectKind.POINTER_TRANSITION
    elif action_type == "ROLLBACK_CANARY":
        canary = engine.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "story5 rollback"})
        payload = {"canary_id": canary["canary_id"]}
        expected_kind = SideEffectKind.QUARANTINE
    elif action_type == "RESTORE_MODULE":
        engine.seed_baseline()
        target = engine.pointer_store.get(engine.pointer_key)[1][0]
        engine.evolution_loop.prune(target, is_critical_seed=True, approved=True)
        engine.registry.set_lifecycle(target, ModuleLifecycle.PRUNED)
        payload = {"module_hash": target}
        pointer_version = engine.current_pointer_version()
        expected_kind = SideEffectKind.POINTER_TRANSITION
    elif action_type == "REQUEST_PERMISSION_EXPANSION":
        payload = {"tool": "network", "reason": "story5 audit"}

    response = _action(client, csrf, action_type, payload, pointer_version)
    assert response.status_code == 200, response.text
    if action_type != "RESTORE_MODULE":
        assert _has_actor_for_kind(engine, expected_kind)
    else:
        assert response.json()["restored"]["restored"] is True
    if action_type != "RESTORE_MODULE":
        assert "local-operator" in _ledger_actors(engine)


def test_submit_request_attributes_canary_run_and_ledger_to_actor():
    client = _client()
    csrf = _csrf(client)

    response = _action(client, csrf, "SUBMIT_REQUEST", {"request_text": "story5 canary actor"})

    assert response.status_code == 200, response.text
    engine = client.app.state.triage
    canary_id = response.json()["canary_id"]
    canary_manifest = next(manifest for manifest in engine.run_manifests if manifest.canary_id == canary_id)
    assert canary_manifest.actor == "local-operator"
    canary_entries = [entry for entry in engine._ledger_entries() if entry.canary_id == canary_id and entry.kind is SideEffectKind.ADAPTER_STATE]
    assert canary_entries
    assert all(entry.actor == "local-operator" for entry in canary_entries)


def test_restore_module_in_memory_records_prune_and_restore_actor_ledgers():
    client = _client()
    csrf = _csrf(client)
    engine = client.app.state.triage
    engine.seed_baseline()
    target = engine.pointer_store.get(engine.pointer_key)[1][0]
    engine.evolution_loop.prune(target, is_critical_seed=True, approved=True)
    engine.registry.set_lifecycle(target, ModuleLifecycle.PRUNED)
    pointer_version = engine.current_pointer_version()

    response = _action(client, csrf, "RESTORE_MODULE", {"module_hash": target}, pointer_version)

    assert response.status_code == 200, response.text
    assert response.json()["restored"] == {"module_hash": target, "pruned": True, "restored": True}
    entries = [entry for entry in engine._ledger_entries() if entry.kind is SideEffectKind.POINTER_TRANSITION and entry.module_hash == target]
    assert [entry.payload["action"] for entry in entries[-1:]] == ["restore"]
    assert all(entry.actor == "local-operator" for entry in entries[-1:])
    assert all(entry.payload["actor"] == "local-operator" for entry in entries[-1:])


def test_atrophy_and_restore_in_memory_records_prune_and_restore_actor_ledgers():
    client = _client()
    engine = client.app.state.triage
    canary = engine.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "story5 restore audit"})
    target = canary["candidate"].content_hash
    engine.benchmark_and_decide(target, canary_id=canary["canary_id"])
    engine.approve_promotion(target, engine.current_pointer_version(), actor="local-operator")

    response = engine.atrophy_and_restore(target, actor="local-operator")

    assert response == {"module_hash": target, "pruned": True, "restored": True}
    entries = [entry for entry in engine._ledger_entries() if entry.kind is SideEffectKind.POINTER_TRANSITION and entry.module_hash == target]
    assert [entry.payload["action"] for entry in entries[-2:]] == ["prune", "restore"]
    assert all(entry.actor == "local-operator" for entry in entries[-2:])
    assert all(entry.payload["actor"] == "local-operator" for entry in entries[-2:])


def test_run_atrophy_scan_in_memory_defaults_actor_on_prune_ledger():
    client = _client()
    engine = client.app.state.triage
    engine.evolution_loop.controls = StabilityControls(active_module_cap=4, diversity_floor=1, promotion_cooldown_s=0, prune_cooldown_s=0)
    engine.seed_baseline()
    result = engine.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "story5 atrophy default actor"})
    target = result["candidate"].content_hash
    candidate = engine.registry.get(target).module.model_copy(
        update={"fitness": FitnessMetadata(primary_metric=-1.0, usage_count=0, last_used_at=1.0, decay_score=1.0, promotion_state=PromotionState.CANDIDATE)},
        deep=True,
    )
    engine._store_fitness_update(target, candidate)
    engine.registry.set_lifecycle(target, ModuleLifecycle.SURVIVOR)
    version, active = engine.pointer_store.get(engine.pointer_key)
    engine.pointer_store.swap(engine.pointer_key, version, active + [target])

    scan = engine.run_atrophy_scan(1000.0)

    assert scan["pruned"] == [target]
    entries = [entry for entry in engine._ledger_entries() if entry.kind is SideEffectKind.POINTER_TRANSITION and entry.module_hash == target and entry.payload["action"] == "atrophy_prune"]
    assert entries
    assert entries[-1].actor == "local-operator"
    assert entries[-1].payload["actor"] == "local-operator"


def test_run_atrophy_scan_in_memory_records_explicit_actor_on_prune_ledger():
    client = _client()
    engine = client.app.state.triage
    engine.evolution_loop.controls = StabilityControls(active_module_cap=4, diversity_floor=1, promotion_cooldown_s=0, prune_cooldown_s=0)
    engine.seed_baseline()
    result = engine.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "story5 atrophy explicit actor"})
    target = result["candidate"].content_hash
    candidate = engine.registry.get(target).module.model_copy(
        update={"fitness": FitnessMetadata(primary_metric=-1.0, usage_count=0, last_used_at=1.0, decay_score=1.0, promotion_state=PromotionState.CANDIDATE)},
        deep=True,
    )
    engine._store_fitness_update(target, candidate)
    engine.registry.set_lifecycle(target, ModuleLifecycle.SURVIVOR)
    version, active = engine.pointer_store.get(engine.pointer_key)
    engine.pointer_store.swap(engine.pointer_key, version, active + [target])

    scan = engine.run_atrophy_scan(1000.0, actor="story5-operator")

    assert scan["pruned"] == [target]
    entries = [entry for entry in engine._ledger_entries() if entry.kind is SideEffectKind.POINTER_TRANSITION and entry.module_hash == target and entry.payload["action"] == "atrophy_prune"]
    assert entries
    assert entries[-1].actor == "story5-operator"
    assert entries[-1].payload["actor"] == "story5-operator"
