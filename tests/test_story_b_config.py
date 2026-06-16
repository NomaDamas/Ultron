"""Story B: .env, out-of-repo secret store, settings API, and committed config CLI."""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest

from ultron.config import (
    ALL_KEYS,
    ConfigService,
    ModelSettingsWrite,
    is_secret_key,
    load_dotenv,
    parse_dotenv,
)
from ultron.config.__main__ import main as config_cli
from ultron.config.secrets import SecretStore, default_store_path

try:
    from fastapi.testclient import TestClient

    from ultron.app.server import create_app
except Exception:  # pragma: no cover
    TestClient = None  # type: ignore

CANARY_SECRET = "sk-canary-SECRET-9999"


# ---------------------------------------------------------------------------
# Secret store
# ---------------------------------------------------------------------------


def test_secret_store_roundtrip_and_owner_only(tmp_path):
    store = SecretStore(tmp_path / "secrets.json")
    store.set_value("llm.api_key", CANARY_SECRET)
    assert store.get_value("llm.api_key") == CANARY_SECRET
    mode = stat.S_IMODE((tmp_path / "secrets.json").stat().st_mode)
    assert mode == 0o600
    assert store.get_updated_at("llm.api_key") is not None


def test_default_store_path_uses_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    assert default_store_path() == tmp_path / "cfg" / "secrets.json"


# ---------------------------------------------------------------------------
# dotenv + precedence
# ---------------------------------------------------------------------------


def test_parse_dotenv_handles_quotes_and_comments():
    parsed = parse_dotenv('# c\nexport ULTRON_LLM_MODEL="gpt-x"\nULTRON_LLM_BASE_URL=https://h/v1\n')
    assert parsed["ULTRON_LLM_MODEL"] == "gpt-x"
    assert parsed["ULTRON_LLM_BASE_URL"] == "https://h/v1"


def test_load_dotenv_does_not_override_process_env(tmp_path):
    env = {"ULTRON_LLM_MODEL": "from-env"}
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("ULTRON_LLM_MODEL=from-dotenv\nULTRON_VLM_MODEL=vlm-dotenv\n")
    parsed = load_dotenv(dotenv_file, environ=env)
    assert env["ULTRON_LLM_MODEL"] == "from-env"  # process env wins
    assert env["ULTRON_VLM_MODEL"] == "vlm-dotenv"  # dotenv fills the gap
    assert parsed["ULTRON_LLM_MODEL"] == "from-dotenv"


def test_resolution_precedence_store_over_env_over_dotenv(tmp_path):
    store = SecretStore(tmp_path / "secrets.json")
    store.set_value("llm.model", "from-store")
    svc = ConfigService(
        store=store,
        environ={"ULTRON_LLM_MODEL": "from-env"},
        dotenv={"ULTRON_LLM_MODEL": "from-dotenv"},
    )
    assert svc.resolve("llm.model") == ("from-store", "secret_store")

    svc2 = ConfigService(store=SecretStore(tmp_path / "empty.json"), environ={"ULTRON_LLM_MODEL": "from-env"}, dotenv={"ULTRON_LLM_MODEL": "from-dotenv"})
    assert svc2.resolve("llm.model") == ("from-env", "env")

    svc3 = ConfigService(store=SecretStore(tmp_path / "empty2.json"), environ={}, dotenv={"ULTRON_LLM_MODEL": "from-dotenv"})
    assert svc3.resolve("llm.model") == ("from-dotenv", "dotenv")


def test_model_settings_read_is_redacted(tmp_path):
    store = SecretStore(tmp_path / "secrets.json")
    store.set_value("llm.api_key", CANARY_SECRET)
    store.set_value("llm.base_url", "https://api.example.com/v1")
    store.set_value("llm.model", "gpt-x")
    svc = ConfigService(store=store, environ={}, dotenv={})
    read = svc.model_settings_read()
    blob = json.dumps(read.model_dump(mode="json"))
    assert CANARY_SECRET not in blob
    assert read.llm_api_key.configured is True
    assert read.llm_api_key.last4 == "9999"
    assert read.llm_base_url_label == "api.example.com"
    assert "api.example.com/v1" not in blob  # only the host label, not the full url path
    assert read.llm_configured is True


def test_apply_write_audit_has_no_raw_secret(tmp_path):
    store = SecretStore(tmp_path / "secrets.json")
    svc = ConfigService(store=store, environ={}, dotenv={})
    svc.apply_write(ModelSettingsWrite(llm_api_key=CANARY_SECRET, llm_model="gpt-x"), actor="tester")
    audit_blob = json.dumps(svc.audit)
    assert CANARY_SECRET not in audit_blob
    assert svc.audit[0]["actor"] == "tester"
    assert "llm.api_key" in svc.audit[0]["fields"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _svc(tmp_path) -> ConfigService:
    return ConfigService(store=SecretStore(tmp_path / "secrets.json"), environ={}, dotenv={})


def test_cli_status_redacted(tmp_path):
    svc = _svc(tmp_path)
    svc.set("llm.api_key", CANARY_SECRET)
    out = io.StringIO()
    rc = config_cli(["status"], service=svc, out=out)
    assert rc == 0
    assert CANARY_SECRET not in out.getvalue()
    assert '"llm_configured"' in out.getvalue()


def test_cli_set_secret_requires_stdin(tmp_path):
    svc = _svc(tmp_path)
    rc = config_cli(["set", "llm.api_key", CANARY_SECRET], service=svc, out=io.StringIO())
    assert rc == 2  # must use --stdin for secrets
    assert svc.value("llm.api_key") is None


def test_cli_set_secret_via_stdin_no_echo(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(CANARY_SECRET + "\n"))
    out = io.StringIO()
    rc = config_cli(["set", "llm.api_key", "--stdin"], service=svc, out=out)
    assert rc == 0
    assert svc.value("llm.api_key") == CANARY_SECRET
    assert CANARY_SECRET not in out.getvalue()  # confirmation never echoes the secret


def test_cli_set_non_secret_and_get(tmp_path):
    svc = _svc(tmp_path)
    out = io.StringIO()
    assert config_cli(["set", "llm.model", "gpt-x"], service=svc, out=out) == 0
    out2 = io.StringIO()
    assert config_cli(["get", "llm.model"], service=svc, out=out2) == 0
    assert "gpt-x" in out2.getvalue()


def test_cli_get_secret_returns_ref_not_raw(tmp_path):
    svc = _svc(tmp_path)
    svc.set("vlm.api_key", CANARY_SECRET)
    out = io.StringIO()
    assert config_cli(["get", "vlm.api_key"], service=svc, out=out) == 0
    text = out.getvalue()
    assert CANARY_SECRET not in text
    assert '"configured": true' in text


def test_cli_unknown_key(tmp_path):
    svc = _svc(tmp_path)
    assert config_cli(["get", "bogus.key"], service=svc, out=io.StringIO()) == 2


# ---------------------------------------------------------------------------
# Server settings endpoints
# ---------------------------------------------------------------------------


@pytest.mark.skipif(TestClient is None, reason="fastapi test client unavailable")
def test_settings_get_redacted_and_post_requires_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    for key in ["ULTRON_LLM_API_KEY", "ULTRON_MODEL_API_KEY"]:
        monkeypatch.delenv(key, raising=False)
    client = TestClient(create_app())

    # GET is redacted and CSP-guarded
    get_resp = client.get("/api/settings/model")
    assert get_resp.status_code == 200
    assert "default-src 'self'" in get_resp.headers["content-security-policy"]
    assert get_resp.json()["llm_api_key"]["configured"] is False

    # POST without session/CSRF is rejected
    no_auth = TestClient(create_app())
    resp = no_auth.post("/api/settings/model", json={"llm_api_key": CANARY_SECRET})
    assert resp.status_code in (401, 403)


@pytest.mark.skipif(TestClient is None, reason="fastapi test client unavailable")
def test_settings_post_writes_and_never_leaks_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    for key in ["ULTRON_LLM_API_KEY", "ULTRON_MODEL_API_KEY"]:
        monkeypatch.delenv(key, raising=False)
    client = TestClient(create_app())
    csrf = client.get("/dashboard").cookies["ultron_csrf"]
    resp = client.post(
        "/api/settings/model",
        headers={"X-CSRF-Token": csrf},
        json={"llm_api_key": CANARY_SECRET, "llm_model": "gpt-x", "llm_base_url": "https://api.example.com/v1"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert CANARY_SECRET not in body
    assert resp.json()["llm_api_key"]["configured"] is True
    assert resp.json()["llm_configured"] is True

    # GET reflects configured state without the raw secret
    get_resp = client.get("/api/settings/model")
    assert CANARY_SECRET not in get_resp.text
    assert get_resp.json()["llm_api_key"]["last4"] == "9999"

    # Telemetry snapshot never contains the secret
    snapshot = json.dumps(client.app.state.triage.telemetry.snapshot())
    assert CANARY_SECRET not in snapshot

    # The on-disk store is outside the repo working tree
    store_path = tmp_path / "cfg" / "secrets.json"
    assert store_path.exists()
    assert CANARY_SECRET in store_path.read_text()  # raw secret lives only server-side


@pytest.mark.skipif(TestClient is None, reason="fastapi test client unavailable")
def test_settings_post_bad_csrf_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    client = TestClient(create_app())
    client.get("/dashboard")
    resp = client.post("/api/settings/model", headers={"X-CSRF-Token": "wrong"}, json={"llm_model": "x"})
    assert resp.status_code == 403

def test_cli_get_base_url_redacted_to_host(tmp_path):
    svc = _svc(tmp_path)
    svc.set("llm.base_url", "https://user:pw@api.example.com/v1/secret-path?token=abc")
    out = io.StringIO()
    assert config_cli(["get", "llm.base_url"], service=svc, out=out) == 0
    text = out.getvalue()
    assert "api.example.com" in text
    assert "secret-path" not in text
    assert "token=abc" not in text
    assert "user:pw" not in text


def test_cli_set_secret_failure_stderr_no_echo(tmp_path, monkeypatch, capsys):
    svc = _svc(tmp_path)
    # secret without --stdin must fail and never echo any provided value
    rc = config_cli(["set", "llm.api_key", CANARY_SECRET], service=svc, out=io.StringIO())
    captured = capsys.readouterr()
    assert rc == 2
    assert CANARY_SECRET not in captured.out
    assert CANARY_SECRET not in captured.err


def test_build_config_service_reports_dotenv_source(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ULTRON_VLM_MODEL=vlm-from-dotenv\n")
    monkeypatch.delenv("ULTRON_VLM_MODEL", raising=False)
    monkeypatch.setenv("ULTRON_DOTENV_PATH", str(env_file))
    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    from ultron.config import build_config_service

    svc = build_config_service()
    assert svc.resolve("vlm.model") == ("vlm-from-dotenv", "dotenv")
    # os.environ is still populated for legacy consumers
    assert os.environ.get("ULTRON_VLM_MODEL") == "vlm-from-dotenv"


def test_runtime_store_overrides_env_for_provider(tmp_path):
    store = SecretStore(tmp_path / "secrets.json")
    store.set_value("llm.base_url", "https://store.example/v1")
    store.set_value("llm.api_key", "store-key")
    store.set_value("llm.model", "store-model")
    svc = ConfigService(
        store=store,
        environ={"ULTRON_MODEL_BASE_URL": "https://env.example/v1", "ULTRON_MODEL_API_KEY": "env-key", "ULTRON_MODEL_NAME": "env-model"},
        dotenv={},
    )
    cfg = svc.provider_config("llm")
    assert cfg.base_url == "https://store.example/v1"
    assert cfg.api_key == "store-key"
    assert cfg.model_name == "store-model"


@pytest.mark.skipif(TestClient is None, reason="fastapi test client unavailable")
def test_settings_post_unknown_field_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    client = TestClient(create_app())
    csrf = client.get("/dashboard").cookies["ultron_csrf"]
    resp = client.post(
        "/api/settings/model",
        headers={"X-CSRF-Token": csrf},
        json={"llm_model": "x", "evil_field": "sk-canary-SECRET-9999"},
    )
    assert resp.status_code == 422
    assert "sk-canary-SECRET-9999" not in resp.text


@pytest.mark.skipif(TestClient is None, reason="fastapi test client unavailable")
def test_settings_post_requires_manage_settings_scope(monkeypatch, tmp_path):
    from ultron.auth.principal import Principal, Scope

    monkeypatch.setenv("ULTRON_CONFIG_DIR", str(tmp_path / "cfg"))
    app = create_app()
    client = TestClient(app)
    # Mint a session for a principal lacking MANAGE_SETTINGS.
    store = app.state.session_store
    restricted = Principal(subject="restricted", scopes=frozenset({Scope.RUN_BENCHMARK.value}))
    token = store.create_session(restricted, 3600)
    client.cookies.set("ultron_session", token)
    resp = client.post("/api/settings/model", headers={"X-CSRF-Token": "anything"}, json={"llm_model": "x"})
    assert resp.status_code == 403
    assert "manage_settings" in resp.json()["detail"]
