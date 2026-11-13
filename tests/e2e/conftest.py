"""UI の E2E テスト。EDGF_E2E=1 のときだけ収集する (通常の pytest ではブラウザを必要としない)。"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

if not os.environ.get("EDGF_E2E"):
    collect_ignore_glob = ["test_*.py"]

ROOT = Path(__file__).resolve().parents[2]
API = "/api/v1"
# 不安定なテストの調査用: 失敗したテストの trace とスクリーンショットを残す。EDGF_E2E_REPEAT は全テストを N 回ずつ実行する
TRACE_DIR = os.environ.get("EDGF_E2E_TRACE_DIR")
TRACE_SNAPSHOTS = bool(os.environ.get("EDGF_E2E_TRACE_SNAPSHOTS"))  # 記録が重いと競合が再現しなくなるので、既定では DOM のスナップショットを取らない
REPEAT = int(os.environ.get("EDGF_E2E_REPEAT") or 0)
ADMIN = {"sub": "root", "roles": ["admin"]}
OWNER = {"sub": "asset-owner", "roles": ["owner"], "domains": ["sales"]}
CONSUMER = {"sub": "consumer1", "roles": ["consumer"], "clearance": "internal"}
STEWARD = {"sub": "steward1", "roles": ["steward"], "domains": ["sales"], "clearance": "confidential"}
COMMITTEE = {"sub": "chair", "roles": ["committee"]}
CUSTODIAN = {"sub": "custodian1", "roles": ["custodian"], "clearance": "confidential"}
USERS = {"admin": ADMIN, "committee": COMMITTEE, "owner": OWNER, "steward": STEWARD, "custodian": CUSTODIAN, "consumer": CONSUMER}
XSS = '<img src=x onerror="window.__xss=1"><script>window.__xss=1</script>'


def pytest_generate_tests(metafunc):
    if REPEAT:
        metafunc.fixturenames.append("e2e_repeat")
        metafunc.parametrize("e2e_repeat", range(REPEAT), indirect=True)


@pytest.fixture()
def e2e_repeat(request):
    return request.param


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if call.when == "call":
        item.e2e_failed = outcome.get_result().failed


@pytest.fixture(autouse=True)
def _keep_artifacts_of_failures(request):
    """全テストより外側で片付ける。成功したテストの trace は消す。"""
    before = set(Path(TRACE_DIR).iterdir()) if TRACE_DIR else set()
    yield
    if TRACE_DIR and not getattr(request.node, "e2e_failed", False):
        for path in set(Path(TRACE_DIR).iterdir()) - before:
            path.unlink(missing_ok=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def idp():
    from tests.e2e.fake_idp import FakeIdp
    server = FakeIdp()
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def app_url(idp, tmp_path_factory):
    db = tmp_path_factory.mktemp("e2e") / "e2e.db"
    port = _free_port()
    env = {**os.environ, "EDGF_DATABASE_URL": f"sqlite:///{db}", "EDGF_ENV": "test", "EDGF_AUTH_MODE": "jwt", "EDGF_OIDC_ISSUER": idp.issuer,
           "EDGF_OIDC_AUDIENCE": "edgf-control-plane", "EDGF_OIDC_JWKS_URL": f"{idp.issuer}/jwks", "EDGF_UI_OIDC_CLIENT_ID": "edgf-ui", "EDGF_UI_CLIENT_SECRET": "e2e-client-secret",
           "EDGF_LOG_LEVEL": "WARNING"}
    alembic = str(Path(sys.executable).parent / "alembic")
    subprocess.run([alembic, "upgrade", "head"], cwd=ROOT, env=env, check=True, capture_output=True)
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "edgf.main:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
                               cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        process.kill()
        pytest.fail("server did not start")
    # 日次のジョブはこのデータベースに対して実行する
    env_for_jobs = env
    yield {"url": url, "env": env_for_jobs}
    process.terminate()
    process.wait(timeout=10)


def transition(page, action, wait="main h1", *, navigation=True):
    """画面の操作をして、遷移が終わるまで待つ (Session.go、ページング、リネージの深さの切り替えで共通)。

    ハッシュは操作と同時に変わり、再描画はそのあとの hashchange で起きる。操作の直後に要素を待つと、
    切り替わる前の画面で成立してしまう (docs/operations/flaky-tests.md)。判定は次の 3 段で行う。

    1. ルートの切り替わり: 再描画は <main> を作り直すので、古い <main> に印を付け、印の無い <main> が現れるまで待つ
    2. 主要な要素の表示: wait の要素が見えること
    3. 読み込みの完了: 読み込み中の表示 (.loading) が無いこと

    navigation=True は「この操作で画面が切り替わるはず」を意味し、ハッシュが変わらなければ失敗にする。
    None はハッシュが変わったときだけ切り替わりを待つ (同じハッシュへの移動を許す)。
    """
    from playwright.sync_api import expect  # playwright は E2E のときだけ入れる
    page.evaluate("""() => {
        const main = document.getElementById("main");
        if (main) main.__e2eStale = true;
        window.__e2eHashBefore = window.location.hash;
    }""")
    action()
    changed = page.evaluate("() => window.location.hash !== window.__e2eHashBefore")
    if navigation and not changed:
        pytest.fail(f"操作で画面が切り替わりませんでした (ハッシュ {page.evaluate('() => window.location.hash')})")
    if changed:
        page.wait_for_function("() => { const main = document.getElementById('main'); return main !== null && !main.__e2eStale; }")
    else:
        page.evaluate("() => { const main = document.getElementById('main'); if (main) delete main.__e2eStale; }")
    expect(page.locator(wait).first).to_be_visible()
    expect(page.locator(".loading")).to_have_count(0)


def _client(idp, app_url, user) -> httpx.Client:
    return httpx.Client(base_url=app_url["url"] + API, headers={"Authorization": f"Bearer {idp.access_token(user)}"}, timeout=30)


@pytest.fixture(scope="session")
def api_as(idp, app_url):
    return lambda user: _client(idp, app_url, user)


@pytest.fixture(scope="session")
def seed(idp, app_url):
    """資産、Lineage、用語、品質、Data Product、Issue を API で登録する。"""
    admin = _client(idp, app_url, ADMIN)
    chair = _client(idp, app_url, COMMITTEE)
    assert admin.post("/domains", json={"name": "sales", "owner": "asset-owner"}).status_code == 201
    controls = ["encryption_at_rest", "encryption_in_transit", "masking"]

    def asset(qn, sensitivity="internal", **kw):
        r = admin.post("/assets", json={"qualified_name": qn, "name": qn, "description": "受注データ", "domain": "sales", "data_class": "transactional",
                                        "sensitivity": sensitivity, "owner": "asset-owner", "steward": "steward1", "custodian": "custodian1", "controls": controls, **kw})
        assert r.status_code == 201, r.text
        return r.json()

    assets = {
        "public": asset("sales.public", "public"), "internal": asset("sales.internal", is_cde=True, schema=[
            {"name": "order_id", "type": "bigint", "description": "受注 ID"}, {"name": "email", "type": "string", "pii": True, "pii_category": "contact"}]),
        "confidential": asset("sales.confidential", "confidential"), "restricted": asset("sales.restricted", "restricted"),
        "xss": asset("sales.xss", description=XSS),
    }
    for source, target in (("sales.public", "sales.internal"), ("sales.internal", "sales.restricted"), ("sales.internal", "ext.unregistered")):
        assert admin.post("/lineage/edges", json={"source": source, "target": target}).status_code == 201
    # カラム単位のリネージ (Phase 16): sales.public.order_id → sales.internal.order_id → sales.restricted.secret_amount
    for source, source_column, target, target_column in (("sales.public", "order_id", "sales.internal", "order_id"),
                                                         ("sales.internal", "order_id", "sales.restricted", "secret_amount")):
        assert admin.post("/lineage/column-edges", json={"source": source, "source_column": source_column, "target": target,
                                                         "target_column": target_column}).status_code == 201
    term = admin.post("/glossary/terms", json={"name": "受注", "definition": "顧客からの注文"}).json()
    owner = _client(idp, app_url, OWNER)
    assert owner.put(f"/glossary/terms/{term['id']}/assets/{assets['internal']['id']}").status_code == 200
    qa = owner
    rule = qa.post(f"/quality/assets/{assets['internal']['id']}/rules", json={"name": "nn", "dimension": "completeness", "rule_type": "not_null", "column": "order_id"})
    assert rule.status_code == 201, rule.text
    for records in ([{"order_id": 1}, {"order_id": None}], [{"order_id": 1}]):
        assert qa.post(f"/quality/assets/{assets['internal']['id']}/runs", json={"records": records}).status_code == 201
    assert admin.post("/issues", json={"kind": "other", "title": "E2E の Issue", "asset_id": assets["internal"]["id"]}).status_code == 201
    product = _client(idp, app_url, STEWARD).post("/mesh/products", json={"name": "orders", "domain": "sales", "owner": "asset-owner", "description": "受注"})
    assert product.status_code == 201, product.text
    consumer = _client(idp, app_url, CONSUMER)
    assert consumer.post("/approvals", json={"type": "access_request", "asset_id": assets["restricted"]["id"], "reason": "調査", "purpose": "分析",
                                             "expires_at": "2099-01-01T00:00:00"}).status_code == 422  # 期限の検証が効く
    from datetime import datetime, timedelta, timezone
    assert consumer.post("/approvals", json={"type": "access_request", "asset_id": assets["restricted"]["id"], "reason": "調査", "purpose": "分析",
                                             "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()}).status_code == 201
    assert chair.get("/kpi").status_code == 200
    subprocess.run([sys.executable, "-m", "edgf.jobs", "run", "kpi_snapshot"], cwd=ROOT, env=app_url["env"], check=True, capture_output=True)
    return {"assets": assets, "term": term}


@pytest.fixture(scope="session")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


class Session:
    """1 人の利用者のブラウザ。全リクエストと CSP の違反を記録する。"""

    def __init__(self, browser, idp, app_url, user):
        self.idp, self.base = idp, app_url["url"]
        idp.user = user
        self.context = browser.new_context(locale="ja-JP", viewport={"width": 1280, "height": 900})
        self.requests = []
        self.console = []
        self.context.on("request", lambda r: self.requests.append(r))
        self.context.on("console", lambda m: self.console.append(m.text))
        self.page = self.context.new_page()
        if TRACE_DIR:
            self.context.tracing.start(screenshots=True, snapshots=TRACE_SNAPSHOTS)

    def open(self):
        self.page.goto(f"{self.base}/ui/")
        self.page.wait_for_selector("#login")
        return self

    def login(self):
        """ログインの画面への遷移 → IdP → /ui/auth/callback → UI に戻る (BFF。ブラウザはトークンを受け取らない)。"""
        self.open()
        self.page.click("#login")
        self.page.wait_for_selector(".site-nav")
        return self

    def go(self, fragment, wait="main h1"):
        """画面を切り替え、遷移が終わるまで待つ。同じハッシュなら再描画は起きないので、切り替わりは待たない。"""
        transition(self.page, lambda: self.page.evaluate("f => { window.location.hash = f; }", fragment), wait, navigation=None)
        return self

    def settled(self):
        """画面の非同期の取得がすべて終わるまで待つ (networkidle は取得の開始前に成立してしまう)。"""
        self.page.wait_for_function("() => document.querySelectorAll('.loading').length === 0")
        self.page.wait_for_load_state("networkidle")
        return self

    def close(self):
        if TRACE_DIR:
            name = Path(TRACE_DIR) / f"{os.getpid()}-{time.time_ns()}"
            screenshot, trace = name.with_suffix(".png"), name.with_suffix(".zip")
            self.page.screenshot(path=screenshot)
            self.context.tracing.stop(path=trace)
        self.context.close()


@pytest.fixture()
def session_of(browser, idp, app_url, seed):
    opened = []

    def factory(user):
        session = Session(browser, idp, app_url, user)
        opened.append(session)
        return session.login()

    yield factory
    for session in opened:
        session.close()
