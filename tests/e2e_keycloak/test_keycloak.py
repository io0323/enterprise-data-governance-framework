"""docker-compose.idp.yml の Keycloak との E2E (EDGF_E2E_KEYCLOAK に UI の URL を指定したときだけ実行する)."""
from __future__ import annotations

import os

import pytest

BASE = os.environ.get("EDGF_E2E_KEYCLOAK")
pytestmark = pytest.mark.skipif(not BASE, reason="EDGF_E2E_KEYCLOAK が未設定")
PASSWORD = "edgf-dev-only"


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


def _login(browser, username):
    context = browser.new_context(locale="ja-JP")
    requests = []
    context.on("request", lambda r: requests.append(r))
    page = context.new_page()
    page.goto(f"{BASE}/ui/")
    page.wait_for_selector("#login")
    page.click("#login")  # Keycloak のログインの画面に移動する (BFF。ポップアップは使わない)
    page.wait_for_selector("#username")
    page.fill("#username", username)
    page.fill("#password", PASSWORD)
    page.click("#kc-login")
    page.wait_for_selector(".site-nav", timeout=20000)
    return context, page, requests


@pytest.mark.parametrize("username,jobs_allowed", [("dg-admin", True), ("dg-committee", True), ("dg-owner", False), ("dg-steward", False),
                                                   ("dg-custodian", False), ("dg-consumer", False)])
def test_login_and_roles(browser, username, jobs_allowed):
    context, page, requests = _login(browser, username)
    try:
        assert username in page.inner_text("header")  # EDGF_CLAIM_SUBJECT = preferred_username
        page.wait_for_selector("#catalog-results")
        page.evaluate("() => { window.location.hash = '#/jobs'; }")
        page.wait_for_selector("text=outbox_deliver" if jobs_allowed else ".notice-denied")
        # 閲覧では自オリジンへ GET 以外を送らない。ブラウザは IdP のトークンエンドポイントと通信しない (交換は BFF が行う)
        non_get = [r.url for r in requests if r.method not in ("GET", "HEAD", "OPTIONS")]
        assert [u for u in non_get if u.startswith(BASE)] == [] and all("/login-actions/" in u for u in non_get)
        assert page.evaluate("() => document.cookie") == ""
    finally:
        context.close()


def test_logout_returns_to_login(browser):
    context, page, _ = _login(browser, "dg-consumer")
    try:
        page.click("text=ログアウト")
        # id_token_hint を持たないので、Keycloak はログアウトの確認の画面を出す
        page.wait_for_selector("#kc-logout, #login", timeout=20000)
        if page.locator("#kc-logout").count():
            page.click("#kc-logout")
        page.wait_for_selector("#login", timeout=20000)
        assert page.locator(".site-nav").count() == 0
    finally:
        context.close()
