"""Phase 7 (§4 / §11): 認証 — jwt / dev モードの Principal 解決と起動時の安全策."""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from starlette.requests import Request

from edgf import migrate
from edgf.auth import (AuthConfigError, AuthError, JwtAuthenticator, JwtConfig, Principal, build_authenticator,
                       get_claim, normalize_roles, resolve_clearance, validate_auth_settings)
from edgf.config import Settings
from edgf.db import engine
from edgf.main import create_app
from tests.conftest import reset_db

API = "/api/v1"
ISS = "https://idp.example.test/realms/edgf"
AUD = "edgf-control-plane"


# ---------------------------------------------------------------- 鍵と JWKS サーバ (テスト用にローカル生成)
def _rsa():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def keys():
    return {"k1": _rsa(), "k2": _rsa(), "rogue": _rsa()}


def _jwk(kid, private_key):
    d = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    return {**d, "kid": kid, "use": "sig", "alg": "RS256"}


class _JwksServer:
    def __init__(self):
        self.jwks: dict = {"keys": []}
        self.fetches = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.fetches += 1
                body = json.dumps(outer.jwks).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/jwks.json"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def jwks(keys):
    srv = _JwksServer()
    srv.jwks = {"keys": [_jwk("k1", keys["k1"])]}
    yield srv
    srv.close()


def jwt_settings(jwks_url: str, **kw) -> Settings:
    base = Settings(auth_mode="jwt", env="test", oidc_issuer=ISS, oidc_audience=AUD, oidc_jwks_url=jwks_url,
                    jwks_refresh_cooldown_seconds=0)
    return replace(base, **kw)


@pytest.fixture()
def jwt_client(jwks):
    reset_db()
    migrate.upgrade(engine)
    with TestClient(create_app(jwt_settings(jwks.url))) as c:
        yield c
    reset_db()


def token(keys, kid="k1", signer=None, **claims) -> str:
    now = int(time.time())
    body = {"iss": ISS, "aud": AUD, "sub": "u-alice", "iat": now, "exp": now + 600, "roles": ["committee"], **claims}
    body = {k: v for k, v in body.items() if v is not None}
    return jwt.encode(body, signer or keys[kid], algorithm="RS256", headers={"kid": kid})


def bearer(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


def assert_401(r):
    assert r.status_code == 401, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"] == "urn:edgf:problem:unauthenticated"
    assert r.headers["www-authenticate"].startswith("Bearer")


# ---------------------------------------------------------------- jwt モード: 正常系
def test_jwt_valid_token_authenticates_and_audits_subject(jwt_client, keys):
    t = token(keys)
    assert jwt_client.get(f"{API}/domains", headers=bearer(t)).status_code == 200
    assert jwt_client.post(f"{API}/domains", headers=bearer(t), json={"name": "sales"}).status_code == 201
    ev = jwt_client.get(f"{API}/audit/events", headers=bearer(t), params={"action": "domain.create"}).json()[0]
    assert ev["actor"] == "u-alice"  # JWT 由来の操作は接頭辞なし
    assert jwt_client.get("/health").json()["auth_mode"] == "jwt"


def test_jwks_is_cached_and_follows_key_rotation(jwt_client, jwks, keys):
    for _ in range(3):
        assert jwt_client.get(f"{API}/domains", headers=bearer(token(keys))).status_code == 200
    assert jwks.fetches == 1  # 2 回目以降はキャッシュ
    jwks.jwks = {"keys": [_jwk("k1", keys["k1"]), _jwk("k2", keys["k2"])]}  # IdP が新しい鍵を公開
    assert jwt_client.get(f"{API}/domains", headers=bearer(token(keys, kid="k2"))).status_code == 200
    assert jwks.fetches == 2  # 未知の kid で再取得した


# ---------------------------------------------------------------- jwt モード: 異常系 (すべて 401)
def test_jwt_missing_token_is_401(jwt_client):
    r = jwt_client.get(f"{API}/domains")
    assert_401(r)
    assert r.headers["www-authenticate"] == "Bearer"


def test_jwt_mode_ignores_x_actor(jwt_client, keys):
    assert_401(jwt_client.get(f"{API}/domains", headers={"X-Actor": "alice", "X-Roles": "admin"}))
    # トークンと併用しても X-Actor は使われない
    t = token(keys, sub="u-bob")
    jwt_client.post(f"{API}/domains", headers={**bearer(t), "X-Actor": "mallory"}, json={"name": "hr"})
    ev = jwt_client.get(f"{API}/audit/events", headers=bearer(t), params={"action": "domain.create"}).json()[0]
    assert ev["actor"] == "u-bob"


@pytest.mark.parametrize("case", ["expired", "aud", "iss", "signature", "nbf", "no_exp", "unknown_kid", "garbage",
                                  "hs256", "not_bearer"])
def test_jwt_invalid_tokens_are_401(jwt_client, keys, case):
    now = int(time.time())
    t = {
        "expired": lambda: token(keys, exp=now - 3600, iat=now - 7200),
        "aud": lambda: token(keys, aud="another-api"),
        "iss": lambda: token(keys, iss="https://evil.example.test"),
        "signature": lambda: token(keys, signer=keys["rogue"]),  # kid は正規、署名鍵は別物
        "nbf": lambda: token(keys, nbf=now + 3600),
        "no_exp": lambda: token(keys, exp=None),
        "unknown_kid": lambda: token(keys, kid="rogue"),
        "garbage": lambda: "not.a.jwt",
        "hs256": lambda: jwt.encode({"iss": ISS, "aud": AUD, "sub": "x", "exp": now + 600}, "s" * 32,
                                    algorithm="HS256", headers={"kid": "k1"}),
        "not_bearer": lambda: None,
    }[case]()
    headers = {"Authorization": f"Basic {token(keys)}"} if case == "not_bearer" else bearer(t)
    r = jwt_client.get(f"{API}/domains", headers=headers)
    assert_401(r)
    if case != "not_bearer":
        assert 'error="invalid_token"' in r.headers["www-authenticate"]


def test_jwks_unreachable_fails_closed_with_503(keys):
    reset_db()
    migrate.upgrade(engine)
    with TestClient(create_app(jwt_settings("http://127.0.0.1:9/jwks.json"))) as c:
        r = c.get(f"{API}/domains", headers=bearer(token(keys)))
    assert r.status_code == 503 and r.json()["type"] == "urn:edgf:problem:auth-unavailable"
    reset_db()


# ---------------------------------------------------------------- claim の対応付け (IdP 非依存)
class _StaticKeys:
    def __init__(self, key):
        self.key = key

    def signing_key(self, token):
        return self.key


def _request(headers: dict) -> Request:
    return Request({"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]})


def test_claim_mapping_is_configurable(keys):
    cfg = JwtConfig(issuer=ISS, audience=AUD, claim_subject="preferred_username", claim_roles="realm_access.roles",
                    claim_clearance="ext.clearance", claim_domains="ext.domains",
                    role_map=(("dg-committee", "committee"), ("dg-stewards", "steward")))
    auth = JwtAuthenticator(cfg, _StaticKeys(keys["k1"].public_key()))
    t = token(keys, sub="opaque-uuid", preferred_username="hanako", roles=None,
              realm_access={"roles": ["dg-stewards", "offline_access"]},
              ext={"clearance": "restricted", "domains": "sales hr"})
    p = auth.authenticate(_request(bearer(t)))
    assert p == Principal(subject="hanako", roles=frozenset({"steward", "consumer"}), clearance="restricted",
                          domains=frozenset({"sales", "hr"}), auth_mode="jwt")
    with pytest.raises(AuthError):  # subject claim が無い
        auth.authenticate(_request(bearer(token(keys, preferred_username=None))))


def test_role_and_clearance_normalization():
    assert normalize_roles("owner, unknown steward") == {"owner", "steward", "consumer"}
    assert normalize_roles(None) == {"consumer"}
    assert normalize_roles(["grp-a"], {"grp-a": "custodian"}) == {"custodian", "consumer"}
    assert resolve_clearance(None, {"consumer"}) == "internal"
    assert resolve_clearance(None, {"steward", "consumer"}) == "confidential"
    assert resolve_clearance("RESTRICTED", {"consumer"}) == "restricted"
    assert resolve_clearance("top-secret", {"committee"}) == "restricted"  # 不正値はロール既定にフォールバック
    assert get_claim({"a": {"b": [1]}}, "a.b") == [1] and get_claim({"a": 1}, "a.b") is None


# ---------------------------------------------------------------- dev モード
def test_dev_mode_requires_x_actor_and_prefixes_audit_actor(client):
    r = client.get(f"{API}/domains", headers={"X-Actor": ""})
    assert r.status_code == 401 and 'error="invalid_request"' in r.headers["www-authenticate"]
    assert client.get(f"{API}/domains", headers={"X-Actor": "bad actor\n"}).status_code == 401
    client.post(f"{API}/domains", headers={"X-Actor": "taro"}, json={"name": "ops"})
    ev = client.get(f"{API}/audit/events", params={"action": "domain.create"}).json()[0]
    assert ev["actor"] == "dev:taro"
    assert client.get("/health").json()["auth_mode"] == "dev"


def test_dev_mode_principal_from_headers():
    from edgf.auth import DevAuthenticator
    p = DevAuthenticator().authenticate(_request({"X-Actor": "k.sato@corp", "X-Roles": "owner,root",
                                                  "X-Domains": "sales", "X-Clearance": "restricted"}))
    assert p.subject == "k.sato@corp" and p.roles == {"owner", "consumer"} and p.domains == {"sales"}
    assert p.clearance == "restricted" and p.audit_actor == "dev:k.sato@corp"
    assert p.in_domain("sales") and not p.in_domain("hr") and not p.in_domain(None)


def test_dev_mode_logs_warning_on_startup():
    lines: list[str] = []

    class H(logging.Handler):
        def emit(self, record):
            lines.append(f"{record.levelname} {record.getMessage()}")

    h = H()
    logging.getLogger("edgf").addHandler(h)
    try:
        create_app(Settings(auth_mode="dev", env="test"))
    finally:
        logging.getLogger("edgf").removeHandler(h)
    assert any(ln.startswith("WARNING") and "EDGF_AUTH_MODE=dev" in ln for ln in lines)


def test_access_log_records_actor(client):
    lines: list[dict] = []

    class H(logging.Handler):
        def emit(self, record):
            lines.append(record.fields)

    h = H()
    logging.getLogger("edgf.access").addHandler(h)
    try:
        client.get(f"{API}/domains", headers={"X-Actor": "jiro"})
    finally:
        logging.getLogger("edgf.access").removeHandler(h)
    assert lines[-1]["actor"] == "dev:jiro"


# ---------------------------------------------------------------- 起動時の安全策 (fail fast)
def test_production_with_dev_mode_refuses_to_start():
    with pytest.raises(AuthConfigError, match="production"):
        create_app(Settings(auth_mode="dev", env="production"))


@pytest.mark.parametrize("kw, match", [
    ({"auth_mode": "basic"}, "EDGF_AUTH_MODE"),
    ({"oidc_issuer": ""}, "EDGF_OIDC_ISSUER"),
    ({"oidc_jwks_url": ""}, "EDGF_OIDC_JWKS_URL"),
    ({"oidc_algorithms": ("HS256",)}, "非対称"),
    ({"oidc_algorithms": ()}, "非対称"),
    ({"env": "production"}, "https"),
])
def test_invalid_jwt_settings_refuse_to_start(kw, match):
    with pytest.raises(AuthConfigError, match=match):
        validate_auth_settings(jwt_settings("http://127.0.0.1:1/jwks.json", **kw))


def test_production_jwt_with_https_jwks_is_accepted():
    auth = build_authenticator(jwt_settings("https://idp.example.test/jwks.json", env="production"))
    assert auth.mode == "jwt"


def test_default_settings_are_jwt(monkeypatch):
    monkeypatch.delenv("EDGF_AUTH_MODE", raising=False)
    assert Settings().auth_mode == "jwt"
    with pytest.raises(AuthConfigError):  # IdP 未設定の jwt は起動しない
        create_app(Settings())
