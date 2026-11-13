"""Phase 17a: BFF のログインとセッション (確定事項 1、3、4)."""
from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from edgf.auth import AuthConfigError
from edgf.config import settings
from edgf.db import SessionLocal
from edgf.jobs import run_job
from edgf.main import create_app
from edgf.models import AuditEvent, UiLoginState, UiSession, utcnow
from edgf.services import ui_sessions
from edgf.ui import bff
from tests.conftest import dump_database
from tests.ui_helpers import AUDIENCE, CLIENT_ID, ISSUER, MARKER, ORIGIN, SECRET, FakeOidc, login, make_app


@pytest.fixture()
def idp(client):  # client で DB を準備する
    return FakeOidc()


@pytest.fixture()
def browser(idp):
    return TestClient(make_app(idp), base_url=ORIGIN, follow_redirects=False)


def cookie_header(response, name):
    return next(v for k, v in response.headers.multi_items() if k == "set-cookie" and v.startswith(name + "="))


def session_rows():
    with SessionLocal() as db:
        return db.scalars(select(UiSession)).all()


# ---------------------------------------------------------------- ログイン
def test_login_redirects_with_pkce_and_stores_state_server_side(browser, idp):
    r = browser.get("/ui/auth/login", params={"return_to": "#/inbox"})
    assert r.status_code == 303
    grant = idp.authorize(r.headers["location"])
    assert grant["code_challenge_method"] == "S256" and len(grant["code_challenge"]) == 43 and grant["nonce"] and grant["state"]
    assert grant["client_id"] == CLIENT_ID and grant["redirect_uri"] == f"{ORIGIN}/ui/auth/callback" and grant["audience"] == AUDIENCE
    assert "max_age" not in grant
    login_cookie = cookie_header(r, ui_sessions.LOGIN_COOKIE).lower()
    assert all(a in login_cookie for a in ("secure", "httponly", "samesite=lax", "path=/", "max-age=600")) and "domain" not in login_cookie
    # Cookie には参照だけ。state / nonce / verifier はサーバ側にあり、参照はハッシュで持つ
    ref = browser.cookies.get(ui_sessions.LOGIN_COOKIE)
    assert grant["state"] not in login_cookie and grant["nonce"] not in login_cookie
    with SessionLocal() as db:
        [row] = db.scalars(select(UiLoginState)).all()
    assert row.ref_hash == ui_sessions.digest(ref) and row.return_to == "#/inbox"


def test_callback_creates_session_with_strict_cookie_and_returns(browser, idp):
    r = login(browser, idp, return_to="#/inbox")
    assert r.status_code == 303 and r.headers["location"] == "/ui/#/inbox"
    cookie = cookie_header(r, ui_sessions.SESSION_COOKIE).lower()
    assert all(a in cookie for a in ("secure", "httponly", "samesite=strict", "path=/")) and "domain" not in cookie
    form = idp.token_requests[-1]
    assert form["grant_type"] == "authorization_code" and form["client_secret"] == SECRET and form["client_id"] == CLIENT_ID
    me = browser.get("/ui/session")
    assert me.status_code == 200 and me.headers["cache-control"] == "no-store"
    body = me.json()
    assert body["subject"] == "chair" and "committee" in body["roles"] and body["clearance"] == "restricted"
    assert body["csrf_token"] == ui_sessions.csrf_token_for(browser.cookies.get(ui_sessions.SESSION_COOKIE))
    assert not {"access_token", "id_token", "refresh_token"} & set(body)
    with SessionLocal() as db:
        assert db.scalars(select(UiLoginState)).all() == []  # 途中の状態は 1 回で消える


def test_session_id_changes_on_every_login(browser, idp):
    login(browser, idp)
    first = browser.cookies.get(ui_sessions.SESSION_COOKIE)
    login(browser, idp)
    second = browser.cookies.get(ui_sessions.SESSION_COOKIE)
    assert first != second
    assert [r.id_hash for r in session_rows()] == [ui_sessions.digest(second)]  # 以前のセッションは消える
    other = TestClient(browser.app, base_url=ORIGIN)
    other.cookies.set(ui_sessions.SESSION_COOKIE, first)
    assert other.get("/ui/session").status_code == 401


def test_session_fixation_cookie_is_replaced(idp):
    """攻撃者が仕込んだ Cookie の値は、ログインで使われない (サーバは常に新しい ID を発行する)。"""
    planted = TestClient(make_app(idp), base_url=ORIGIN, follow_redirects=False,
                         cookies={ui_sessions.SESSION_COOKIE: "attacker-chosen-value"})
    r = login(planted, idp)
    issued = cookie_header(r, ui_sessions.SESSION_COOKIE).split(";")[0].split("=", 1)[1]
    assert issued != "attacker-chosen-value" and [row.id_hash for row in session_rows()] == [ui_sessions.digest(issued)]


def test_no_raw_ids_or_tokens_in_db_logs_or_audit(browser, idp, edgf_logs):
    login(browser, idp)
    raw = browser.cookies.get(ui_sessions.SESSION_COOKIE)
    csrf = browser.get("/ui/session").json()["csrf_token"]
    dump = dump_database()
    assert raw.lower() not in dump and csrf.lower() not in dump
    assert MARKER.lower() not in dump and "refresh-" not in dump
    assert MARKER.lower() not in edgf_logs.text() and raw.lower() not in edgf_logs.text()
    with SessionLocal() as db:
        [event] = db.scalars(select(AuditEvent).where(AuditEvent.action == "ui.login")).all()
    assert event.actor == "chair" and event.payload["channel"] == "ui" and event.payload["session"] == ui_sessions.digest(raw)[:12]


@pytest.mark.parametrize("case", ["state", "reuse", "expired", "no_cookie", "idp_error", "secret"])
def test_callback_failures_do_not_create_sessions(browser, idp, case):
    started = browser.get("/ui/auth/login")
    grant = idp.authorize(started.headers["location"])
    params = {"code": grant["code"], "state": grant["state"]}
    if case == "state":
        params["state"] = "other"
    elif case == "reuse":
        assert browser.get("/ui/auth/callback", params=params).status_code == 303
        browser.cookies.delete(ui_sessions.SESSION_COOKIE)
        browser.cookies.set(ui_sessions.LOGIN_COOKIE, started.cookies.get(ui_sessions.LOGIN_COOKIE))
        with SessionLocal() as db:
            db.execute(update(UiSession).values(expires_at=utcnow()))  # 1 回目のセッションは除いて数える
            db.commit()
    elif case == "expired":
        with SessionLocal() as db:
            db.execute(update(UiLoginState).values(expires_at=utcnow() - timedelta(seconds=1)))
            db.commit()
    elif case == "no_cookie":
        browser.cookies.delete(ui_sessions.LOGIN_COOKIE)
    elif case == "idp_error":
        params = {"error": "access_denied", "state": grant["state"]}
    else:
        browser.app.state.ui_oidc.s = dataclasses.replace(browser.app.state.ui_oidc.s, ui_client_secret="wrong")
    r = browser.get("/ui/auth/callback", params=params)
    assert r.status_code == 303 and r.headers["location"] == "/ui/?error=login_failed"
    assert browser.get("/ui/session").status_code == 401


def test_return_to_only_inside_the_ui(browser, idp):
    for bad in ("https://evil.example/", "//evil.example", "javascript:alert(1)", "#/x\n"):
        r = login(browser, idp, return_to=bad)
        assert r.headers["location"] == "/ui/"


def test_discovery_issuer_must_match(browser, idp):
    idp.discovery_doc["issuer"] = "https://other.example"
    with pytest.raises(bff.LoginFailed):
        browser.get("/ui/auth/login")


def test_id_token_must_match_nonce_and_audience(browser, idp):
    started = browser.get("/ui/auth/login")
    grant = idp.authorize(started.headers["location"])
    grant["nonce"] = "tampered"
    r = browser.get("/ui/auth/callback", params={"code": grant["code"], "state": grant["state"]})
    assert r.headers["location"] == "/ui/?error=login_failed"


def test_max_age_requires_fresh_authentication(browser, idp):
    idp.auth_time = int((utcnow() - timedelta(hours=1)).timestamp())
    r = login(browser, idp, max_age=0)
    assert r.headers["location"] == "/ui/?error=login_failed"  # IdP が再認証しなかった
    assert idp.authorizations[-1]["max_age"] == "0"
    idp.auth_time = None
    assert login(browser, idp, max_age=0).headers["location"] == "/ui/"


# ---------------------------------------------------------------- 失効
def test_idle_and_absolute_expiry(browser, idp):
    login(browser, idp)
    assert browser.get("/ui/session").status_code == 200
    with SessionLocal() as db:
        db.execute(update(UiSession).values(last_seen_at=utcnow() - timedelta(minutes=settings.session_idle_minutes)))
        db.commit()
    r = browser.get("/ui/session")
    assert r.status_code == 401 and r.json()["type"].endswith("session-expired")
    assert session_rows() == []
    login(browser, idp)
    with SessionLocal() as db:
        db.execute(update(UiSession).values(expires_at=utcnow()))
        db.commit()
    assert browser.get("/ui/session").status_code == 401


def test_activity_extends_idle_but_not_max(idp):
    limits = ui_sessions.Limits(idle=timedelta(minutes=30), max_age=timedelta(hours=8))
    now = utcnow()
    with SessionLocal() as db:
        from edgf.auth import Principal
        raw = ui_sessions.create(db, Principal("u", frozenset({"consumer"}), "internal", frozenset()), auth_time=now, now=now, limits=limits)
        for minutes in range(20, 8 * 60, 20):  # 20 分ごとに操作すれば、無操作では切れない
            assert ui_sessions.load(db, raw, now=now + timedelta(minutes=minutes), limits=limits) is not None
        assert ui_sessions.load(db, raw, now=now + timedelta(hours=8), limits=limits) is None  # 最大時間で切れる
        db.commit()


def test_logout_requires_csrf_and_invalidates(browser, idp):
    login(browser, idp)
    csrf = browser.get("/ui/session").json()["csrf_token"]
    assert browser.post("/ui/auth/logout", headers={"Origin": ORIGIN}).status_code == 403
    r = browser.post("/ui/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert r.json()["logout_url"].startswith(f"{ISSUER}/logout?") and "post_logout_redirect_uri" in r.json()["logout_url"]
    assert "max-age=0" in cookie_header(r, ui_sessions.SESSION_COOKIE).lower()
    assert session_rows() == []
    with SessionLocal() as db:
        assert db.scalars(select(AuditEvent.action).where(AuditEvent.action == "ui.logout")).all() == ["ui.logout"]


def test_cleanup_job_deletes_expired(browser, idp):
    login(browser, idp)
    browser.get("/ui/auth/login")
    with SessionLocal() as db:
        db.execute(update(UiSession).values(expires_at=utcnow() - timedelta(seconds=1)))
        db.execute(update(UiLoginState).values(expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    result = run_job("ui_session_cleanup")
    assert result["status"] == "succeeded" and result["result"] == {"sessions_deleted": 1, "login_states_deleted": 1}


# ---------------------------------------------------------------- 設定
def test_ui_is_not_available_in_dev_mode(client):
    assert client.get("/ui/config.json").json() == {"configured": False}
    assert client.get("/ui/auth/login").status_code == 404


def test_config_json_has_no_oidc_details(browser):
    assert browser.get("/ui/config.json").json() == {"configured": True}


def test_production_requires_client_secret():
    base = dataclasses.replace(settings, env="production", auth_mode="jwt", oidc_issuer=ISSUER, oidc_audience=AUDIENCE,
                               oidc_jwks_url="https://idp.example.test/certs", subject_key="k" * 32, ui_oidc_client_id=CLIENT_ID)
    with pytest.raises(AuthConfigError, match="EDGF_UI_CLIENT_SECRET"):
        bff.validate_ui_settings(base)
    with pytest.raises(AuthConfigError, match="https"):
        bff.validate_ui_settings(dataclasses.replace(base, ui_client_secret="s", ui_public_url="http://ui.example"))
    bff.validate_ui_settings(dataclasses.replace(base, ui_client_secret="s", ui_public_url="https://ui.example"))
    bff.validate_ui_settings(dataclasses.replace(base, ui_oidc_client_id=""))  # UI を使わないなら秘密は不要


def test_create_app_refuses_production_ui_without_secret(monkeypatch):
    s = dataclasses.replace(settings, env="production", auth_mode="jwt", oidc_issuer=ISSUER, oidc_audience=AUDIENCE,
                            oidc_jwks_url="https://idp.example.test/certs", subject_key="k" * 32, ui_oidc_client_id=CLIENT_ID)
    with pytest.raises(AuthConfigError):
        create_app(s)
