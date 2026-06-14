import hashlib
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
    sentinel = "story6rawsentinel629d0d88XYZabcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    truncated_prefix = sentinel[:80]
    request = f"Please triage authentication dashboard benchmark {sentinel} ghp_story6SECRETtoken1234567890 sk-story6SECRETtoken1234567890 story6-secret@example.com 0123456789abcdef0123456789abcdef01234567"
    submit = _action(client, csrf, "SUBMIT_REQUEST", {"request_text": request})
    assert submit.status_code == 200, submit.text
    submit_body = submit.json()
    assert set(submit_body) == {"ok", "run_id", "candidate_hash", "canary_id", "active_pointer_version", "envelope"}
    assert len(submit_body["candidate_hash"]) <= 12
    run_id = submit_body["envelope"]["run_id"]
    feedback = _action(client, csrf, "GIVE_FEEDBACK", {"run_id": run_id, "rating": -1, "comment": "story6 feedback raw sentinel ghp_story6SECRETtoken1234567890"})
    assert feedback.status_code == 200, feedback.text
    assert set(feedback.json()) == {"ok", "run_id", "status"}
    permission_reason = f"Permission expansion path {sentinel} ghp_story6SECRETtoken1234567890 sk-story6SECRETtoken1234567890 story6-secret@example.com"
    permission = _action(
        client,
        csrf,
        "REQUEST_PERMISSION_EXPANSION",
        {"tool": "shell", "scope": "story6-secret", "reason": permission_reason},
        client.app.state.triage.current_pointer_version(),
    )
    assert permission.status_code == 200, permission.text

    surfaces = {"SUBMIT_REQUEST": submit_body, "GIVE_FEEDBACK": feedback.json(), "InlineGenUiEnvelope": submit_body["envelope"]}
    for endpoint in ["/api/personalization", "/api/toolbelt", "/api/ecology", "/api/runs", "/api/ledger", "/api/metrics"]:
        response = client.get(endpoint)
        assert response.status_code == 200
        surfaces[endpoint] = response.json()
    combined = _dump(surfaces)
    for secret in [*SECRET_VALUES, sentinel, truncated_prefix, request]:
        assert secret not in combined
    assert "[redacted]" in combined or "redacted" in combined.lower()

def test_malformed_action_validation_does_not_echo_secret_bearing_input():
    client = _client()
    csrf = _csrf(client)
    sentinel = "story6-malformed-validation-sentinel-1b7f3e2c"
    gh_secret = "ghp_malformedStory6SECRETtoken1234567890"
    sk_secret = "sk-malformedStory6SECRETtoken1234567890"
    response = client.post(
        "/api/action",
        headers={"X-CSRF-Token": csrf},
        json={
            "type": "SUBMIT_REQUEST",
            "payload": {"request_text": f"{sentinel} {gh_secret} {sk_secret}"},
            "csrf_token": csrf,
            "unexpected_extra_field": f"{sentinel} {gh_secret} {sk_secret}",
        },
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert set(body) == {"detail"}
    assert body["detail"]
    assert all(set(error) == {"loc", "msg", "type"} for error in body["detail"])
    combined = _dump(body)
    for raw in [sentinel, gh_secret, sk_secret]:
        assert raw not in combined


def test_malformed_action_validation_does_not_echo_secret_bearing_extra_field_name():
    client = _client()
    csrf = _csrf(client)
    secret_field = "ghp_EXTRAfieldStory6SECRETtoken1234567890"
    response = client.post(
        "/api/action",
        headers={"X-CSRF-Token": csrf},
        json={
            "type": "SUBMIT_REQUEST",
            "payload": {"request_text": "safe request"},
            "csrf_token": csrf,
            secret_field: 1,
        },
    )
    assert response.status_code == 422, response.text
    combined = _dump(response.json())
    assert secret_field not in combined
    assert "EXTRAfieldStory6SECRET" not in combined


def test_action_success_responses_do_not_echo_payload_supplied_ids():
    client = _client()
    csrf = _csrf(client)
    engine = client.app.state.triage
    submit = _action(client, csrf, "SUBMIT_REQUEST", {"request_text": "story6 id reflection audit"})
    assert submit.status_code == 200, submit.text
    candidate_hash = engine.last_candidate_hash

    raw_run_id = "story6-run-id-sentinel ghp_RUNIDStory6SECRETtoken1234567890"
    feedback = _action(client, csrf, "GIVE_FEEDBACK", {"run_id": raw_run_id, "rating": 1, "comment": "ok"})
    assert feedback.status_code == 200, feedback.text
    feedback_body = _dump(feedback.json())
    assert raw_run_id not in feedback_body
    assert "ghp_RUNIDStory6SECRETtoken1234567890" not in feedback_body

    raw_canary_id = "story6-canary-id-sentinel ghp_CANARYStory6SECRETtoken1234567890"
    benchmark = _action(client, csrf, "RUN_BENCHMARK", {"candidate_hash": candidate_hash, "canary_id": raw_canary_id}, engine.current_pointer_version())
    assert benchmark.status_code == 200, benchmark.text
    benchmark_body = _dump(benchmark.json())
    assert raw_canary_id not in benchmark_body
    assert "ghp_CANARYStory6SECRETtoken1234567890" not in benchmark_body
    assert benchmark.json()["canary_id"] == hashlib.sha256(raw_canary_id.encode()).hexdigest()[:12]
    assert not benchmark.json()["canary_id"].startswith("ghp_")


def test_live_unavailable_503_bodies_are_generic():
    client = _client()

    @client.app.get("/story6-live-hermes")
    def story6_live_hermes():
        raise LiveHermesUnavailable("internal hermes secret ghp_HERMESStory6SECRETtoken1234567890")

    @client.app.get("/story6-live-model")
    def story6_live_model():
        raise LiveModelUnavailable("internal model secret sk-MODELStory6SECRETtoken1234567890")

    hermes = client.get("/story6-live-hermes")
    assert hermes.status_code == 503
    assert hermes.json() == {"detail": "live Hermes unavailable"}
    assert "ghp_HERMESStory6SECRETtoken1234567890" not in hermes.text

    model = client.get("/story6-live-model")
    assert model.status_code == 503
    assert model.json() == {"detail": "live model unavailable"}
    assert "sk-MODELStory6SECRETtoken1234567890" not in model.text

def test_redaction_preserves_common_words_and_blocks_truncated_prefixes():
    from ultron.app.triage import _redact

    request = "authentication dashboard benchmark sentinelZZZ1234567890abcdefghijklmnopqrstuvwx"
    sentinel_prefix = "sentinelZZZ1234567890abcdefghijklmnopqrstuvwx"[:32]
    leaked = f"authentication dashboard benchmark {sentinel_prefix} sk-story6SECRETtoken1234567890"
    redacted = _redact(leaked, request)
    assert "authentication" in redacted
    assert "dashboard" in redacted
    assert "benchmark" in redacted
    assert sentinel_prefix not in redacted
    assert "sk-story6SECRETtoken1234567890" not in redacted


def test_permission_request_reason_redacted_from_ledger_safety_surface():
    client = _client()
    csrf = _csrf(client)
    engine = client.app.state.triage
    sentinel = "permission-raw-sentinel-story6-blocker"
    secret = "ghp_permissionSECRETtoken1234567890"
    reason = f"Need shell for {sentinel} {secret} story6-permission@example.com sk-permissionSECRETtoken1234567890"
    permission = _action(
        client,
        csrf,
        "REQUEST_PERMISSION_EXPANSION",
        {"tool": "shell", "scope": "story6-permission", "reason": reason},
        engine.current_pointer_version(),
    )
    assert permission.status_code == 200, permission.text
    short_id = permission.json()["request_id"]

    endpoints = ["/api/ledger"]
    if any(getattr(route, "path", None) == "/api/safety" for route in client.app.routes):
        endpoints.append("/api/safety")
    surfaces = {}
    for endpoint in endpoints:
        response = client.get(endpoint)
        assert response.status_code == 200
        surfaces[endpoint] = response.json()

    combined = _dump(surfaces)
    for raw in [sentinel, secret, reason, "story6-permission@example.com", "sk-permissionSECRETtoken1234567890"]:
        assert raw not in combined
    assert short_id == surfaces["/api/ledger"]["safety"]["pending_permission_expansions"][0]["request_id"]
    assert "pending_human_approval" in combined
    assert "reason_summary" in combined
    assert "tool_summary" in combined


def test_action_responses_are_status_and_short_ids_only():
    client = _client()
    csrf = _csrf(client)
    engine = client.app.state.triage
    submit = _action(client, csrf, "SUBMIT_REQUEST", {"request_text": "story6 benchmark response audit"})
    assert submit.status_code == 200, submit.text
    body = submit.json()
    candidate_hash = engine.last_candidate_hash
    canary_id = body["canary_id"]

    benchmark = _action(client, csrf, "RUN_BENCHMARK", {"candidate_hash": candidate_hash, "canary_id": canary_id}, engine.current_pointer_version())
    assert benchmark.status_code == 200, benchmark.text
    assert set(benchmark.json()) == {"ok", "candidate_hash", "canary_id", "status"}
    assert benchmark.json()["canary_id"] == hashlib.sha256(canary_id.encode()).hexdigest()[:12]

    approve = _action(client, csrf, "APPROVE_PROMOTION", {"candidate_hash": candidate_hash}, engine.current_pointer_version())
    assert approve.status_code == 200, approve.text
    assert set(approve.json()) == {"ok", "candidate_hash", "promoted", "active_pointer_version", "status"}

    rollback = _action(client, csrf, "ROLLBACK_CANARY", {"canary_id": canary_id}, engine.current_pointer_version())
    assert rollback.status_code == 200, rollback.text
    assert set(rollback.json()) == {"ok", "canary_id", "status"}
    assert rollback.json()["canary_id"] == hashlib.sha256(canary_id.encode()).hexdigest()[:12]

    engine.registry.set_lifecycle(candidate_hash, ModuleLifecycle.PRUNED)
    restore = _action(client, csrf, "RESTORE_MODULE", {"module_hash": candidate_hash}, engine.current_pointer_version())
    assert restore.status_code == 200, restore.text
    assert set(restore.json()) == {"ok", "module_hash", "restored", "status"}

    permission = _action(client, csrf, "REQUEST_PERMISSION_EXPANSION", {"scope": "story6-secret", "reason": "ghp_story6SECRETtoken1234567890"}, engine.current_pointer_version())
    assert permission.status_code == 200, permission.text
    assert set(permission.json()) == {"ok", "request_id", "status"}

    combined = _dump([benchmark.json(), approve.json(), rollback.json(), restore.json(), permission.json()])
    assert "story6-secret" not in combined
    assert "ghp_story6SECRETtoken1234567890" not in combined
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
    chat_css = (STATIC / "chat.css").read_text()
    for class_name, animation in re.findall(r"\.(anim-[\w-]+)\s*\{\s*animation:\s*([^;}]+)", chat_css):
        duration = re.search(r"(\d+(?:\.\d+)?)(ms|s)\b", animation)
        assert duration, class_name
        milliseconds = float(duration.group(1)) * (1000 if duration.group(2) == "s" else 1)
        assert milliseconds <= 1200, f"{class_name} is {milliseconds}ms"
    reduced_motion = re.search(r"@media \(prefers-reduced-motion: reduce\) \{(?P<body>.*)\}\s*$", chat_css, re.DOTALL)
    assert reduced_motion
    reduced_body = reduced_motion.group("body")
    assert "animation-duration: 1ms !important" in reduced_body
    assert "body::before { animation: none; }" in reduced_body
    assert ".ultron-orb, .ultron-orb::before, .ultron-orb::after { animation: none; }" in reduced_body


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
    candidate_hash = engine.last_candidate_hash
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
    assert engine.has_promotable_evidence(candidate_hash)
    approve = _action(client, csrf, "APPROVE_PROMOTION", {"candidate_hash": candidate_hash})
    assert approve.status_code == 200, approve.text

    rollback_canary = engine.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": "story6 rollback"}, actor="local-operator")
    rollback = _action(client, csrf, "ROLLBACK_CANARY", {"canary_id": rollback_canary["canary_id"]})
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["status"] == "rollback_complete"
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
    bench = _action(fresh, csrf, "RUN_BENCHMARK", {"candidate_hash": fresh.app.state.triage.last_candidate_hash, "canary_id": submit.json()["canary_id"]})
    assert bench.status_code == 200, bench.text
