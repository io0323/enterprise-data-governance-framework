"""Phase 13: UI の配信、セキュリティヘッダ、config.json、静的な検査."""
from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from edgf.config import settings
from edgf.main import create_app
from edgf.ui import routes

STATIC = Path(routes.__file__).parent / "static"
ISSUER = "https://idp.example.test/realms/x"


def _files(*suffixes):
    return sorted(p for p in STATIC.rglob("*") if p.suffix in suffixes)


JS = _files(".js")
CODE_OR_MARKUP = _files(".js", ".html", ".css")


def _strip_comments(text: str) -> str:
    return re.sub(r"(^|\s)//[^\n]*", r"\1", text)


@pytest.fixture()
def ui_client():
    configured = dataclasses.replace(settings, ui_oidc_issuer=ISSUER, ui_oidc_client_id="edgf-ui", ui_oidc_audience="edgf-control-plane",
                                     ui_oidc_scopes=("openid", "profile", "email"))
    return TestClient(create_app(configured), follow_redirects=False)


# ---------------------------------------------------------------- 配信
@pytest.mark.parametrize("path,media", [
    ("/ui/", "text/html"), ("/ui/index.html", "text/html"), ("/ui/callback.html", "text/html"), ("/ui/app.js", "text/javascript"),
    ("/ui/styles.css", "text/css"), ("/ui/i18n/ja.json", "application/json"), ("/ui/lib/oidc.js", "text/javascript"),
])
def test_static_files_are_served_without_auth(ui_client, path, media):
    r = ui_client.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith(media)


def test_root_redirects(ui_client):
    r = ui_client.get("/ui")
    assert r.status_code == 307 and r.headers["location"] == "/ui/"


@pytest.mark.parametrize("path", ["/ui/missing.js", "/ui/../main.py", "/ui/%2e%2e/main.py", "/ui/routes.py", "/ui/lib", "/ui/lib/../../routes.py"])
def test_only_static_files_are_served(ui_client, path):
    r = ui_client.get(path)
    assert r.status_code == 404 and "BaseSettings" not in r.text and "import" not in r.text


def test_python_sources_are_outside_static_dir():
    assert not list(STATIC.rglob("*.py"))
    assert routes.MEDIA_TYPES.keys() == {".html", ".js", ".css", ".json"}


# ---------------------------------------------------------------- セキュリティヘッダ
@pytest.mark.parametrize("path", ["/ui", "/ui/", "/ui/app.js", "/ui/config.json", "/ui/missing.js"])
def test_security_headers_on_every_response(ui_client, path):
    h = ui_client.get(path).headers
    assert h["referrer-policy"] == "no-referrer" and h["x-content-type-options"] == "nosniff"
    assert "camera=()" in h["permissions-policy"] and "geolocation=()" in h["permissions-policy"]
    csp = {d.strip().split(" ")[0]: d.strip().split(" ")[1:] for d in h["content-security-policy"].split(";")}
    assert csp["default-src"] == ["'self'"] and csp["script-src"] == ["'self'"] and csp["frame-ancestors"] == ["'none'"]
    assert csp["connect-src"] == ["'self'", "https://idp.example.test"]  # 自身と IdP の issuer だけ
    assert "unsafe-inline" not in h["content-security-policy"] and "unsafe-eval" not in h["content-security-policy"]


def test_csp_without_issuer_allows_self_only():
    bare = dataclasses.replace(settings, ui_oidc_issuer="", oidc_issuer="")
    assert "connect-src 'self';" in routes.content_security_policy(bare)


def test_api_responses_do_not_get_ui_csp(ui_client):
    assert "content-security-policy" not in ui_client.get("/health").headers


# ---------------------------------------------------------------- config.json
def test_config_json_from_settings(ui_client):
    r = ui_client.get("/ui/config.json")
    assert r.json() == {"configured": True, "issuer": ISSUER, "client_id": "edgf-ui", "scopes": ["openid", "profile", "email"], "audience": "edgf-control-plane"}


def test_config_falls_back_to_api_settings_and_reports_unconfigured():
    s = dataclasses.replace(settings, ui_oidc_issuer="", oidc_issuer="https://api-issuer.test", ui_oidc_client_id="", ui_oidc_audience="", oidc_audience="aud")
    body = TestClient(create_app(s)).get("/ui/config.json").json()
    assert body["issuer"] == "https://api-issuer.test" and body["audience"] == "aud" and body["configured"] is False


# ---------------------------------------------------------------- 静的な検査: 読み取り専用と XSS 対策
FORBIDDEN_JS = {
    "innerHTML": r"\binnerHTML\b", "outerHTML": r"\bouterHTML\b", "insertAdjacentHTML": r"insertAdjacentHTML", "document.write": r"document\.write",
    "eval": r"\beval\s*\(", "new Function": r"new\s+Function\b", "setTimeout string": r"set(?:Timeout|Interval)\s*\(\s*[\"'`]",
    "localStorage": r"\blocalStorage\b", "sessionStorage": r"\bsessionStorage\b", "indexedDB": r"\bindexedDB\b", "cookie": r"document\.cookie",
    "XMLHttpRequest": r"XMLHttpRequest", "sendBeacon": r"sendBeacon", "WebSocket": r"\bWebSocket\b", "EventSource": r"\bEventSource\b",
    "X-Actor": r"X-Actor", "dynamic import": r"\bimport\s*\(", "srcdoc": r"srcdoc", "javascript url": r"javascript:",
}


@pytest.mark.parametrize("path", JS, ids=lambda p: str(p.relative_to(STATIC)))
def test_javascript_has_no_forbidden_apis(path):
    text = _strip_comments(path.read_text(encoding="utf-8"))
    for name, pattern in FORBIDDEN_JS.items():
        assert not re.search(pattern, text), f"{name} in {path.name}"


def _fetch_calls():
    """fetch 呼び出しごとの (ファイル, 引数の先頭から括弧が閉じるまで)。"""
    calls = []
    for path in JS:
        text = _strip_comments(path.read_text(encoding="utf-8"))
        for match in re.finditer(r"\bfetch\s*\(", text):
            depth, i = 1, match.end()
            while depth and i < len(text):
                depth += {"(": 1, ")": -1}.get(text[i], 0)
                i += 1
            calls.append((path.name, text[match.end():i - 1]))
    return calls


def test_every_fetch_is_get_except_one_token_exchange():
    calls = _fetch_calls()
    assert calls
    posts = []
    for name, args in calls:
        methods = re.findall(r"method:\s*[\"']([A-Za-z]+)[\"']", args)
        assert len(methods) == 1, f"fetch は method を明示する: {name}: {args[:80]}"
        if methods[0] != "GET":
            posts.append((name, methods[0], args))
    # 唯一の例外は oidc.js のトークンエンドポイントへの POST (コードの交換に必須)
    assert [(n, m) for n, m, _ in posts] == [("oidc.js", "POST")]
    assert posts[0][2].lstrip().startswith("endpoints.token_endpoint")


def test_api_client_only_sends_get_and_no_dev_headers():
    text = (STATIC / "lib" / "api.js").read_text(encoding="utf-8")
    assert text.count("fetch(") == 1 and 'method: "GET"' in text and "Bearer" in text


URL_PATTERN = re.compile(r"(?:https?:)?//[A-Za-z0-9.-]+\.[a-z]{2,}|https?://")
ALLOWED_URLS = ("http://www.w3.org/2000/svg",)


@pytest.mark.parametrize("path", CODE_OR_MARKUP, ids=lambda p: str(p.relative_to(STATIC)))
def test_no_external_urls(path):
    text = path.read_text(encoding="utf-8")
    for allowed in ALLOWED_URLS:
        text = text.replace(allowed, "")
    assert not URL_PATTERN.search(_strip_comments(text)), path.name


@pytest.mark.parametrize("path", _files(".html"), ids=lambda p: p.name)
def test_html_has_no_inline_code(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), "inline script"
    assert "<style" not in text and not re.search(r"\sstyle\s*=", text) and not re.search(r"\son[a-z]+\s*=", text)


def test_css_has_no_external_resources():
    text = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert "@import" not in text and "url(" not in text and "@font-face" not in text


def test_no_build_or_npm_files():
    root = Path(routes.__file__).resolve().parents[2]
    assert not list(root.glob("package*.json")) and not (root / "node_modules").exists()


# ---------------------------------------------------------------- 辞書
def _dict(lang):
    return json.loads((STATIC / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))


def test_dictionaries_have_same_keys():
    assert _dict("ja").keys() == _dict("en").keys()


def test_used_keys_exist():
    keys = _dict("ja").keys()
    for path in JS:
        for key in re.findall(r"\bt\(\s*\"([a-z_]+\.[a-z0-9_.]+)\"", path.read_text(encoding="utf-8")):
            assert key in keys, f"{key} in {path.name}"


def test_ui_is_not_dev_mode_aware():
    for path in JS:
        text = path.read_text(encoding="utf-8")
        assert "X-Roles" not in text and "auth_mode" not in text, path.name
