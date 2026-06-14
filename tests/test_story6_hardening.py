import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ultron.app.server import create_app
from ultron.evaluation.harness import GuardrailMetrics, PairedTask
from ultron.evolution.loop import StabilityControls
from ultron.evolution.variation import VariationPrimitive
from ultron.hermes.adapter import LiveHermesUnavailable, PinnedHermesAdapter
from ultron.hermes.runner import SubprocessHermesRunner
from ultron.ledger.side_effect_ledger import SideEffectKind
from ultron.model_provider import HttpModelProvider
from ultron.module.model import FitnessMetadata, PromotionState
from ultron.registry.store import ModuleLifecycle
from ultron.ui.runtime import AnimationHint
from ultron.ui.generator import LiveModelUnavailable

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "ultron" / "app" / "static"

MUTATING_ACTIONS = [
    "SUBMIT_REQUEST",
    "GIVE_FEEDBACK",
    "RUN_BENCHMARK",
    "APPROVE_PROMOTION",
    "ROLLBACK_CANARY",
    "RESTORE_MODULE",
    "REQUEST_PERMISSION_EXPANSION",
]
PRIVILEGED_ACTIONS = set(MUTATING_ACTIONS) - {"SUBMIT_REQUEST", "GIVE_FEEDBACK"}
SECRET_VALUES = [
    "story6-raw-sentinel-629d0d88",
    "story6 feedback raw sentinel",
    "ghp_story6SECRETtoken1234567890",
    "sk-story6SECRETtoken1234567890",
    "story6-secret@example.com",
    "0123456789abcdef0123456789abcdef01234567",
]


def _client():
    return TestClient(create_app())


def _csrf(client):
    return client.get("/").cookies["ultron_csrf"]


def _action(client, csrf, action_type, payload=None, pointer_version=None):
    body = {"type": action_type, "payload": payload or {}, "csrf_token": csrf}
    if action_type in PRIVILEGED_ACTIONS:
        body["active_pointer_version"] = client.app.state.triage.current_pointer_version() if pointer_version is None else pointer_version
    return client.post("/api/action", headers={"X-CSRF-Token": csrf}, json=body)


def _dump(value):
    return json.dumps(value, sort_keys=True, default=str)


def test_csp_shells_and_static_assets_are_strict_scan_clean():
    client = _client()
    for path, script in [("/", "/static/chat.js"), ("/dashboard", "/static/dashboard.js")]:
        response = client.get(path)
        assert response.status_code == 200
        csp = response.headers["content-security-policy"]
        assert csp == "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        assert f'<script src="{script}"></script>' in response.text
        assert re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", response.text, re.I) is None
        assert "<style" not in response.text.lower()
        assert "style=" not in response.text.lower()

    forbidden_js = ["innerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write", "createElement('script'", 'createElement("script"', ".style", "className = props", "className = component", "className = data", "className = envelope"]
    for name in ["chat.js", "dashboard.js"]:
        source = (STATIC / name).read_text()
        for token in forbidden_js:
            assert token not in source, f"{name} contains unsafe renderer token {token}"
        assert "createElement" in source
        assert "textContent" in source

    for name in ["chat.css", "dashboard.css"]:
        css = (STATIC / name).read_text()
        lowered = css.lower()
        assert "prefers-reduced-motion" in css
        assert "@import" not in lowered
        assert "url(javascript:" not in lowered.replace(" ", "")
        assert "expression(" not in lowered

    chat_css = (STATIC / "chat.css").read_text()
    assert set(re.findall(r"\.anim-([a-z-]+)\b", chat_css)) == {"fade-in", "slide-up", "pulse-glow", "reticle-scan", "expand"}


def test_no_raw_request_feedback_or_secrets_across_envelope_and_read_surfaces():
    client = _client()
    csrf = _csrf(client)
    request = "Please triage story6-raw-sentinel-629d0d88 ghp_story6SECRETtoken1234567890 sk-story6SECRETtoken1234567890 story6-secret@example.com 0123456789abcdef0123456789abcdef01234567"
    submit = _action(client, csrf, "SUBMIT_REQUEST", {"request_text": request})
    assert submit.status_code == 200, submit.text
    run_id = submit.json()["result"]["run_manifest"]["run_id"]
    feedback = _action(client, csrf, "GIVE_FEEDBACK", {"run_id": run_id, "rating": -1, "comment": "story6 feedback raw sentinel ghp_story6SECRETtoken1234567890"})
    assert feedback.status_code == 200, feedback.text

    surfaces = {"InlineGenUiEnvelope": submit.json()["envelope"]}
    for endpoint in ["/api/personalization", "/api/toolbelt", "/api/ecology", "/api/runs", "/api/ledger", "/api/metrics"]:
        response = client.get(endpoint)
        assert response.status_code == 200
        surfaces[endpoint] = response.json()
    combined = _dump(surfaces)
    for secret in SECRET_VALUES:
        assert secret not in combined
    assert "[redacted]" in combined or "redacted" in combined.lower()


def test_animation_hint_budget_and_reduced_motion_runtime_guards():
    assert AnimationHint(kind="fade_in", duration_ms=1200, delay_ms=1000, reduced_motion_fallback="none")
    for payload in [
        {"kind": "fade_in", "duration_ms": 1201, "delay_ms": 0, "reduced_motion_fallback": "none"},
        {"kind": "fade_in", "duration_ms": 1, "delay_ms": 1001, "reduced_motion_fallback": "none"},
        {"kind": "spin", "duration_ms": 1, "delay_ms": 0, "reduced_motion_fallback": "none"},
        {"kind": "slide_up", "duration_ms": 1, "delay_ms": 0},
        {"kind": "slide_up", "duration_ms": 1, "delay_ms": 0, "reduced_motion_fallback": "expand"},
    ]:
        with pytest.raises(ValidationError):
            AnimationHint(**payload)
    chat_js = (STATIC / "chat.js").read_text()
    assert "prefers-reduced-motion: reduce" in chat_js
    assert "if (REDUCED_MOTION) return" in chat_js


@pytest.mark.parametrize("action_type", MUTATING_ACTIONS)
def test_every_mutating_action_session_csrf_and_privileged_pointer_scope_gates(action_type):
    no_session = _client()
    payload = {"request_text": "story6 gate"} if action_type == "SUBMIT_REQUEST" else {}
    assert no_session.post("/api/action", json={"type": action_type, "payload": payload, "active_pointer_version": 1}).status_code == 401

    client = _client()
    csrf = _csrf(client)
    engine = client.app.state.triage
    assert client.post("/api/action", json={"type": action_type, "payload": payload, "active_pointer_version": engine.current_pointer_version()}).status_code == 403

    if action_type in PRIVILEGED_ACTIONS:
        denied = _action(client, csrf, action_type, {}, pointer_version=0)
        assert denied.status_code == 403


def test_benchmark_provenance_rollback_no_poisoning_actor_audit_and_read_only_secret_safety():
    client = _client()
    csrf = _csrf(client)
    engine = client.app.state.triage
    engine.evolution_loop.controls = StabilityControls(active_module_cap=4, diversity_floor=1, promotion_cooldown_s=0, prune_cooldown_s=0)

    submit = _action(client, csrf, "SUBMIT_REQUEST", {"request_text": "story6 gates sk-secret-no-leak"})
    assert submit.status_code == 200, submit.text
    candidate_hash = submit.json()["candidate"]["content_hash"]
    canary_id = submit.json()["canary_id"]

    manual = engine.evaluate_and_decide(candidate_hash, [PairedTask(task_id=f"manual-{i}", baseline_metric=1.0, candidate_metric=1.4) for i in range(10)], canary_id, GuardrailMetrics(), GuardrailMetrics())
    assert manual["report"].promotable is True
    assert manual["report"].provenance == "manual"
    assert manual["report"].benchmark_fixture_id is None
    assert manual["report"].benchmark_task_trajectory_ids == {}
    assert not engine.has_promotable_evidence(candidate_hash)
    assert _action(client, csrf, "APPROVE_PROMOTION", {"candidate_hash": candidate_hash}).status_code == 403

    benchmark = _action(client, csrf, "RUN_BENCHMARK", {"candidate_hash": candidate_hash, "canary_id": canary_id})
    assert benchmark.status_code == 200, benchmark.text
    assert benchmark.json()["evaluation"]["report"]["provenance"] == "benchmark_runner"
    assert benchmark.json()["evaluation"]["report"]["promotable"] is True
    approve = _action(client, csrf, "APPROVE_PROMOTION", {"candidate_hash": candidate_hash})
    assert approve.status_code == 200, approve.text

    rollback_canary = engine.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "story6 rollback"}, actor="local-operator")
    rollback = _action(client, csrf, "ROLLBACK_CANARY", {"canary_id": rollback_canary["canary_id"]})
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["rollback"]["dropped_namespaces"]
    assert engine.canary_active(rollback_canary["canary_id"]) is False

    target = candidate_hash
    candidate = engine.registry.get(target).module.model_copy(update={"fitness": FitnessMetadata(primary_metric=-1.0, usage_count=0, last_used_at=1.0, decay_score=1.0, promotion_state=PromotionState.SURVIVOR)}, deep=True)
    engine._store_fitness_update(target, candidate)
    engine.run_atrophy_scan(1000.0)
    engine.atrophy_and_restore(target, actor="local-operator")
    engine.registry.set_lifecycle(target, ModuleLifecycle.PRUNED)
    restore = _action(client, csrf, "RESTORE_MODULE", {"module_hash": target})
    assert restore.status_code == 200, restore.text

    audited = [entry for entry in engine._ledger_entries() if entry.kind in {SideEffectKind.POINTER_TRANSITION, SideEffectKind.QUARANTINE}]
    assert audited
    assert all(entry.actor and entry.actor.strip() for entry in audited)
    assert all(entry.payload.get("actor", "local-operator") for entry in audited)

    client.get("/api/personalization")
    client.get("/api/toolbelt")
    combined = "\n".join(client.get(endpoint).text for endpoint in ["/api/personalization", "/api/toolbelt", "/api/ecology", "/api/runs", "/api/ledger", "/api/metrics"])
    assert "sk-secret-no-leak" not in combined


def test_fail_closed_live_seams_and_default_fake_flow(monkeypatch):
    for key in ["ULTRON_MODEL_BASE_URL", "ULTRON_MODEL_API_KEY", "ULTRON_MODEL_NAME"]:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(LiveModelUnavailable):
        HttpModelProvider().complete("prompt", "schema")
    with pytest.raises(Exception) as hermes_exc:
        PinnedHermesAdapter(runner=None).run(__import__("tests.test_gap8_redteam", fromlist=["_request"]). _request())
    assert hermes_exc.type.__name__ == "LiveHermesUnavailable"
    assert "Hermes runner" in str(hermes_exc.value)

    def block_toolsets(name, package=None):
        if name == "toolsets":
            raise ImportError(name)
        return __import__(name, fromlist=["*"])

    monkeypatch.setattr("ultron.hermes.runner.importlib.import_module", block_toolsets)
    with pytest.raises(LiveHermesUnavailable):
        SubprocessHermesRunner().run_plan(PinnedHermesAdapter().build_invocation_plan(__import__("tests.test_gap8_redteam", fromlist=["_request"]). _request()), "/tmp/story6-hermes")

    monkeypatch.setenv("ULTRON_UI_GENERATOR", "model")
    client = _client()
    response = client.get("/api/uispec")
    assert response.status_code == 503

    monkeypatch.delenv("ULTRON_UI_GENERATOR", raising=False)
    monkeypatch.delenv("ULTRON_ADAPTER", raising=False)
    fresh = _client()
    csrf = _csrf(fresh)
    submit = _action(fresh, csrf, "SUBMIT_REQUEST", {"request_text": "default fake green"})
    assert submit.status_code == 200, submit.text
    bench = _action(fresh, csrf, "RUN_BENCHMARK", {"candidate_hash": submit.json()["candidate"]["content_hash"], "canary_id": submit.json()["canary_id"]})
    assert bench.status_code == 200, bench.text
