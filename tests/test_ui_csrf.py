"""Phase 17b: API の入口 (Bearer とセッションの Cookie)、CSRF、ステップアップ (確定事項 2、3、5)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from edgf.config import settings
from edgf.db import SessionLocal
from edgf.models import AuditEvent
from edgf.services import ui_sessions
from tests.process_helpers import ADMIN, API, U, asset
from tests.ui_helpers import AUDIENCE, ORIGIN, FakeOidc, UiClient, as_ui, login, make_app

STALE = settings.step_up_minutes + 5


def _exception(client, who=U("req", "admin")) -> str:
    a = asset(client, "sales.report", data_class="analytical")
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    r = client.post(f"{API}/policies/exceptions", headers=who, json={"policy_id": "LIN-001", "asset_id": a["id"], "reason": "移行中",
                                                                    "expires_at": expires})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _issue(client) -> str:
    a = asset(client, "sales.orders", owner="alice")
    r = client.post(f"{API}/issues", headers=ADMIN, json={"kind": "other", "title": "t", "asset_id": a["id"]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def audit_payloads(action):
    with SessionLocal() as db:
        return [e.payload for e in db.scalars(select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.seq))]


# ---------------------------------------------------------------- CSRF
@pytest.fixture()
def issue_ctx(client):
    issue_id = _issue(client)
    return client.app, issue_id, as_ui(client.app, "alice", "owner")


def test_get_needs_no_csrf(issue_ctx):
    _, issue_id, ui = issue_ctx
    r = ui.client.get(f"{API}/issues")  # Origin も CSRF のトークンも付けない
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("change,detail", [
    ({ui_sessions.CSRF_HEADER: None}, "X-CSRF-Token"),
    ({ui_sessions.CSRF_HEADER: "wrong"}, "X-CSRF-Token"),
    ({"Origin": "https://evil.example"}, "Origin"),
    ({"Origin": None}, "Origin"),
    ({"Origin": "http://testserver"}, "Origin"),  # スキームが違えば別のオリジン
    ({"Sec-Fetch-Site": "cross-site"}, "Sec-Fetch-Site"),
    ({"Sec-Fetch-Site": "same-site"}, "Sec-Fetch-Site"),
])
def test_write_without_valid_csrf_is_rejected(issue_ctx, change, detail):
    _, issue_id, ui = issue_ctx
    headers = {k: v for k, v in {**ui.headers(), **change}.items() if v is not None}
    r = ui.client.patch(f"{API}/issues/{issue_id}", json={"status": "in_progress"}, headers=headers)
    assert r.status_code == 403 and r.json()["type"].endswith("csrf-failed") and detail in r.json()["detail"]
    assert audit_payloads("issue.update") == []


def test_another_sessions_token_is_rejected(issue_ctx):
    app, issue_id, ui = issue_ctx
    other = as_ui(app, "alice", "owner")
    r = ui.client.patch(f"{API}/issues/{issue_id}", json={"status": "in_progress"},
                        headers={**ui.headers(), ui_sessions.CSRF_HEADER: other.csrf})
    assert r.status_code == 403


def test_valid_write_is_recorded_with_channel(issue_ctx):
    _, issue_id, ui = issue_ctx
    r = ui.patch(f"{API}/issues/{issue_id}", json={"status": "in_progress"})
    assert r.status_code == 200, r.text
    [payload] = audit_payloads("issue.update")
    assert payload["channel"] == "ui" and payload["session"] == ui_sessions.digest(ui.raw)[:12]
    assert ui.raw not in str(payload)


def test_bearer_path_skips_csrf_and_has_no_channel(client):
    issue_id = _issue(client)
    r = client.patch(f"{API}/issues/{issue_id}", headers=U("alice", "owner"), json={"status": "in_progress"})
    assert r.status_code == 200
    assert "channel" not in audit_payloads("issue.update")[0]


def test_cookie_and_dev_header_together_is_400(issue_ctx):
    _, issue_id, ui = issue_ctx
    r = ui.client.get(f"{API}/issues", headers={"X-Actor": "alice"})
    assert r.status_code == 400 and r.json()["type"].endswith("ambiguous-credentials")


def test_expired_cookie_is_401_and_cleared(issue_ctx):
    app, issue_id, _ = issue_ctx
    stale = UiClient(app, "not-a-session")
    r = stale.client.get(f"{API}/issues")
    assert r.status_code == 401 and r.json()["type"].endswith("session-expired") and "max-age=0" in r.headers["set-cookie"].lower()


# ---------------------------------------------------------------- jwt モード: Bearer と Cookie
@pytest.fixture()
def jwt_ctx(client):
    idp = FakeOidc()
    app = make_app(idp)
    return idp, app


def _bearer(idp, sub, roles):
    token = idp.sign({"sub": sub, "aud": AUDIENCE, "roles": roles})
    return {"Authorization": f"Bearer {token}"}


def test_jwt_bearer_writes_without_csrf_and_both_is_400(jwt_ctx):
    idp, app = jwt_ctx
    api = TestClient(app, base_url=ORIGIN)
    r = api.post(f"{API}/glossary/terms", headers=_bearer(idp, "root", ["admin"]), json={"name": "受注", "definition": "注文"})
    assert r.status_code == 201, r.text
    browser = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    login(browser, idp)
    r = browser.get(f"{API}/glossary/terms", headers=_bearer(idp, "root", ["admin"]))
    assert r.status_code == 400 and r.json()["type"].endswith("ambiguous-credentials")
    assert browser.get(f"{API}/glossary/terms").status_code == 200


# ---------------------------------------------------------------- ステップアップ
def test_step_up_required_for_approval_actions_on_ui_path(client):
    ex = _exception(client)
    stale = as_ui(client.app, "chair", "committee", auth_age_minutes=STALE)
    r = stale.post(f"{API}/policies/exceptions/{ex}/decision", json={"approve": True})
    assert r.status_code == 401 and r.json()["type"].endswith("step-up-required")
    assert r.json()["login_url"] == "/ui/auth/login?max_age=0" and r.json()["action"] == "exception.decide"
    assert stale.client.get(f"{API}/policies/exceptions").status_code == 200  # 読み取りは止めない
    fresh = as_ui(client.app, "chair", "committee")
    assert fresh.post(f"{API}/policies/exceptions/{ex}/decision", json={"approve": True}).status_code == 200


def test_step_up_comes_after_authorization(client):
    """権限の無い利用者には、再ログインではなく従来どおり 403。"""
    ex = _exception(client)
    stale = as_ui(client.app, "someone", "steward", auth_age_minutes=STALE)
    r = stale.post(f"{API}/policies/exceptions/{ex}/decision", json={"approve": True})
    assert r.status_code == 403


def test_step_up_not_applied_to_bearer_path(client):
    ex = _exception(client)
    r = client.post(f"{API}/policies/exceptions/{ex}/decision", headers=U("chair", "committee"), json={"approve": True})
    assert r.status_code == 200


def test_step_up_not_required_for_non_approval_writes(issue_ctx):
    app, issue_id, _ = issue_ctx
    stale = as_ui(app, "alice", "owner", auth_age_minutes=STALE)
    assert stale.patch(f"{API}/issues/{issue_id}", json={"status": "in_progress"}).status_code == 200


def test_step_up_by_logging_in_again(jwt_ctx):
    """auth_time が古いと step-up-required。max_age=0 で再ログインすると成功する (jwt モードの BFF で通しで確認)。"""
    idp, app = jwt_ctx
    admin = TestClient(app, base_url=ORIGIN)
    root = _bearer(idp, "root", ["admin"])
    a = admin.post(f"{API}/assets", headers=root, json={"qualified_name": "sales.report", "name": "r", "data_class": "analytical",
                                                         "sensitivity": "internal"}).json()
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    ex = admin.post(f"{API}/policies/exceptions", headers=root, json={"policy_id": "LIN-001", "asset_id": a["id"], "reason": "移行中",
                                                                      "expires_at": expires}).json()
    browser = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    idp.auth_time = int((datetime.now(timezone.utc) - timedelta(minutes=STALE)).timestamp())
    login(browser, idp)

    def decide():
        csrf = browser.get("/ui/session").json()["csrf_token"]
        return browser.post(f"{API}/policies/exceptions/{ex['id']}/decision", json={"approve": True},
                            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
    r = decide()
    assert r.status_code == 401 and r.json()["type"].endswith("step-up-required")
    idp.auth_time = None  # IdP が再認証した
    assert login(browser, idp, max_age=0, return_to="#/inbox").headers["location"] == "/ui/#/inbox"
    assert decide().status_code == 200


# ---------------------------------------------------------------- 却下の理由
@pytest.mark.parametrize("path", ["api", "ui"])
def test_exception_rejection_requires_reason(client, path):
    ex = _exception(client)
    body = {"approve": False, "comment": "  "}
    if path == "api":
        r = client.post(f"{API}/policies/exceptions/{ex}/decision", headers=U("chair", "committee"), json=body)
    else:
        r = as_ui(client.app, "chair", "committee").post(f"{API}/policies/exceptions/{ex}/decision", json=body)
    assert r.status_code == 422
    r = client.post(f"{API}/policies/exceptions/{ex}/decision", headers=U("chair", "committee"), json={"approve": False, "comment": "根拠が不足"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
