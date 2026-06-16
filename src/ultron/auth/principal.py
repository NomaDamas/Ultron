"""Principal, scope, and session primitives for the local Ultron app boundary."""

from __future__ import annotations

import secrets
import time
from enum import StrEnum
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field


class Scope(StrEnum):
    APPROVE_PROMOTION = "approve_promotion"
    ROLLBACK = "rollback"
    RESTORE = "restore"
    REQUEST_PERMISSION_EXPANSION = "request_permission_expansion"
    RUN_BENCHMARK = "run_benchmark"
    MANAGE_SETTINGS = "manage_settings"


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject: str
    scopes: frozenset[str] = Field(default_factory=frozenset)
    tenant_scope: str = "local"

    def has_scope(self, scope: Scope | str) -> bool:
        return str(scope.value if isinstance(scope, Scope) else scope) in self.scopes


DEFAULT_LOCAL_PRINCIPAL = Principal(
    subject="local-operator",
    tenant_scope="local",
    scopes=frozenset(scope.value for scope in Scope),
)


class SessionStore:
    """Small in-process session store with explicit expiry."""

    def __init__(self, *, secure_cookies: bool = False, same_site: str = "strict") -> None:
        self.secure_cookies = secure_cookies
        self.same_site = same_site
        self._sessions: dict[str, tuple[Principal, float]] = {}

    def create_session(self, principal: Principal, ttl: float, *, now: float | None = None) -> str:
        if ttl <= 0:
            raise ValueError("session ttl must be positive")
        token = secrets.token_urlsafe(32)
        self._sessions[token] = (principal, (time.time() if now is None else now) + ttl)
        return token

    def resolve(self, token: str | None, now: float | None = None) -> Principal | None:
        if not token:
            return None
        stored = self._sessions.get(token)
        if stored is None:
            return None
        principal, expires_at = stored
        current = time.time() if now is None else now
        if expires_at <= current:
            self._sessions.pop(token, None)
            return None
        return principal

    def cookie_attributes(self, *, httponly: bool = True) -> Mapping[str, bool | str]:
        return {"httponly": httponly, "samesite": self.same_site, "secure": self.secure_cookies}
