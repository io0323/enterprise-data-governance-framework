"""Phase 17 のテストの共通部品: プロセス内の IdP の模擬と、UI の経路 (セッションの Cookie) のクライアント."""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from edgf.auth import JwtAuthenticator, JwtConfig, Principal
from edgf.config import settings
from edgf.db import SessionLocal
from edgf.main import create_app
from edgf.models import utcnow
from edgf.services import ui_sessions

ISSUER = "https://idp.example.test/realms/edgf"
AUDIENCE = "edgf-control-plane"
CLIENT_ID = "edgf-ui"
SECRET = "client-secret-for-tests-only"
ORIGIN = "https://testserver"
# トークンに埋める目印。DB、ログ、監査に残らないことを確かめる
MARKER = "TOKENMARK7f3e9"


class StaticKeys:
    def __init__(self, key: Any) -> None:
        self.key = key

    def signing_key(self, token: str) -> Any:
        return self.key


class FakeOidc:
    """ディスカバリ、認可 (要求を記録する)、トークン (PKCE と秘密を検証する) の模擬。"""

    def __init__(self) -> None:
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.user: dict = {"sub": "chair", "roles": ["committee"]}
        self.auth_time: int | None = None
        self.authorizations: list[dict] = []
        self.token_requests: list[dict] = []
        self.discovery_doc = {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/auth", "token_endpoint": f"{ISSUER}/token",
                              "jwks_uri": f"{ISSUER}/certs", "end_session_endpoint": f"{ISSUER}/logout"}

    def sign(self, claims: dict, lifetime: int = 300) -> str:
        now = int(time.time())
        return jwt.encode({"iss": ISSUER, "iat": now, "exp": now + lifetime, **claims}, self.private, algorithm="RS256")

    def http_get(self, url: str) -> dict:
        assert url == f"{ISSUER}/.well-known/openid-configuration"
        return dict(self.discovery_doc)

    def http_post(self, url: str, form: dict) -> dict:
        self.token_requests.append(dict(form))
        grant = next((a for a in self.authorizations if a["code"] == form.get("code")), None)
        digest = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
        if grant is None or form.get("client_secret") != SECRET or digest != grant["code_challenge"]:
            from edgf.ui.bff import LoginFailed
            raise LoginFailed("トークンエンドポイントが 400 を返しました")
        user = grant["user"]
        auth_time = self.auth_time if self.auth_time is not None else int(time.time())
        access = self.sign({"sub": user["sub"], "aud": AUDIENCE, "roles": user.get("roles", []), "domains": user.get("domains", []),
                            "marker": MARKER, **({"clearance": user["clearance"]} if "clearance" in user else {})})
        id_token = self.sign({"sub": user["sub"], "aud": CLIENT_ID, "nonce": grant["nonce"], "auth_time": auth_time,
                              "preferred_username": user["sub"], "marker": MARKER})
        return {"access_token": access, "id_token": id_token, "refresh_token": f"refresh-{MARKER}", "token_type": "Bearer"}

    def authorize(self, location: str) -> dict:
        """認可の要求を受けて、コードを発行する (ログインの画面の代わり)。"""
        parts = urlsplit(location)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == self.discovery_doc["authorization_endpoint"]
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        grant = {**query, "code": f"code-{len(self.authorizations)}", "user": dict(self.user)}
        self.authorizations.append(grant)
        return grant


def ui_settings(**overrides: Any):
    return dataclasses.replace(settings, auth_mode="jwt", oidc_issuer=ISSUER, oidc_audience=AUDIENCE, oidc_jwks_url=f"{ISSUER}/certs",
                               ui_oidc_client_id=CLIENT_ID, ui_client_secret=SECRET, ui_oidc_audience=AUDIENCE, **overrides)


def make_app(idp: FakeOidc, **overrides: Any):
    s = ui_settings(**overrides)
    app = create_app(s)
    keys = StaticKeys(idp.private.public_key())
    app.state.authenticator = JwtAuthenticator(JwtConfig(issuer=ISSUER, audience=AUDIENCE), keys)
    oidc = app.state.ui_oidc
    oidc._get, oidc._post, oidc._keys = idp.http_get, idp.http_post, keys
    return app


def login(client: TestClient, idp: FakeOidc, *, return_to: str = "", max_age: int | None = None):
    params = {"return_to": return_to, **({"max_age": max_age} if max_age is not None else {})}
    started = client.get("/ui/auth/login", params=params)
    assert started.status_code == 303, started.text
    grant = idp.authorize(started.headers["location"])
    return client.get("/ui/auth/callback", params={"code": grant["code"], "state": grant["state"]})


# ---------------------------------------------------------------- UI の経路の利用者 (dev モードのアプリにセッションを直接作る)
def session_for(user: str, roles: str = "", domains: str = "", clearance: str | None = None, *, auth_age_minutes: float = 0) -> str:
    role_set = frozenset(r for r in roles.split(",") if r) | {"consumer"}
    from edgf.auth import resolve_clearance
    principal = Principal(subject=user, roles=role_set, clearance=clearance or resolve_clearance(None, role_set),
                          domains=frozenset(d for d in domains.split(",") if d))
    with SessionLocal() as db:
        raw = ui_sessions.create(db, principal, auth_time=utcnow() - timedelta(minutes=auth_age_minutes), name=user)
        db.commit()
    return raw


class UiClient:
    """セッションの Cookie で API を呼ぶ (ブラウザの UI と同じ経路)。書き込みには Origin と CSRF のトークンを付ける。"""

    def __init__(self, app, raw: str) -> None:
        self.client = TestClient(app, base_url=ORIGIN)
        self.client.cookies.set(ui_sessions.SESSION_COOKIE, raw)
        self.raw = raw
        self.csrf = ui_sessions.csrf_token_for(raw)

    def headers(self, extra: dict | None = None) -> dict:
        return {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin", ui_sessions.CSRF_HEADER: self.csrf, **(extra or {})}

    def get(self, path: str, **kw):
        return self.client.get(path, **kw)

    def post(self, path: str, json: Any = None, headers: dict | None = None, **kw):
        return self.client.post(path, json=json, headers=self.headers(headers), **kw)

    def patch(self, path: str, json: Any = None, headers: dict | None = None, **kw):
        return self.client.patch(path, json=json, headers=self.headers(headers), **kw)


@pytest.fixture()
def ui_app(client):
    """dev モードのアプリ (client と同じ DB)。UI の経路は Cookie で、client は dev の X-Actor で準備に使う。"""
    return client.app


def as_ui(app, user: str, roles: str = "", domains: str = "", clearance: str | None = None, *, auth_age_minutes: float = 0) -> UiClient:
    return UiClient(app, session_for(user, roles, domains, clearance, auth_age_minutes=auth_age_minutes))


def old(minutes: float) -> datetime:
    return utcnow() - timedelta(minutes=minutes)
