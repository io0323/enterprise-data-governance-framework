"""Phase 13e: 実ブラウザでの E2E."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.e2e.conftest import ADMIN, COMMITTEE, CONSUMER, CUSTODIAN, OWNER, STEWARD, USERS, Session

pytestmark = pytest.mark.e2e
ROUTES = ["#/catalog", "#/glossary", "#/lineage/sales.internal", "#/dashboard", "#/products", "#/issues", "#/inbox", "#/jobs"]


def _catalog_names(session) -> set[str]:
    session.go("#/catalog", "#catalog-results")
    return set(session.page.locator("#catalog-results tbody tr td:first-child").all_inner_texts())


@pytest.mark.parametrize("role", sorted(USERS))
def test_catalog_matches_api_abac_for_each_role(session_of, api_as, role):
    user = USERS[role]
    expected = {a["qualified_name"] for a in api_as(user).get("/assets", params={"limit": 25}).json()}
    assert _catalog_names(session_of(user)) == expected


def test_restricted_asset_is_hidden_from_consumer(session_of, seed):
    s = session_of(CONSUMER)
    names = _catalog_names(s)
    assert "sales.restricted" not in names and "sales.confidential" not in names and "sales.internal" in names
    # 直接開いても「権限がありません」
    s.go(f"#/assets/{seed['assets']['restricted']['id']}", ".notice-denied")
    assert "権限がありません" in s.page.inner_text(".notice-denied")
    assert "sales.restricted" not in s.page.inner_text("main")


def test_committee_sees_restricted_and_badges(session_of, seed):
    s = session_of(COMMITTEE)
    assert "sales.restricted" in _catalog_names(s)
    row = s.page.locator("#catalog-results tbody tr", has_text="sales.restricted")
    assert "Restricted" in row.inner_text() and "Draft" in row.inner_text()


def test_only_get_requests_except_token_exchange(session_of, idp, app_url):
    s = session_of(ADMIN)
    for route in ROUTES:
        s.go(route).settled()
    assert {"/kpi", "/mesh/products/overview", "/approvals/inbox", "/ops/jobs", "/issues"} <= {r.url.split("/api/v1")[1].split("?")[0] for r in s.requests if "/api/v1" in r.url}
    non_get = [r for r in s.requests if r.method not in ("GET", "HEAD", "OPTIONS")]
    # 自オリジンへの GET 以外は 0 件。それ以外の POST はトークンエンドポイントの 1 件だけ
    assert [r for r in non_get if r.url.startswith(app_url["url"])] == []
    assert [r.url for r in non_get] == [idp.token_endpoint]
    # X-Actor は送らない。API には Bearer を付ける
    api_calls = [r for r in s.requests if "/api/v1/" in r.url]
    assert api_calls and all("x-actor" not in r.all_headers() for r in api_calls)
    assert all(r.all_headers().get("authorization", "").startswith("Bearer ") for r in api_calls)
    # 通信先は自身と IdP の issuer だけ
    assert {r.url.split("/")[2] for r in s.requests} <= {app_url["url"].split("/")[2], idp.issuer.split("/")[2]}
    assert not [m for m in s.console if "Content Security Policy" in m]


def test_token_is_kept_only_in_memory(session_of, idp):
    s = session_of(OWNER)
    stored = s.page.evaluate("() => [localStorage.length, sessionStorage.length, document.cookie]")
    assert stored == [0, 0, ""]
    assert s.context.cookies() == []
    # PKCE で交換している
    assert idp.token_requests[-1]["code_verifier"] and "client_secret" not in idp.token_requests[-1]
    # 再読み込みするとトークンは残らない
    s.page.reload()
    s.page.wait_for_selector("#login")
    assert s.page.locator(".site-nav").count() == 0


def test_login_rejects_wrong_state_and_requires_pkce(browser, idp, app_url, seed):
    s = Session(browser, idp, app_url, CONSUMER).open()
    try:
        # 別の state の応答は無視され、ログインにならない
        s.page.evaluate("() => { const c = new BroadcastChannel('edgf-oidc'); c.postMessage({code: 'forged', state: 'forged'}); c.close(); }")
        s.page.wait_for_timeout(200)
        assert s.page.locator(".site-nav").count() == 0
    finally:
        s.close()


def test_401_asks_to_log_in_again(session_of):
    s = session_of(CONSUMER)
    s.page.route("**/api/v1/issues*", lambda route: route.fulfill(status=401, content_type="application/problem+json", body='{"status":401}'))
    s.go("#/issues", "#login")
    assert "期限が切れました" in s.page.inner_text("main")
    # 再ログインすると同じ画面に戻る
    s.page.unroute("**/api/v1/issues*")
    with s.context.expect_page():
        s.page.click("#login")
    s.page.wait_for_selector(".site-nav")
    assert s.page.locator("main h1").inner_text() == "Issue"


@pytest.mark.parametrize("user,allowed", [(ADMIN, True), (COMMITTEE, True), (CONSUMER, False), (OWNER, False), (CUSTODIAN, False)])
def test_jobs_screen_follows_api_permission(session_of, user, allowed):
    s = session_of(user)
    s.go("#/jobs")
    if allowed:
        s.page.wait_for_selector("text=outbox_deliver")
    else:
        s.page.wait_for_selector(".notice-denied")
        assert "権限がありません" in s.page.inner_text(".notice-denied")


def test_data_is_shown_as_text_not_markup(session_of, seed):
    s = session_of(ADMIN)
    s.go(f"#/assets/{seed['assets']['xss']['id']}", "text=Business Metadata")
    s.settled()
    assert "<img src=x" in s.page.inner_text("main")
    assert s.page.locator("main img").count() == 0 and s.page.evaluate("() => window.__xss") is None


def test_asset_detail_sections(session_of, seed):
    s = session_of(OWNER)
    s.go(f"#/assets/{seed['assets']['internal']['id']}", "text=Business Metadata")
    s.settled()
    text = s.page.inner_text("main")
    for expected in ("asset-owner", "steward1", "custodian1", "order_id", "PII", "受注", "GOV-001", "completeness", "保持期間"):
        assert expected in text, expected
    assert s.page.locator("svg.chart polyline").count() == 1


def test_glossary_list_and_detail(session_of, seed):
    s = session_of(CONSUMER)
    s.go("#/glossary", "text=顧客からの注文")
    s.page.click("main table a")
    s.page.wait_for_selector("text=紐付く資産")
    assert "sales.internal" in s.page.inner_text("main")


def test_lineage_graph_and_redaction(session_of):
    s = session_of(CONSUMER)
    s.go("#/lineage/sales.internal", "svg.lineage")
    s.page.wait_for_selector("text=通知先の Owner")
    svg = s.page.locator("svg.lineage").text_content()
    assert "sales.public" in svg and "ext.unregistered" in svg and "sales.restricted" in svg
    assert "閲覧権限なし" in svg and "未登録" in svg and "CDE" in svg
    hidden = s.page.locator("svg.lineage .node-redacted")
    assert hidden.count() == 1 and s.page.locator("svg.lineage a[href*='sales.restricted']").count() == 0
    # 深さの切り替え
    s.page.select_option("select[name=depth]", "1")
    s.page.click("main form button")
    s.page.wait_for_function("() => window.location.hash.includes('depth=1')")
    s.page.wait_for_selector("svg.lineage")
    # 伏せたノードは表にも詳細のリンクを出さない
    assert s.page.locator("main table a", has_text="sales.restricted").count() == 0


def test_dashboard(session_of):
    s = session_of(CONSUMER)
    s.go("#/dashboard", "text=全社の KPI と目標")
    s.page.wait_for_selector("text=現在のレベル")
    s.settled()
    text = s.page.inner_text("main")
    for expected in ("Owner 割当率", "目標", "未計測", "AI の KPI", "Data Mesh の KPI", "Privacy の KPI", "sales"):
        assert expected in text, expected
    assert s.page.locator("svg.chart").count() == 2


def test_products_issues_inbox(session_of):
    s = session_of(OWNER)
    s.go("#/products", "text=orders")
    assert "MESH-001" in s.page.inner_text("main")
    s.go("#/issues", "text=E2E の Issue")
    s.go("#/inbox", "code")
    assert "POST /api/v1/approvals/" in s.page.inner_text("main") and "読み取り専用" in s.page.inner_text("main")


def test_pagination_uses_headers(session_of, api_as):
    s = session_of(ADMIN)
    s.page.route("**/api/v1/assets?*", lambda route: route.continue_(url=route.request.url.replace("limit=25", "limit=2")))
    s.go("#/catalog", "#catalog-results")
    pager = s.page.locator(".pager")
    assert "全" in pager.inner_text() and pager.locator("button").nth(0).is_disabled()
    pager.locator("button").nth(1).click()
    s.page.wait_for_function("() => window.location.hash.includes('offset=2')")


def test_language_switch(session_of):
    s = session_of(CONSUMER)
    s.go("#/catalog", "#catalog-results")
    s.page.click("button[lang=en]")
    s.page.wait_for_selector("text=Data catalog")
    assert s.page.evaluate("() => document.documentElement.lang") == "en"


def test_logout_clears_session(session_of):
    s = session_of(CONSUMER)
    s.page.click("text=ログアウト")
    s.page.wait_for_selector("#login")
    assert s.page.locator(".site-nav").count() == 0


# ---------------------------------------------------------------- キーボード
def _tab_until(page, predicate, limit=60):
    for _ in range(limit):
        page.keyboard.press("Tab")
        if page.evaluate(predicate):
            return
    pytest.fail(f"focus did not reach: {predicate}")


def test_keyboard_only_flow(browser, idp, app_url, seed):
    s = Session(browser, idp, app_url, STEWARD).open()
    page = s.page
    try:
        # ログインもキーボードだけで
        _tab_until(page, "() => document.activeElement?.id === 'login'")
        with s.context.expect_page():
            page.keyboard.press("Enter")
        page.wait_for_selector(".site-nav")
        # スキップリンク
        page.keyboard.press("Tab")
        assert page.evaluate("() => document.activeElement.className") == "skip-link"
        assert page.evaluate("() => getComputedStyle(document.activeElement).outlineStyle") != "none"
        page.keyboard.press("Enter")
        assert page.evaluate("() => document.activeElement.id") == "main"
        # 検索して結果を開く
        _tab_until(page, "() => document.activeElement?.id === 'catalog-q'")
        page.keyboard.type("sales.internal")
        page.keyboard.press("Enter")
        page.wait_for_function("() => window.location.hash.includes('q=sales.internal')")
        page.wait_for_selector("#catalog-results a")
        _tab_until(page, "() => document.activeElement?.textContent === 'sales.internal'")
        page.keyboard.press("Enter")
        page.wait_for_selector("text=Business Metadata")
        assert page.evaluate("() => document.activeElement.id") == "main"  # 画面の切り替えで本文に移動する
        # ナビゲーションで別の画面へ
        page.evaluate("() => { document.activeElement.blur(); window.scrollTo(0, 0); document.querySelector('.skip-link').focus(); }")
        _tab_until(page, "() => document.activeElement?.getAttribute('href') === '#/dashboard'")
        page.keyboard.press("Enter")
        page.wait_for_selector("text=全社の KPI と目標")
        assert page.locator(".site-nav a[aria-current=page]").inner_text() == "ダッシュボード"
    finally:
        s.close()


def test_landmarks_and_labels(session_of):
    s = session_of(CONSUMER)
    s.go("#/catalog", "#catalog-results")
    page = s.page
    assert page.locator("header").count() == 1 and page.locator("nav.site-nav").count() == 1 and page.locator("main").count() == 1
    unlabeled = page.evaluate("""() => [...document.querySelectorAll('input, select')].filter(e => !e.closest('label')).length""")
    assert unlabeled == 0
    assert page.locator("table caption").count() == 1 and page.locator("th[scope=col]").count() > 0
    assert page.evaluate("() => document.documentElement.lang") == "ja" and "カタログ" in page.title()


# ---------------------------------------------------------------- スクリーンショット
@pytest.mark.skipif(not os.environ.get("EDGF_UI_SCREENSHOTS"), reason="EDGF_UI_SCREENSHOTS を指定したときだけ撮る")
def test_capture_screenshots(session_of, seed):
    out = Path(os.environ["EDGF_UI_SCREENSHOTS"])
    out.mkdir(parents=True, exist_ok=True)
    s = session_of(COMMITTEE)
    shots = {"catalog": "#/catalog", "asset": f"#/assets/{seed['assets']['internal']['id']}", "glossary": "#/glossary", "lineage": "#/lineage/sales.internal",
             "dashboard": "#/dashboard", "products": "#/products", "issues": "#/issues", "inbox": "#/inbox", "jobs": "#/jobs"}
    for name, route in shots.items():
        s.go(route).settled()
        s.page.screenshot(path=str(out / f"{name}.png"), full_page=True)
