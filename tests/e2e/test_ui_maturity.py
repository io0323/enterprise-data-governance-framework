"""Phase 18e: 成熟度の画面の E2E (自己評価の CSRF、判定のステップアップ、キーボード、目安と正式な評価の区別)."""
from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN, COMMITTEE, Session

pytestmark = pytest.mark.e2e


def new_domain(api_as) -> tuple[str, dict, dict]:
    """ドメインを作り、評価を起票して自動の判定まで進める。戻り値は (ドメイン、評価、そのドメインの Owner の利用者)。"""
    name = f"d{uuid.uuid4().hex[:8]}"
    assert api_as(ADMIN).post("/domains", json={"name": name, "owner": "dom-owner"}).status_code == 201
    chair = api_as(COMMITTEE)
    a = chair.post("/maturity/assessments", json={"domain": name})
    assert a.status_code == 201, a.text
    evaluated = chair.post(f"/maturity/assessments/{a.json()['id']}/evaluate")
    assert evaluated.status_code == 200, evaluated.text
    return name, evaluated.json(), {"sub": "dom-owner", "roles": ["owner"], "domains": [name], "clearance": "confidential"}


def status_of(api_as, assessment_id) -> str:
    return api_as(ADMIN).get(f"/maturity/assessments/{assessment_id}").json()["status"]


def test_indicative_and_formal_are_shown_separately(session_of, api_as):
    name, _, _ = new_domain(api_as)
    s = session_of(ADMIN)
    s.go("#/maturity", ".maturity-formal")
    s.settled()
    main = s.page.inner_text("main")
    assert "目安" in main and "正式な評価" in main and "全ドメインが到達しているレベル" in main
    assert s.page.locator(".maturity-indicative").count() >= 1 and s.page.locator(".maturity-formal").count() >= 1
    s.go(f"#/maturity/{name}", "#self-assessment-form")
    s.settled()
    labels = s.page.locator("dl.definitions dt").all_inner_texts()
    assert "正式な評価" in labels and "目安" in labels


def test_self_assessment_requires_csrf(browser, idp, app_url, seed, api_as):
    name, assessment, owner = new_domain(api_as)
    s = Session(browser, idp, app_url, owner)
    try:
        s.login()
        s.go(f"#/maturity/{name}", "#self-assessment-form")
        s.settled()
        s.page.check("#achieved-L1-COMMITTEE")
        s.page.fill("#uri-L1-COMMITTEE", "https://docs.example.com/committee")
        # 1 回目: X-CSRF-Token を外して送る → 403
        def strip(route):
            headers = {k: v for k, v in route.request.headers.items() if k.lower() != "x-csrf-token"}
            route.continue_(headers=headers)
        s.page.route("**/self-assessment", strip)
        s.page.click("#self-assessment-submit")
        expect(s.page.locator("#self-assessment-form .form-error")).to_contain_text("X-CSRF-Token")
        assert status_of(api_as, assessment["id"]) == "auto_evaluated"
        # 2 回目: 通常どおり (actions.js がトークンを付ける)
        s.page.unroute("**/self-assessment")
        s.page.click("#self-assessment-submit")
        expect(s.page.locator("#self-assessment-status")).to_contain_text("提出しました")
        assert status_of(api_as, assessment["id"]) == "self_assessed"
        [put] = [r for r in s.requests if r.method == "PUT" and r.url.endswith("/self-assessment")][-1:]
        assert put.all_headers().get("x-csrf-token") and "authorization" not in put.all_headers()
    finally:
        s.close()


def test_self_assessment_with_keyboard_only(browser, idp, app_url, seed, api_as):
    name, assessment, owner = new_domain(api_as)
    s = Session(browser, idp, app_url, owner)
    page = s.page
    try:
        s.open()
        page.focus("#login")
        page.keyboard.press("Enter")
        page.wait_for_selector(".site-nav")
        s.go(f"#/maturity/{name}", "#self-assessment-form")
        s.settled()
        for _ in range(120):
            page.keyboard.press("Tab")
            if page.evaluate("() => document.activeElement?.id") == "achieved-L1-COMMITTEE":
                break
        else:
            pytest.fail("自己評価の項目にキーボードで届きませんでした")
        page.keyboard.press("Space")
        page.keyboard.press("Tab")
        assert page.evaluate("() => document.activeElement.id") == "uri-L1-COMMITTEE"
        page.keyboard.type("https://docs.example.com/committee")
        page.keyboard.press("Tab")
        page.keyboard.type("Committee の設置の議事録")
        for _ in range(80):
            page.keyboard.press("Tab")
            if page.evaluate("() => document.activeElement?.id") == "self-assessment-submit":
                break
        page.keyboard.press("Enter")
        expect(page.locator("#self-assessment-status")).to_contain_text("提出しました")
        body = api_as(ADMIN).get(f"/maturity/assessments/{assessment['id']}").json()
        [item] = [i for i in body["self_assessment"] if i["criterion_id"] == "L1-COMMITTEE"]
        assert item["achieved"] is True and item["evidence"][0]["uri"] == "https://docs.example.com/committee"
    finally:
        s.close()


def test_committee_decision_requires_step_up(browser, idp, app_url, seed, api_as):
    name, assessment, owner = new_domain(api_as)
    owner_api = api_as(owner)
    r = owner_api.put(f"/maturity/assessments/{assessment['id']}/self-assessment", json={"items": [
        {"criterion_id": "L1-COMMITTEE", "achieved": True}, {"criterion_id": "L1-OWNER-APPOINT", "achieved": True}]})
    assert r.status_code == 200, r.text
    approval_id = r.json()["approval_id"]
    idp.auth_time_offset = 3600
    s = Session(browser, idp, app_url, COMMITTEE)
    try:
        s.login()
        s.go("#/inbox", ".op-buttons")
        s.settled()
        group = s.page.locator(f'.op-buttons[data-item="{approval_id}"]')
        group.locator("button[data-op=decide]").click()
        s.page.select_option("#field-level", "0")
        with s.page.expect_navigation(url="**/ui/#/inbox"):  # step-up-required → max_age=0 で再ログイン → 承認箱に戻る
            s.page.click("#decision-submit")
        s.page.wait_for_selector(".site-nav")
        assert idp.authorize_requests[-1]["max_age"] == "0"
        assert status_of(api_as, assessment["id"]) == "self_assessed"
        s.settled()
        s.page.locator(f'.op-buttons[data-item="{approval_id}"] button[data-op=decide]').click()
        s.page.select_option("#field-level", "0")
        s.page.fill("#field-reason", "手動の基準の証拠が不足")
        s.page.click("#decision-submit")
        expect(s.page.locator("#inbox-status")).to_contain_text("判定しました")
        body = api_as(ADMIN).get(f"/maturity/assessments/{assessment['id']}").json()
        assert body["status"] == "decided" and body["decided_level"] == 0 and body["decided_by"] == "chair"
        s.go(f"#/maturity/{name}", "dl.definitions")
        s.settled()
        assert "判定済み" in s.page.inner_text("main")
    finally:
        idp.auth_time_offset = 0
        s.close()
