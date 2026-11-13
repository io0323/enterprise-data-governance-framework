"""Phase 19f: 自律統治の画面の E2E (提案の確定のステップアップ、停止はすぐに効く、取り消し、キーボードだけの操作)."""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import datetime, timedelta

import pytest
import yaml
from playwright.sync_api import expect
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DbSession

from edgf.models import AccessGrant
from tests.e2e.conftest import ADMIN, COMMITTEE, ROOT, Session

pytestmark = pytest.mark.e2e
VICE_CHAIR = {"sub": "vice-chair", "roles": ["committee"]}
AUTO_OWNER = {"sub": "auto-owner", "roles": ["owner"], "domains": ["autonomy"], "clearance": "confidential"}
AUTO_STEWARD = {"sub": "auto-steward", "roles": ["steward"], "domains": ["autonomy"], "clearance": "confidential"}
PLAYBOOKS = {"playbooks": [
    {"id": "PB-CLASSIFICATION", "trigger": {"detector": "classification_scan"}, "condition": {"min_confidence": 0.6},
     "action": "propose.classification", "category": "B", "mode": "active", "max_actions_per_run": 100},
    {"id": "PB-ACCESS-STALE", "trigger": {"detector": "stale_access"}, "condition": {"expired": True, "asset_gone": False},
     "action": "access.revoke_stale", "category": "A", "mode": "active", "max_actions_per_run": 100, "undo_days": 30,
     "false_positive_threshold": 1.0},
]}


def run_autonomy(app_url) -> dict:
    out = subprocess.run([sys.executable, "-m", "edgf.jobs", "run", "autonomy_run"], cwd=ROOT,
                         env={**app_url["env"], "EDGF_AUTONOMY_ENABLED": "true"}, check=True, capture_output=True, text=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def active_playbooks(api_as, seed):
    """プレイブックを active にする Policy 変更を、提案 → Committee の承認 → 公開の順に通す (ADR §6)。"""
    admin, chair = api_as(ADMIN), api_as(COMMITTEE)
    if admin.get("/domains/autonomy").status_code == 404:
        assert admin.post("/domains", json={"name": "autonomy", "owner": "auto-owner"}).status_code == 201
    r = chair.post("/policies/bundles", json={"changes": {"remediation/playbooks.yaml": yaml.safe_dump(PLAYBOOKS)}, "reason": "E2E"})
    assert r.status_code == 202, r.text
    proposal = r.json()
    assert api_as(VICE_CHAIR).post(f"/approvals/{proposal['approval_id']}/decision", json={"approve": True, "comment": ""}).status_code == 200
    assert chair.post(f"/policies/bundles/{proposal['proposal_version']}/publish").status_code == 200
    return True


def new_asset(api_as, columns) -> dict:
    qn = f"autonomy.a{uuid.uuid4().hex[:8]}"
    r = api_as(ADMIN).post("/assets", json={"qualified_name": qn, "name": qn, "description": "d", "domain": "autonomy",
                                            "data_class": "transactional", "sensitivity": "internal", "owner": "auto-owner",
                                            "steward": "auto-steward", "custodian": "custodian1",
                                            "controls": ["encryption_at_rest", "encryption_in_transit", "masking"], "schema": columns})
    assert r.status_code == 201, r.text
    return r.json()


def suggestion_of(api_as, asset_id) -> dict:
    [s] = api_as(COMMITTEE).get("/autonomy/suggestions", params={"asset_id": asset_id}).json()
    return s


def test_suggestion_confirmation_requires_step_up(browser, idp, app_url, seed, api_as, active_playbooks):
    a = new_asset(api_as, [{"name": "customer_email", "type": "string"}])
    run_autonomy(app_url)
    s_id = suggestion_of(api_as, a["id"])["id"]
    assert suggestion_of(api_as, a["id"])["status"] == "proposed"
    idp.auth_time_offset = 3600
    s = Session(browser, idp, app_url, AUTO_STEWARD)
    try:
        s.login()
        s.go("#/autonomy", "#main table")
        s.settled()
        s.page.locator(f'.op-buttons[data-item="{s_id}"] button[data-op=adopt]').click()
        expect(s.page.locator("#decision-form")).to_contain_text("承認の操作です")
        with s.page.expect_navigation(url="**/ui/#/autonomy"):  # step-up-required → max_age=0 で再ログイン → 画面に戻る
            s.page.click("#decision-submit")
        s.page.wait_for_selector(".site-nav")
        assert idp.authorize_requests[-1]["max_age"] == "0"
        assert suggestion_of(api_as, a["id"])["status"] == "proposed"  # まだ確定していない
        s.settled()
        s.page.locator(f'.op-buttons[data-item="{s_id}"] button[data-op=adopt]').click()
        s.page.fill("#field-reason", "メールアドレスです")
        s.page.click("#decision-submit")
        expect(s.page.locator("#autonomy-status")).to_contain_text("確定")
        body = suggestion_of(api_as, a["id"])
        assert body["status"] == "accepted" and body["decided_by"] == "auto-steward"
        column = api_as(ADMIN).get(f"/assets/{a['id']}").json()["schema"][0]
        assert column["pii"] is True and column["pii_category"] == "contact"
        [write] = [r for r in s.requests if r.method == "POST" and r.url.endswith(f"/suggestions/{s_id}/decision")][-1:]
        assert write.all_headers().get("x-csrf-token") and "authorization" not in write.all_headers()
    finally:
        idp.auth_time_offset = 0
        s.close()


def test_stop_takes_effect_immediately_and_resume_needs_step_up(browser, idp, app_url, seed, api_as, active_playbooks):
    s = Session(browser, idp, app_url, COMMITTEE)
    try:
        s.login()
        s.go("#/autonomy", "#main table")
        s.settled()
        s.page.locator('.op-buttons[data-item="control"] button[data-op=stop]').click()
        s.page.fill("#field-reason", "E2E の停止")
        s.page.click("#decision-submit")
        expect(s.page.locator("#autonomy-status")).to_contain_text("停止")
        assert api_as(COMMITTEE).get("/autonomy/status").json()["stopped"] is True
        assert run_autonomy(app_url)["result"] == {"status": "stopped"}  # 次の実行はすぐに止まる
        # 再開は ✓ の操作: ログインから時間がたっていれば再ログインを求める
        idp.auth_time_offset = 3600
        s.close()
        s = Session(browser, idp, app_url, COMMITTEE)
        s.login()
        s.go("#/autonomy", "#main table")
        s.settled()
        s.page.locator('.op-buttons[data-item="control"] button[data-op=resume]').click()
        s.page.fill("#field-reason", "原因を取り除いた")
        with s.page.expect_navigation(url="**/ui/#/autonomy"):
            s.page.click("#decision-submit")
        s.page.wait_for_selector(".site-nav")
        assert api_as(COMMITTEE).get("/autonomy/status").json()["stopped"] is True
        s.settled()
        s.page.locator('.op-buttons[data-item="control"] button[data-op=resume]').click()
        s.page.fill("#field-reason", "原因を取り除いた")
        s.page.click("#decision-submit")
        expect(s.page.locator("#autonomy-status")).to_contain_text("再開")
        assert api_as(COMMITTEE).get("/autonomy/status").json()["stopped"] is False
    finally:
        idp.auth_time_offset = 0
        if api_as(COMMITTEE).get("/autonomy/status").json()["stopped"]:
            api_as(COMMITTEE).post("/autonomy/resume", json={"reason": "E2E の後始末"})
        s.close()


def _expired_grant(app_url, asset_id) -> str:
    engine = create_engine(app_url["env"]["EDGF_DATABASE_URL"])
    with DbSession(engine) as db:
        row = AccessGrant(asset_id=asset_id, subject=f"u{uuid.uuid4().hex[:6]}", purpose="分析", granted_by="auto-owner",
                          expires_at=datetime.utcnow() - timedelta(days=1))
        db.add(row)
        db.commit()
        grant_id = row.id
    engine.dispose()
    return grant_id


def test_undo_with_keyboard_only(browser, idp, app_url, seed, api_as, active_playbooks):
    a = new_asset(api_as, [{"name": "id", "type": "string"}])
    grant_id = _expired_grant(app_url, a["id"])
    run_autonomy(app_url)
    [run] = [r for r in api_as(COMMITTEE).get("/autonomy/runs", params={"asset_id": a["id"]}).json() if r["target_id"] == grant_id]
    assert run["status"] == "executed" and run["undoable"] is True
    s = Session(browser, idp, app_url, AUTO_OWNER).open()
    page = s.page
    try:
        page.focus("#login")
        page.keyboard.press("Enter")
        page.wait_for_selector(".site-nav")
        s.go("#/autonomy", "#main table").settled()
        target = page.locator(f'.op-buttons[data-item="{run["id"]}"] button[data-op=undo]')
        for _ in range(200):
            page.keyboard.press("Tab")
            if page.evaluate("(el) => document.activeElement === el", target.element_handle()):
                break
        else:
            pytest.fail("取り消しのボタンにキーボードで届きませんでした")
        page.keyboard.press("Enter")
        expect(page.locator("#decision-form")).to_be_visible()
        assert page.evaluate("() => document.activeElement.id") == "field-reason"
        page.keyboard.type("まだ利用しています")
        page.keyboard.press("Tab")
        assert page.evaluate("() => document.activeElement.id") == "decision-submit"
        page.keyboard.press("Enter")
        expect(page.locator("#autonomy-status")).to_contain_text("取り消し")
        body = api_as(COMMITTEE).get(f"/autonomy/runs/{run['id']}").json()
        assert body["status"] == "undone" and body["undone_by"] == "auto-owner" and body["undo_reason"] == "まだ利用しています"
    finally:
        s.close()


def test_read_views_show_dry_run_and_suggestions(session_of, api_as, active_playbooks):
    s = session_of(COMMITTEE)
    s.go("#/autonomy", "#main table")
    s.settled()
    main = s.page.inner_text("main")
    for text in ("全体の状態", "プレイブック", "分類の提案", "実行の記録", "品質の異常", "PB-CLASSIFICATION"):
        assert text in main, text
