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
    with context.expect_page() as popup_info:
        page.click("#login")
    popup = popup_info.value
    popup.wait_for_selector("#username")
    popup.fill("#username", username)
    popup.fill("#password", PASSWORD)
    popup.click("#kc-login")
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
        non_get = [r.url for r in requests if r.method not in ("GET", "HEAD", "OPTIONS")]
        same_origin = [u for u in non_get if u.startswith(BASE)]
        assert same_origin == [] and all(u.endswith("/protocol/openid-connect/token") or "/login-actions/" in u for u in non_get)
    finally:
        context.close()


def test_logout_returns_to_login(browser):
    context, page, _ = _login(browser, "dg-consumer")
    try:
        page.click("text=ログアウト")
        page.wait_for_selector("#login", timeout=20000)
        assert page.locator(".site-nav").count() == 0
    finally:
        context.close()
