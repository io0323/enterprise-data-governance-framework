"""Phase 13e: 実ブラウザでの E2E."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN, COMMITTEE, CONSUMER, CUSTODIAN, OWNER, STEWARD, USERS, Session, transition

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
    """ログインの応答を待っている間に、別の state の応答を送る。

    受け手 (state を照合するチャンネル) はログインの開始後にしか無いので、IdP の画面を保留して応答待ちの状態を作る。
    """
    s = Session(browser, idp, app_url, CONSUMER).open()
    held = []
    s.context.route(f"{idp.issuer}/authorize*", lambda route: held.append(route))
    try:
        with s.context.expect_event("request", lambda request: "/authorize" in request.url):
            s.page.click("#login")
        expect(s.page.locator(".login [role=status]")).to_have_text("別のウィンドウでログインしてください。")
        exchanges = len(idp.token_requests)
        # 同じチャンネルのメッセージは送った順に処理されるので、目印が届いた時点で偽の応答の処理は終わっている
        s.page.evaluate("""() => new Promise((resolve) => {
            const probe = new BroadcastChannel('edgf-oidc');
            probe.onmessage = (event) => { if (event.data.probe) { probe.close(); resolve(); } };
            const sender = new BroadcastChannel('edgf-oidc');
            sender.postMessage({code: 'forged', state: 'forged'});
            sender.postMessage({probe: true});
            sender.close();
        })""")
        assert len(idp.token_requests) == exchanges  # 偽の応答ではトークンの交換をしない
        assert s.page.locator(".site-nav").count() == 0
        expect(s.page.locator(".login [role=status]")).to_have_text("別のウィンドウでログインしてください。")
        # 本物の応答はまだ受け付ける (受け手がいたことの確認)
        held.pop().continue_()
        expect(s.page.locator(".site-nav")).to_be_visible()
        assert idp.token_requests[-1]["code_verifier"] and "client_secret" not in idp.token_requests[-1]
        assert len(idp.token_requests) == exchanges + 1
    finally:
        for route in held:
            route.continue_()
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
    assert "sales.public" in svg and "ext.unregistered" in svg and "sales.restricted" not in svg
    assert "閲覧権限なし" in svg and "未登録" in svg and "CDE" in svg
    hidden = s.page.locator("svg.lineage .node-redacted")
    assert hidden.count() == 1 and "sales.restricted" not in s.page.inner_text("main")
    # 伏せたノードは表にも詳細のリンクを出さない
    assert s.page.locator("main table a", has_text="sales.restricted").count() == 0


def _graph_names(page) -> list[str]:
    """グラフのノードの名前 (title の先頭)。"""
    return sorted(t.split(" / ")[0] for t in page.locator("svg.lineage g.node > title").all_text_contents())


def test_lineage_depth_switch_shows_the_new_graph(session_of, api_as):
    """深さの切り替え。sales.public は深さ 1 と 3 でノードが変わる (sales.internal は上下 1 段ずつで変わらない)。"""
    api = api_as(CONSUMER)
    deep, shallow = (api.get("/lineage/sales.public/graph", params={"depth": d}).json() for d in (3, 1))
    deep_names, shallow_names = (sorted(n["qualified_name"] for n in g["nodes"]) for g in (deep, shallow))
    assert len(deep_names) > len(shallow_names) and "ext.unregistered" in deep_names and "ext.unregistered" not in shallow_names  # 前提
    s = session_of(CONSUMER)
    s.go("#/lineage/sales.public", "svg.lineage")
    assert _graph_names(s.page) == deep_names
    s.page.select_option("select[name=depth]", "1")
    transition(s.page, lambda: s.page.click("main form button"), "svg.lineage")
    # 切り替えの後でしか成り立たない条件
    assert "depth=1" in s.page.evaluate("() => window.location.hash")
    assert _graph_names(s.page) == shallow_names
    assert "ext.unregistered" not in s.page.locator("svg.lineage").text_content()
    assert s.page.locator("svg.lineage").get_attribute("aria-label").count(f"ノード {len(shallow_names)} 件") == 1
    assert s.page.locator("select[name=depth]").input_value() == "1"
    distances = s.page.locator("main table").first.locator("tbody tr td:nth-child(3)").all_inner_texts()
    assert distances and max(int(d) for d in distances) == 1


def test_dashboard(session_of):
    s = session_of(CONSUMER)
    s.go("#/dashboard", "text=全社の KPI と目標")
    s.page.wait_for_selector("text=現在のレベル")
    s.settled()
    text = s.page.inner_text("main")
    for expected in ("Owner 割当率", "目標", "未計測", "AI の KPI", "Data Mesh の KPI", "Privacy の KPI", "sales"):
        assert expected in text, expected
    assert s.page.locator("svg.chart").count() == 2
    # 線種の凡例で、どの線がどの指標か分かる
    assert s.page.locator(".chart-legend li").count() == 6 and "Policy 準拠率" in s.page.locator(".chart-legend").first.inner_text()


def test_products_issues_inbox(session_of):
    s = session_of(OWNER)
    s.go("#/products", "text=orders")
    assert "MESH-001" in s.page.inner_text("main")
    s.go("#/issues", "text=E2E の Issue")
    s.go("#/inbox", "code")
    assert "POST /api/v1/approvals/" in s.page.inner_text("main") and "読み取り専用" in s.page.inner_text("main")


def test_pagination_uses_headers(session_of, api_as):
    s = session_of(ADMIN)
    api = api_as(ADMIN)
    first, second = ([a["qualified_name"] for a in api.get("/assets", params={"limit": 2, "offset": o}).json()] for o in (0, 2))
    assert first and second and set(first).isdisjoint(second)  # 前提: 2 ページ目は別の行
    s.page.route("**/api/v1/assets?*", lambda route: route.continue_(url=route.request.url.replace("limit=25", "limit=2")))
    s.go("#/catalog", "#catalog-results")
    rows = s.page.locator("#catalog-results tbody tr td:first-child")
    assert rows.all_inner_texts() == first
    pager = s.page.locator(".pager")
    assert "全" in pager.inner_text() and pager.locator("button").nth(0).is_disabled()
    transition(s.page, lambda: pager.locator("button").nth(1).click(), "#catalog-results")
    # 2 ページ目の内容に変わったこと
    assert "offset=2" in s.page.evaluate("() => window.location.hash")
    assert rows.all_inner_texts() == second
    assert s.page.locator(".pager button").nth(0).is_enabled()


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
        # ハッシュは送信と同時に変わり、再描画はそのあとの hashchange で起きる。リンクの有無だけで待つと、検索前の一覧で成立してしまう
        expect(page.locator("#catalog-results a")).to_have_text(["sales.internal"])
        expect(page.locator("#main")).to_be_focused()
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


def test_search_keeps_focus_on_main_while_results_load(browser, idp, app_url, seed):
    """検索の応答を止めて、読み込み中の状態を固定する。古い待ち方は、この状態で Tab を使い切って失敗していた。"""
    s = Session(browser, idp, app_url, STEWARD).login()
    page = s.page
    held = []
    try:
        expect(page.locator("#catalog-results a")).not_to_have_count(0)  # 検索前の一覧
        assert page.locator("#catalog-results a").count() > 1
        page.route("**/api/v1/assets?*q=sales.internal*", lambda route: held.append(route))
        page.fill("#catalog-q", "sales.internal")
        with page.expect_request(lambda request: "q=sales.internal" in request.url):
            page.press("#catalog-q", "Enter")
        # 検索前の一覧は残らず、読み込み中のあいだフォーカスは本文にある
        expect(page.locator("#catalog-results")).to_have_count(0)
        expect(page.locator(".loading")).not_to_have_count(0)
        expect(page.locator("#main")).to_be_focused()
        assert len(held) == 1
        held.pop().continue_()
        # 応答が来ても、フォーカスを奪わない
        expect(page.locator("#catalog-results a")).to_have_text(["sales.internal"])
        expect(page.locator("#main")).to_be_focused()
    finally:
        for route in held:
            route.continue_()
        s.close()


# hashchange の処理を数フレーム遅らせる。go() が切り替わる前の画面で戻ると、下のテストが失敗する
DEFER_HASHCHANGE = """
const add = window.addEventListener.bind(window);
window.addEventListener = (type, listener, options) => type !== "hashchange" ? add(type, listener, options) : add(type, (event) => {
  let frames = 5;
  const step = () => (--frames > 0 ? requestAnimationFrame(step) : listener(event));
  requestAnimationFrame(step);
}, options);
"""


def test_go_waits_until_the_new_page_is_rendered(browser, idp, app_url, seed):
    s = Session(browser, idp, app_url, STEWARD)
    s.context.add_init_script(DEFER_HASHCHANGE)
    s.login()
    try:
        s.go("#/catalog", "#catalog-results")
        catalog_title = s.page.locator("main h1").inner_text()
        s.go("#/glossary")  # 既定の待ち (main h1) は、切り替わる前の画面にもある
        assert s.page.locator("main h1").inner_text() != catalog_title
        assert "glossary" in s.page.evaluate("() => window.location.hash")
        assert s.page.locator(".loading").count() == 0
        # 同じハッシュでは再描画が起きないので、切り替わりを待たずに戻る
        s.go("#/glossary")
        assert "glossary" in s.page.evaluate("() => window.location.hash")
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
