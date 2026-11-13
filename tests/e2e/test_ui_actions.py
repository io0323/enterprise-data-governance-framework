"""Phase 17: 書き込みのできる UI の E2E (承認箱、却下の理由、キーボード、ステップアップ、別オリジン、Policy の公開)."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN, COMMITTEE, CONSUMER, OWNER, Session

pytestmark = pytest.mark.e2e
PROPOSER = {"sub": "proposer", "roles": ["committee"]}


def new_request(api_as) -> str:
    """Owner (asset-owner) が判断する閲覧の申請を作る。テストごとに別の資産にする。"""
    admin = api_as(ADMIN)
    name = f"sales.req_{uuid.uuid4().hex[:8]}"
    a = admin.post("/assets", json={"qualified_name": name, "name": name, "description": "d", "domain": "sales", "data_class": "transactional",
                                    "sensitivity": "confidential", "owner": "asset-owner", "steward": "steward1", "custodian": "custodian1"})
    assert a.status_code == 201, a.text
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    r = api_as(CONSUMER).post("/approvals", json={"type": "access_request", "asset_id": a.json()["id"], "reason": "調査", "purpose": "分析",
                                                  "expires_at": expires})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def row(page, item_id):
    """案件の操作のボタンの並び (data-item に案件の ID)。"""
    return page.locator(f'.op-buttons[data-item="{item_id}"]')


def audit_of(api_as, action, entity_id) -> list[dict]:
    events = api_as(ADMIN).get("/audit/events", params={"action": action, "limit": 1000}).json()
    return [e for e in events if e["entity_id"] == entity_id]


def open_operation(s, approval_id, op):
    s.go("#/inbox", "#main tbody tr")
    s.settled()
    row(s.page, approval_id).locator(f"button[data-op={op}]").click()
    expect(s.page.locator("#decision-form")).to_be_visible()


# ---------------------------------------------------------------- 承認箱
def test_login_inbox_approve_until_audit(session_of, api_as):
    approval_id = new_request(api_as)
    s = session_of(OWNER)
    open_operation(s, approval_id, "approve")
    s.page.fill("#field-comment", "確認しました")
    s.page.click("#decision-submit")
    expect(s.page.locator("#inbox-status")).to_contain_text("承認しました")
    expect(row(s.page, approval_id)).to_have_count(0)
    [event] = audit_of(api_as, "approval.decision", approval_id)
    assert event["actor"] == "asset-owner" and event["payload"]["channel"] == "ui" and len(event["payload"]["session"]) == 12
    assert event["payload"]["approve"] is True
    # 書き込みは actions.js から、X-CSRF-Token を付けて送っている
    [write] = [r for r in s.requests if r.method == "POST" and f"/approvals/{approval_id}/decision" in r.url]
    headers = write.all_headers()
    assert headers.get("x-csrf-token") and "authorization" not in headers and "__Host-edgf_session=" in headers.get("cookie", "")


def test_reject_requires_a_reason(session_of, api_as):
    approval_id = new_request(api_as)
    s = session_of(OWNER)
    open_operation(s, approval_id, "reject")
    before = len([r for r in s.requests if r.method == "POST"])
    s.page.click("#decision-submit")
    expect(s.page.locator(".form-error")).to_contain_text("却下の理由")
    assert len([r for r in s.requests if r.method == "POST"]) == before  # 送らない
    assert s.page.evaluate("() => document.activeElement.id") == "field-comment"
    s.page.fill("#field-comment", "目的が不明確です")
    s.page.click("#decision-submit")
    expect(s.page.locator("#inbox-status")).to_contain_text("却下しました")
    [event] = audit_of(api_as, "approval.decision", approval_id)
    assert event["payload"]["approve"] is False and event["payload"]["comment"] == "目的が不明確です"


def test_approve_with_keyboard_only(browser, idp, app_url, seed, api_as):
    approval_id = new_request(api_as)
    s = Session(browser, idp, app_url, OWNER).open()
    page = s.page
    try:
        page.focus("#login")
        page.keyboard.press("Enter")
        page.wait_for_selector(".site-nav")
        s.go("#/inbox", "#main tbody tr").settled()
        target = row(page, approval_id).locator("button[data-op=approve]")
        for _ in range(80):
            page.keyboard.press("Tab")
            if page.evaluate("(el) => document.activeElement === el", target.element_handle()):
                break
        else:
            pytest.fail("承認のボタンにキーボードで届きませんでした")
        page.keyboard.press("Enter")
        expect(page.locator("#decision-form")).to_be_visible()
        assert page.evaluate("() => document.activeElement.id") == "field-comment"  # フォームの最初の項目へ移る
        page.keyboard.type("キーボードで承認")
        page.keyboard.press("Tab")
        assert page.evaluate("() => document.activeElement.id") == "decision-submit"
        page.keyboard.press("Enter")
        expect(page.locator("#inbox-status")).to_contain_text("承認しました")
        assert audit_of(api_as, "approval.decision", approval_id)[0]["payload"]["channel"] == "ui"
    finally:
        s.close()


def test_escape_cancels_and_returns_focus(session_of, api_as):
    approval_id = new_request(api_as)
    s = session_of(OWNER)
    open_operation(s, approval_id, "reject")
    s.page.keyboard.press("Escape")
    expect(s.page.locator("#decision-form")).to_have_count(0)
    assert s.page.evaluate("() => document.activeElement.dataset.op") == "reject"


# ---------------------------------------------------------------- ステップアップ
def test_step_up_logs_in_again_and_returns(browser, idp, app_url, seed, api_as):
    approval_id = new_request(api_as)
    idp.auth_time_offset = 3600  # 1 時間前にログインしたことにする
    s = Session(browser, idp, app_url, OWNER)
    try:
        s.login()
        open_operation(s, approval_id, "approve")
        requests_before = len(idp.authorize_requests)
        with s.page.expect_navigation(url="**/ui/#/inbox"):  # 401 step-up-required → max_age=0 で再ログイン → 承認箱に戻る
            s.page.click("#decision-submit")
        s.page.wait_for_selector(".site-nav")
        assert len(idp.authorize_requests) == requests_before + 1 and idp.authorize_requests[-1]["max_age"] == "0"
        assert audit_of(api_as, "approval.decision", approval_id) == []  # まだ判断していない
        s.settled()
        row(s.page, approval_id).locator("button[data-op=approve]").click()
        s.page.click("#decision-submit")
        expect(s.page.locator("#inbox-status")).to_contain_text("承認しました")
        assert audit_of(api_as, "approval.decision", approval_id)[0]["payload"]["channel"] == "ui"
    finally:
        idp.auth_time_offset = 0
        s.close()


# ---------------------------------------------------------------- 別オリジン
def test_write_from_another_origin_is_rejected(session_of, api_as, idp, app_url):
    approval_id = new_request(api_as)
    s = session_of(OWNER)
    statuses = []
    s.context.on("response", lambda r: statuses.append(r.status) if f"/approvals/{approval_id}/decision" in r.url else None)
    other = s.context.new_page()
    other.goto(f"{idp.issuer}/jwks")  # 別のオリジン (ポートが違う) のページ
    other.evaluate("""(url) => fetch(url, {method: 'POST', mode: 'no-cors', credentials: 'include',
                                       headers: {'Content-Type': 'text/plain'}, body: '{"approve": true}'}).catch(() => null)""",
                   f"{app_url['url']}/api/v1/approvals/{approval_id}/decision")
    deadline = time.time() + 10
    while not statuses and time.time() < deadline:
        other.wait_for_timeout(100)
    assert statuses and all(code == 403 for code in statuses)
    assert audit_of(api_as, "approval.decision", approval_id) == []
    req = api_as(OWNER).get(f"/approvals/{approval_id}").json()
    assert req["status"] == "pending"


# ---------------------------------------------------------------- Policy の変更 (Phase 16 の未適用の Policy を有効にする手順)
def test_policy_change_is_approved_and_published_in_ui(session_of, api_as):
    proposer, admin = api_as(PROPOSER), api_as(ADMIN)
    active = admin.get("/policies/bundles", params={"status": "active"}).json()[0]
    files = admin.get(f"/policies/bundles/{active['version']}").json()["files"]
    # 既存の環境を再現する: LIN-002 の無い版を公開しておく (API の経路)
    doc = yaml.safe_load(files["global.yaml"])
    doc["policies"] = [p for p in doc["policies"] if p["id"] != "LIN-002"]
    r = proposer.post("/policies/bundles", json={"changes": {"global.yaml": yaml.safe_dump(doc, allow_unicode=True)}, "reason": "旧版の再現"})
    assert r.status_code == 202, r.text
    old = r.json()
    assert api_as(COMMITTEE).post(f"/approvals/{old['approval_id']}/decision", json={"approve": True}).status_code == 200
    assert api_as(COMMITTEE).post(f"/policies/bundles/{old['proposal_version']}/publish").status_code == 200
    # CDE のカラムを持ち、上流があるのにカラム単位のリネージが無い資産
    name = f"sales.cde_{uuid.uuid4().hex[:6]}"
    a = admin.post("/assets", json={"qualified_name": name, "name": name, "description": "d", "domain": "sales", "data_class": "analytical",
                                    "sensitivity": "internal", "owner": "asset-owner", "schema": [{"name": "amount", "cde": True}]}).json()
    assert admin.post("/lineage/edges", json={"source": "sales.public", "target": name}).status_code == 201

    def lin002():
        results = admin.get(f"/policies/evaluate/assets/{a['id']}").json()["results"]
        return [r["status"] for r in results if r["policy_id"] == "LIN-002"]
    assert lin002() == []  # 未適用
    # ディスクの内容 (LIN-002 を含む) を reload で提案し、Committee が UI で承認して公開する
    r = proposer.post("/policies/reload")
    assert r.status_code == 202, r.text
    proposal = r.json()
    s = session_of(COMMITTEE)
    open_operation(s, proposal["approval_id"], "approve")
    s.page.click("#decision-submit")
    expect(s.page.locator("#inbox-status")).to_contain_text("承認しました")
    publish_id = f"policy_bundle:{proposal['proposal_version']}"
    s.settled()
    row(s.page, publish_id).locator("button[data-op=publish]").click()
    s.page.click("#decision-submit")
    expect(s.page.locator("#inbox-status")).to_contain_text("公開しました")
    assert admin.get("/policies/bundles", params={"status": "active"}).json()[0]["version"] == proposal["proposal_version"]
    assert lin002() == ["fail"]
    [event] = audit_of(api_as, "policy.publish", str(proposal["proposal_version"]))
    assert event["payload"]["channel"] == "ui" and event["actor"] == "chair"
