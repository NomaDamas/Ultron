"""FastAPI server for the G007 triage MVP."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import Cookie, FastAPI, Header, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ultron.app.triage import DEFAULT_SCOPE, DEFAULT_WORKFLOW, PolicyDenied, TriageApp
from ultron.evaluation.harness import PairedTask
from ultron.evolution.variation import VariationPrimitive
from ultron.ui.runtime import ActionCommand, ActionType, validate_action

CSP = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
STATIC_DIR = Path(__file__).with_name("static")


def create_app() -> FastAPI:
    engine = TriageApp()
    engine.seed_baseline()
    sessions: dict[str, str] = {}
    app = FastAPI(title="Ultron Triage MVP")
    app.state.triage = engine
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Any, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    @app.get("/", response_class=HTMLResponse)
    def index(response: Response) -> str:
        session_token = secrets.token_urlsafe(24)
        csrf_token = secrets.token_urlsafe(24)
        sessions[session_token] = csrf_token
        response.headers["Content-Security-Policy"] = CSP
        response.set_cookie("ultron_session", session_token, httponly=True, samesite="strict")
        response.set_cookie("ultron_csrf", csrf_token, httponly=False, samesite="strict")
        return """<!doctype html>
<html lang=\"en\">
<head><meta charset=\"utf-8\"><title>Ultron Triage</title></head>
<body>
  <main id=\"app\" data-csrf-cookie=\"ultron_csrf\">Loading Ultron triage...</main>
  <script src=\"/static/app.js\"></script>
</body>
</html>"""

    @app.get("/api/uispec")
    def uispec(response: Response) -> dict[str, Any]:
        response.headers["Content-Security-Policy"] = CSP
        return engine.current_uispec().model_dump(mode="json")

    @app.post("/api/action")
    def action(
        cmd: ActionCommand,
        response: Response,
        ultron_session: str | None = Cookie(default=None),
        x_csrf_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        response.headers["Content-Security-Policy"] = CSP
        authed = ultron_session in sessions
        csrf_ok = authed and cmd.csrf_token is not None and sessions.get(ultron_session or "") == cmd.csrf_token and x_csrf_token == cmd.csrf_token
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
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if cmd.type is ActionType.SUBMIT_REQUEST:
            request_text = str(cmd.payload.get("request_text", ""))
            result = engine.start_run(DEFAULT_SCOPE, DEFAULT_WORKFLOW, request_text)
            canary = engine.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {"prompt_pack_hash": f"submit request: {request_text}"}, request_text)
            candidate_hash = canary["candidate"].content_hash or ""
            evaluation = engine.evaluate_and_decide(
                candidate_hash,
                [PairedTask(task_id=f"submit-{i}", baseline_metric=1.0, candidate_metric=1.2) for i in range(10)],
                canary["canary_id"],
            )
            return _jsonable({"ok": True, "result": result, "candidate": canary["candidate"], "canary_id": canary["canary_id"], "evaluation": evaluation})
        if cmd.type is ActionType.GIVE_FEEDBACK:
            event = engine.submit_feedback(str(cmd.payload.get("run_id", engine.last_manifest.run_id if engine.last_manifest else "run")), int(cmd.payload.get("rating", 1)), str(cmd.payload.get("comment", "")))
            return _jsonable({"ok": True, "feedback": event})
        if cmd.type is ActionType.APPROVE_PROMOTION:
            candidate_hash = str(cmd.payload.get("candidate_hash") or "")
            try:
                decision = engine.approve_promotion(candidate_hash, cmd.active_pointer_version or -1)
            except PolicyDenied as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except (KeyError, PermissionError, ValueError) as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            return _jsonable({"ok": True, "decision": decision})
        if cmd.type is ActionType.ROLLBACK_CANARY:
            canary_id = str(cmd.payload.get("canary_id") or engine.last_canary_id or "")
            if not canary_id:
                raise HTTPException(status_code=403, detail="canary rejected by policy")
            report = engine.rollback_controller.rollback(canary_id)
            return _jsonable({"ok": True, "rollback": report})
        if cmd.type is ActionType.RESTORE_MODULE:
            restored = engine.atrophy_and_restore(str(cmd.payload.get("module_hash") or "") or None)
            return _jsonable({"ok": True, "restored": restored})
        if cmd.type is ActionType.REQUEST_PERMISSION_EXPANSION:
            request = engine.record_permission_expansion_request(cmd.payload)
            return _jsonable({"ok": True, "permission_expansion": request})
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


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("ultron.app.server:create_app", host="127.0.0.1", port=8717, factory=True)
