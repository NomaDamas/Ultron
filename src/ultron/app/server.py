"""FastAPI server for the G007 triage MVP."""

from __future__ import annotations

import hashlib
import os
import time
import secrets
from pathlib import Path
from typing import Any

from fastapi import Cookie, FastAPI, Header, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ultron.app.triage import DEFAULT_SCOPE, DEFAULT_WORKFLOW, PolicyDenied, _redacted_scalar, build_triage_app_from_env
from ultron.auth.principal import DEFAULT_LOCAL_PRINCIPAL, Scope, SessionStore
from ultron.config import ModelSettingsWrite, build_config_service
from ultron.evolution.variation import VariationPrimitive
from ultron.ui.runtime import ActionCommand, ActionType, validate_action
from ultron.hermes.adapter import LiveHermesUnavailable
from ultron.ui.generator import LiveModelUnavailable

CSP = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
STATIC_DIR = Path(__file__).with_name("static")
SESSION_TTL_SECONDS = 60 * 60
ACTION_SCOPES = {
    ActionType.APPROVE_PROMOTION: Scope.APPROVE_PROMOTION,
    ActionType.ROLLBACK_CANARY: Scope.ROLLBACK,
    ActionType.RESTORE_MODULE: Scope.RESTORE,
    ActionType.REQUEST_PERMISSION_EXPANSION: Scope.REQUEST_PERMISSION_EXPANSION,
    ActionType.RUN_BENCHMARK: Scope.RUN_BENCHMARK,
}
MUTATING_USER_ACTIONS = {ActionType.SUBMIT_REQUEST, ActionType.GIVE_FEEDBACK}




def create_app() -> FastAPI:
    config_service = build_config_service()
    engine = build_triage_app_from_env(config_service)
    engine.seed_baseline()
    csrf_tokens: dict[str, str] = {}
    session_store = SessionStore(secure_cookies=os.getenv("ULTRON_SECURE_COOKIES", "0") == "1")
    app = FastAPI(title="Ultron Triage MVP")
    app.state.triage = engine
    app.state.session_store = session_store
    app.state.config = config_service
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Any, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": _safe_validation_errors(exc)})

    @app.exception_handler(LiveHermesUnavailable)
    async def live_hermes_unavailable_handler(request: Any, exc: LiveHermesUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "live Hermes unavailable"})

    @app.exception_handler(LiveModelUnavailable)
    async def live_model_unavailable_handler(request: Any, exc: LiveModelUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "live model unavailable"})

    @app.get("/", response_class=HTMLResponse)
    def index(response: Response) -> str:
        session_token = session_store.create_session(DEFAULT_LOCAL_PRINCIPAL, SESSION_TTL_SECONDS)
        csrf_token = secrets.token_urlsafe(24)
        csrf_tokens[session_token] = csrf_token
        response.headers["Content-Security-Policy"] = CSP
        response.set_cookie("ultron_session", session_token, **session_store.cookie_attributes(httponly=True))
        response.set_cookie("ultron_csrf", csrf_token, httponly=False, samesite="strict", secure=session_store.secure_cookies)
        return """<!doctype html>
<html lang=\"en\">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Ultron Chat</title><link rel="stylesheet" href="/static/chat.css"></head>
<body>
  <main id=\"app\" data-csrf-cookie=\"ultron_csrf\">Loading Ultron chat...</main>
  <script src=\"/static/chat.js\"></script>
</body>
</html>"""

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(response: Response) -> str:
        session_token = session_store.create_session(DEFAULT_LOCAL_PRINCIPAL, SESSION_TTL_SECONDS)
        csrf_token = secrets.token_urlsafe(24)
        csrf_tokens[session_token] = csrf_token
        response.headers["Content-Security-Policy"] = CSP
        response.set_cookie("ultron_session", session_token, **session_store.cookie_attributes(httponly=True))
        response.set_cookie("ultron_csrf", csrf_token, httponly=False, samesite="strict", secure=session_store.secure_cookies)
        return """<!doctype html>
<html lang=\"en\">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Ultron Dashboard</title><link rel="stylesheet" href="/static/dashboard.css"></head>
<body>
  <main id=\"app\" data-csrf-cookie=\"ultron_csrf\">Loading Ultron dashboard...</main>
  <script src=\"/static/dashboard.js\"></script>
</body>
</html>"""

    @app.get("/api/uispec")
    def uispec(response: Response) -> dict[str, Any]:
        response.headers["Content-Security-Policy"] = CSP
        return engine.current_uispec().model_dump(mode="json")

    @app.get("/api/metrics")
    def metrics(response: Response) -> dict[str, Any]:
        response.headers["Content-Security-Policy"] = CSP
        return engine.telemetry.snapshot()

    @app.get("/api/toolbelt")
    def toolbelt(response: Response) -> dict[str, Any]:
        response.headers["Content-Security-Policy"] = CSP
        return {"modules": engine.active_modules(), "active_pointer_version": engine.current_pointer_version()}

    @app.get("/api/ecology")
    def ecology(response: Response) -> dict[str, Any]:
        response.headers["Content-Security-Policy"] = CSP
        return {"modules_by_lifecycle": engine.modules_by_lifecycle(), "active_pointer_version": engine.current_pointer_version(), "lineage": engine.lineage_view()}

    @app.get("/api/runs")
    def runs(response: Response, limit: int = 20) -> dict[str, Any]:
        response.headers["Content-Security-Policy"] = CSP
        return {"runs": engine.recent_runs(limit)}

    @app.get("/api/ledger")
    def ledger(response: Response, limit: int = 20) -> dict[str, Any]:
        response.headers["Content-Security-Policy"] = CSP
        return {"entries": engine.recent_ledger(limit), "safety": engine.safety_status()}

    @app.get("/api/personalization")
    def personalization(response: Response) -> dict[str, Any]:
        response.headers["Content-Security-Policy"] = CSP
        return engine.personalization_observability(DEFAULT_SCOPE, DEFAULT_WORKFLOW)

    @app.get("/api/settings/model")
    def get_model_settings(response: Response) -> dict[str, Any]:
        response.headers["Content-Security-Policy"] = CSP
        return config_service.model_settings_read().model_dump(mode="json")

    @app.post("/api/settings/model")
    def post_model_settings(
        settings: ModelSettingsWrite,
        response: Response,
        ultron_session: str | None = Cookie(default=None),
        x_csrf_token: str | None = Header(default=None),
        x_csrf: str | None = Header(default=None),
    ) -> dict[str, Any]:
        response.headers["Content-Security-Policy"] = CSP
        principal = session_store.resolve(ultron_session, time.time())
        if principal is None:
            engine.telemetry.increment("auth_failures", event="missing_or_expired_session")
            raise HTTPException(status_code=401, detail="settings update requires authenticated session")
        if not principal.has_scope(Scope.MANAGE_SETTINGS):
            engine.telemetry.increment("auth_failures", event="missing_scope", subject=principal.subject)
            raise HTTPException(status_code=403, detail=f"settings update requires scope {Scope.MANAGE_SETTINGS.value}")
        provided_csrf = x_csrf_token or x_csrf
        expected = csrf_tokens.get(ultron_session or "")
        if not expected or provided_csrf != expected:
            engine.telemetry.increment("auth_failures", event="invalid_request", subject=principal.subject)
            raise HTTPException(status_code=403, detail="settings update requires a valid CSRF token")
        try:
            read = config_service.apply_write(settings, actor=principal.subject)
        except KeyError as exc:
            raise HTTPException(status_code=422, detail="unknown settings field") from exc
        return read.model_dump(mode="json")

    @app.post("/api/action")
    def action(
        cmd: ActionCommand,
        response: Response,
        ultron_session: str | None = Cookie(default=None),
        x_csrf_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        response.headers["Content-Security-Policy"] = CSP
        principal = session_store.resolve(ultron_session, time.time())
        authed = principal is not None
        csrf_ok = authed and cmd.csrf_token is not None and csrf_tokens.get(ultron_session or "") == cmd.csrf_token and x_csrf_token == cmd.csrf_token
        required_scope = ACTION_SCOPES.get(cmd.type)
        is_mutating = cmd.type in MUTATING_USER_ACTIONS or required_scope is not None
        if is_mutating:
            if principal is None:
                engine.telemetry.increment("auth_failures", event="missing_or_expired_session")
                raise HTTPException(status_code=401, detail="mutating action requires authenticated session")
            if required_scope is not None and not principal.has_scope(required_scope):
                engine.telemetry.increment("auth_failures", event="missing_scope", subject=principal.subject)
                raise HTTPException(status_code=403, detail=f"privileged action requires scope {required_scope.value}")
            if not csrf_ok:
                engine.telemetry.increment("auth_failures", event="invalid_request", subject=principal.subject)
                raise HTTPException(status_code=403, detail="mutating action requires a valid CSRF token")
        policy_ok = _policy_ok(engine, cmd)
        try:
            validate_action(
                cmd,
                session_authed=authed,
                csrf_ok=bool(csrf_ok),
                current_pointer_version=engine.current_pointer_version(),
                policy_ok=policy_ok,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=_safe_validation_errors(exc)) from exc
        except (LiveHermesUnavailable, LiveModelUnavailable) as exc:
            raise HTTPException(status_code=503, detail=_generic_live_unavailable_detail(exc)) from exc

        if cmd.type is ActionType.SUBMIT_REQUEST:
            request_text = str(cmd.payload.get("request_text", ""))
            try:
                result = engine.start_run(DEFAULT_SCOPE, DEFAULT_WORKFLOW, request_text, actor=principal.subject if principal else None)
                canary = engine.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": f"submit request: {request_text}"}, request_text, actor=principal.subject if principal else None)
            except (LiveHermesUnavailable, LiveModelUnavailable) as exc:
                raise HTTPException(status_code=503, detail=_generic_live_unavailable_detail(exc)) from exc
            envelope = engine.build_inline_genui_envelope(result, canary)
            return _jsonable(
                {
                    "ok": True,
                    "run_id": envelope.run_id,
                    "candidate_hash": _short_hash(canary["candidate"].content_hash),
                    "canary_id": canary["canary_id"],
                    "active_pointer_version": engine.current_pointer_version(),
                    "envelope": envelope,
                }
            )
        if cmd.type is ActionType.RUN_BENCHMARK:
            candidate_hash = str(cmd.payload.get("candidate_hash") or engine.last_candidate_hash or "")
            if not candidate_hash:
                raise HTTPException(status_code=403, detail="candidate benchmark requires a candidate")
            try:
                evaluation = engine.benchmark_and_decide(candidate_hash, canary_id=str(cmd.payload.get("canary_id") or engine.last_canary_id or ""), actor=principal.subject if principal else None)
            except (LiveHermesUnavailable, LiveModelUnavailable) as exc:
                raise HTTPException(status_code=503, detail=_generic_live_unavailable_detail(exc)) from exc
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=403, detail=_safe_validation_errors(exc)) from exc
            return _jsonable({"ok": True, "candidate_hash": _short_hash(candidate_hash), "canary_id": _short_hash(str(evaluation.get("canary_id") or engine.last_canary_id or "")), "status": "benchmark_complete"})
        if cmd.type is ActionType.GIVE_FEEDBACK:
            server_run_id = engine.last_manifest.run_id if engine.last_manifest else "run"
            event = engine.submit_feedback(server_run_id, int(cmd.payload.get("rating", 1)), str(cmd.payload.get("comment", "")), actor=principal.subject if principal else None)
            return _jsonable({"ok": True, "run_id": _short_hash(event.run_id), "status": event.event_type.value})
        if cmd.type is ActionType.APPROVE_PROMOTION:
            candidate_hash = str(cmd.payload.get("candidate_hash") or "")
            try:
                decision = engine.approve_promotion(candidate_hash, cmd.active_pointer_version or -1, actor=principal.subject if principal else None)
            except PolicyDenied as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=403, detail=_safe_validation_errors(exc)) from exc
            return _jsonable({"ok": True, "candidate_hash": _short_hash(candidate_hash), "promoted": bool(decision.get("promoted")), "active_pointer_version": engine.current_pointer_version(), "status": "promotion_decided"})
        if cmd.type is ActionType.ROLLBACK_CANARY:
            canary_id = str(cmd.payload.get("canary_id") or engine.last_canary_id or "")
            if not canary_id:
                raise HTTPException(status_code=403, detail="canary rejected by policy")
            report = engine.rollback_controller.rollback(canary_id, actor=principal.subject if principal else None)
            engine.telemetry.increment("rollbacks", event="rollback", subject=principal.subject if principal else None)
            return _jsonable({"ok": True, "canary_id": _short_hash(report.canary_id), "status": "rollback_complete"})
        if cmd.type is ActionType.RESTORE_MODULE:
            restored = engine.atrophy_and_restore(str(cmd.payload.get("module_hash") or "") or None, actor=principal.subject if principal else None)
            return _jsonable({"ok": True, "module_hash": _short_hash(str(restored.get("module_hash") or "")), "restored": bool(restored.get("restored")), "status": "restore_complete"})
        if cmd.type is ActionType.REQUEST_PERMISSION_EXPANSION:
            request = engine.record_permission_expansion_request(cmd.payload, actor=principal.subject if principal else None)
            return _jsonable({"ok": True, "request_id": _short_hash(str(request.get("request_id") or request.get("id") or "")), "status": request.get("status")})
        raise HTTPException(status_code=403, detail="unsupported privileged action")

    return app


def _policy_ok(engine: TriageApp, cmd: ActionCommand) -> bool:
    # MVP auth is a single-user session cookie; multi-tenant auth is explicit non-scope.
    # The policy and evidence gates below are product-real and independent of that MVP auth boundary.
    if cmd.type is ActionType.APPROVE_PROMOTION:
        candidate_hash = str(cmd.payload.get("candidate_hash") or "")
        try:
            return engine.has_promotable_evidence(candidate_hash)
        except KeyError:
            return False
    if cmd.type is ActionType.ROLLBACK_CANARY:
        return engine.canary_active(str(cmd.payload.get("canary_id") or ""))
    if cmd.type is ActionType.RESTORE_MODULE:
        return engine.module_is_pruned(str(cmd.payload.get("module_hash") or ""))
    if cmd.type is ActionType.REQUEST_PERMISSION_EXPANSION:
        return True
    return True


def _safe_validation_errors(exc: ValidationError | RequestValidationError | ValueError) -> list[dict[str, Any]] | str:
    if isinstance(exc, (ValidationError, RequestValidationError)):
        return [
            {
                "loc": _safe_validation_loc(error.get("loc", ())),
                "msg": str(error.get("msg", "invalid action request")),
                "type": str(error.get("type", "value_error")),
            }
            for error in exc.errors()
        ]
    return "invalid action request"


_KNOWN_ACTION_LOC_SEGMENTS = frozenset(ActionCommand.model_fields)


def _safe_validation_loc(loc: Any) -> list[str | int]:
    sanitized: list[str | int] = []
    for index, segment in enumerate(loc if isinstance(loc, (list, tuple)) else (loc,)):
        if isinstance(segment, int):
            sanitized.append(segment)
        elif isinstance(segment, str) and index == 0 and segment in {"body", "query", "path", "header", "cookie"}:
            sanitized.append(segment)
        elif isinstance(segment, str) and segment in _KNOWN_ACTION_LOC_SEGMENTS:
            sanitized.append(segment)
        elif isinstance(segment, str):
            sanitized.append(_redacted_scalar(segment, max_length=32) if segment != _redacted_scalar(segment, max_length=32) else "<field>")
        else:
            sanitized.append("<field>")
    return sanitized


def _generic_live_unavailable_detail(exc: Exception) -> str:
    if isinstance(exc, LiveHermesUnavailable):
        return "live Hermes unavailable"
    return "live model unavailable"


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _short_hash(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest()[:12] if value else None


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("ultron.app.server:create_app", host="127.0.0.1", port=8717, factory=True)
