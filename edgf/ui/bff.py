"""§4 / §11 UI の BFF (Phase 17): サーバが OIDC の機密クライアントとしてログインを処理する.

- Authorization Code + PKCE。クライアントの秘密は EDGF_UI_CLIENT_SECRET からだけ読む
- IdP の設定はディスカバリから取得する。製品に固有の処理は持たない
- トークン (アクセス、リフレッシュ、ID) はサーバにもブラウザにも保存しない。アクセストークンは API と同じ検証で
  Principal にし、その写しと auth_time だけをセッションに持つ
- ログインの途中の state / nonce / code_verifier はサーバに保存し、短命の __Host- の Cookie (SameSite=Lax) には参照だけを入れる
- UI は dev モードに対応しない (ログインは jwt モードのときだけ)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from edgf.auth import AuthError, AuthUnavailable, JwksKeyResolver, KeyResolver, Principal
from edgf.config import Settings
from edgf.db import get_session
from edgf.errors import ProblemError
from edgf.models import utcnow
from edgf.services import audit, ui_sessions

log = logging.getLogger("edgf.ui.bff")

CALLBACK_PATH = "/ui/auth/callback"
DISCOVERY_SECONDS = 3600
HTTP_TIMEOUT = 10
MAX_RESPONSE_BYTES = 1024 * 1024
_RETURN_TO = re.compile(r"#/[A-Za-z0-9/_\-.~?=&%:@+,]{0,480}")  # fullmatch で使う (末尾の改行も許さない)

# GET (ディスカバリ、JWKS) と POST (トークン) の通信。テストでは差し替える
HttpGet = Callable[[str], Mapping[str, Any]]
HttpPost = Callable[[str, Mapping[str, str]], Mapping[str, Any]]


class LoginFailed(Exception):
    """ログインの失敗。理由はログにだけ残し、ブラウザには汎用の表示を返す (トークンや IdP の応答の本文は出さない)。"""


def ui_enabled(s: Settings) -> bool:
    return s.auth_mode == "jwt" and bool(issuer_of(s) and s.ui_oidc_client_id)


def validate_ui_settings(s: Settings) -> None:
    """production で UI を有効にするなら、クライアントの秘密と https の公開 URL (設定する場合) が必要。"""
    from edgf.auth import AuthConfigError

    if s.env != "production" or not ui_enabled(s):
        return
    if not s.ui_client_secret:
        raise AuthConfigError("EDGF_ENV=production で UI を有効にするには EDGF_UI_CLIENT_SECRET が必要です (起動を中止します)")
    if s.ui_public_url and not s.ui_public_url.startswith("https://"):
        raise AuthConfigError("EDGF_ENV=production では EDGF_UI_PUBLIC_URL に https が必要です")


def issuer_of(s: Settings) -> str:
    return s.ui_oidc_issuer or s.oidc_issuer


def _read_json(response: Any) -> Mapping[str, Any]:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise LoginFailed("IdP の応答が大きすぎます")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise LoginFailed("IdP の応答がオブジェクトではありません")
    return data


def _http_get(url: str) -> Mapping[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310  # nosec B310 - IdP の URL は運用者の設定
        return _read_json(response)


def _http_post(url: str, form: Mapping[str, str]) -> Mapping[str, Any]:
    data = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310  # nosec B310 - discovery の token_endpoint
            return _read_json(response)
    except urllib.error.HTTPError as e:  # 本文には IdP の内部情報が入りうるので読まない
        raise LoginFailed(f"トークンエンドポイントが {e.code} を返しました") from None


def _b64url_sha256(value: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).rstrip(b"=").decode()


class OidcClient:
    def __init__(self, s: Settings, *, http_get: HttpGet = _http_get, http_post: HttpPost = _http_post,
                 keys: KeyResolver | None = None) -> None:
        self.s = s
        self.issuer = issuer_of(s)
        self.discovery_url = s.ui_oidc_discovery_url or f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"
        self._get, self._post = http_get, http_post
        self._keys = keys
        self._discovery: Mapping[str, Any] | None = None
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def discovery(self) -> Mapping[str, Any]:
        with self._lock:
            if self._discovery is None or time.monotonic() - self._loaded_at > DISCOVERY_SECONDS:
                try:
                    found = self._get(self.discovery_url)
                except (urllib.error.URLError, OSError, ValueError) as e:
                    raise AuthUnavailable("IdP のディスカバリを取得できません") from e
                if found.get("issuer") != self.issuer:
                    raise LoginFailed("ディスカバリの issuer が設定と一致しません")
                for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                    if not isinstance(found.get(key), str):
                        raise LoginFailed(f"ディスカバリに {key} がありません")
                self._discovery, self._loaded_at = found, time.monotonic()
            return self._discovery

    def keys(self) -> KeyResolver:
        if self._keys is None:
            self._keys = JwksKeyResolver(str(self.discovery()["jwks_uri"]))
        return self._keys

    def authorization_url(self, start: ui_sessions.LoginStart, redirect_uri: str, max_age: int | None) -> str:
        params = {"response_type": "code", "client_id": self.s.ui_oidc_client_id, "redirect_uri": redirect_uri,
                  "scope": " ".join(self.s.ui_oidc_scopes), "state": start.state, "nonce": start.nonce,
                  "code_challenge": _b64url_sha256(start.code_verifier), "code_challenge_method": "S256"}
        audience = self.s.ui_oidc_audience or self.s.oidc_audience
        if audience:
            params["audience"] = audience
        if max_age is not None:
            params["max_age"] = str(max_age)
        endpoint = str(self.discovery()["authorization_endpoint"])
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{urllib.parse.urlencode(params)}"

    def exchange(self, code: str, verifier: str, redirect_uri: str) -> Mapping[str, Any]:
        """コードをトークンに交換する (client_secret_post)。戻り値はこの関数の中でだけ使い、保存しない。"""
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier,
                "client_id": self.s.ui_oidc_client_id, "client_secret": self.s.ui_client_secret}
        try:
            tokens = self._post(str(self.discovery()["token_endpoint"]), form)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LoginFailed("トークンエンドポイントに接続できません") from e
        if not isinstance(tokens.get("access_token"), str) or not isinstance(tokens.get("id_token"), str):
            raise LoginFailed("トークンの応答に access_token と id_token がありません")
        return tokens

    def verify_id_token(self, id_token: str, nonce: str) -> Mapping[str, Any]:
        key = self.keys().signing_key(id_token)
        try:
            claims = jwt.decode(id_token, key, algorithms=list(self.s.oidc_algorithms), audience=self.s.ui_oidc_client_id,
                                issuer=self.issuer, leeway=self.s.jwt_leeway_seconds, options={"require": ["exp", "iss", "aud", "sub"]})
        except jwt.InvalidTokenError as e:
            raise LoginFailed(f"ID トークンを検証できません ({type(e).__name__})") from e
        if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(claims["nonce"], nonce):
            raise LoginFailed("ID トークンの nonce が一致しません")
        return claims


# ---------------------------------------------------------------- 要求の検査 (API の入口とログアウトで共通)
def public_origin(s: Settings, request: Request) -> str:
    return s.ui_public_url or f"{request.url.scheme}://{request.url.netloc}"


def check_same_origin(s: Settings, request: Request) -> None:
    """Origin と Sec-Fetch-Site で、同一オリジンからの要求であることを確かめる。"""
    origin = request.headers.get("Origin")
    site = request.headers.get("Sec-Fetch-Site")
    if origin != public_origin(s, request):
        raise ProblemError(403, "同一オリジン以外からの要求は受け付けません (Origin)", type_="csrf-failed")
    if site is not None and site != "same-origin":
        raise ProblemError(403, "同一オリジン以外からの要求は受け付けません (Sec-Fetch-Site)", type_="csrf-failed")


def check_csrf(s: Settings, request: Request, row: Any) -> None:
    check_same_origin(s, request)
    if not ui_sessions.csrf_valid(row, request.headers.get(ui_sessions.CSRF_HEADER)):
        raise ProblemError(403, f"{ui_sessions.CSRF_HEADER} が無いか、一致しません", type_="csrf-failed")


def clear_cookie_header(name: str) -> str:
    return f"{name}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict"


def _set_cookie(response: Response, name: str, value: str, *, max_age: int, same_site: str) -> None:
    response.set_cookie(name, value, max_age=max_age, path="/", secure=True, httponly=True, samesite=same_site)


def sanitize_return_to(value: str | None) -> str:
    """戻り先は UI の中の画面 (#/...) だけ。外部の URL への転送に使わせない。"""
    return value if value and _RETURN_TO.fullmatch(value) else ""


def _epoch(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
    return None


# ---------------------------------------------------------------- ルート
def build_router(s: Settings, client: OidcClient | None = None) -> APIRouter:
    router = APIRouter(tags=["ui"], include_in_schema=False)
    oidc = client or OidcClient(s)

    def require_enabled(request: Request) -> None:
        if not ui_enabled(s) or getattr(request.app.state, "auth_mode", "jwt") != "jwt":
            raise ProblemError(404, "UI のログインは設定されていません (jwt モードと EDGF_UI_OIDC_CLIENT_ID が必要です)",
                               type_="ui-not-configured")

    def failed(request: Request, reason: str) -> Response:
        log.warning("ui login failed", extra={"fields": {"reason": reason}})
        response = RedirectResponse("/ui/?error=login_failed", status_code=303)
        response.delete_cookie(ui_sessions.LOGIN_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    @router.get("/ui/auth/login")
    def login(request: Request, return_to: str = "", max_age: int | None = None, db: Session = Depends(get_session)) -> Response:
        """IdP の認可エンドポイントへ転送する。max_age はステップアップの再ログインで使う (0 は再認証の要求)。"""
        require_enabled(request)
        if max_age is not None and not 0 <= max_age <= 86400:
            raise ProblemError(422, "max_age は 0〜86400 秒です", type_="validation-error")
        start = ui_sessions.start_login(db, return_to=sanitize_return_to(return_to), max_age=max_age)
        url = oidc.authorization_url(start, public_origin(s, request) + CALLBACK_PATH, max_age)
        response = RedirectResponse(url, status_code=303)
        _set_cookie(response, ui_sessions.LOGIN_COOKIE, start.ref, max_age=ui_sessions.LOGIN_STATE_SECONDS, same_site="lax")
        return response

    @router.get(CALLBACK_PATH)
    def callback(request: Request, code: str = "", state: str = "", error: str = "", db: Session = Depends(get_session)) -> Response:
        require_enabled(request)
        pending = ui_sessions.take_login(db, request.cookies.get(ui_sessions.LOGIN_COOKIE))
        db.commit()  # 途中の状態は 1 回だけ使える (失敗しても戻さない)
        if pending is None:
            return failed(request, "login_state_missing_or_expired")
        if error or not code:
            return failed(request, f"idp_error:{error[:40]}" if error else "code_missing")
        if not hmac.compare_digest(state, pending.state):
            return failed(request, "state_mismatch")
        try:
            tokens = oidc.exchange(code, pending.code_verifier, public_origin(s, request) + CALLBACK_PATH)
            claims = oidc.verify_id_token(str(tokens["id_token"]), pending.nonce)
            principal: Principal = request.app.state.authenticator.principal_from_token(str(tokens["access_token"]))
        except (LoginFailed, AuthError) as e:
            return failed(request, str(e) if isinstance(e, LoginFailed) else e.detail)
        except AuthUnavailable:
            return failed(request, "idp_unavailable")
        if claims.get("sub") is None:
            return failed(request, "sub_missing")
        now = utcnow()
        auth_time = _epoch(claims.get("auth_time")) or now  # auth_time が無い IdP では、ログインの完了の時刻とする
        if pending.max_age is not None and now - auth_time > timedelta(seconds=pending.max_age + s.jwt_leeway_seconds):
            return failed(request, "reauthentication_not_performed")
        ui_sessions.end(db, request.cookies.get(ui_sessions.SESSION_COOKIE))  # 以前のセッションは使わせない
        name = claims.get("preferred_username") or claims.get("name") or principal.subject
        raw = ui_sessions.create(db, principal, auth_time=auth_time, name=str(name), now=now)
        ref = ui_sessions.session_ref(ui_sessions.digest(raw))
        audit.record(db, principal.audit_actor, "ui.login", "ui_session", ref, {
            "channel": "ui", "session": ref, "auth_time": auth_time, "step_up": pending.max_age is not None})
        db.commit()
        response = RedirectResponse("/ui/" + pending.return_to, status_code=303)
        _set_cookie(response, ui_sessions.SESSION_COOKIE, raw, max_age=s.session_max_hours * 3600, same_site="strict")
        response.delete_cookie(ui_sessions.LOGIN_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    def current(request: Request, db: Session) -> tuple[str, Any]:
        raw = request.cookies.get(ui_sessions.SESSION_COOKIE)
        row = ui_sessions.load(db, raw)
        db.commit()
        if row is None:
            raise ProblemError(401, "ログインしていないか、セッションの期限が切れています", type_="session-expired",
                               headers={"Set-Cookie": clear_cookie_header(ui_sessions.SESSION_COOKIE)} if raw else None)
        return str(raw), row

    @router.get("/ui/session")
    def session(request: Request, db: Session = Depends(get_session)) -> Response:
        """ログイン中の利用者と CSRF のトークン (認証済みの全員)。"""
        raw, row = current(request, db)
        return JSONResponse(json.loads(json.dumps(ui_sessions.view(row, raw), default=str)), headers={"Cache-Control": "no-store"})

    @router.post("/ui/auth/logout")
    def logout(request: Request, db: Session = Depends(get_session)) -> Response:
        raw, row = current(request, db)
        check_csrf(s, request, row)
        ref = ui_sessions.session_ref(row.id_hash)
        ui_sessions.end(db, raw)
        audit.record(db, row.subject, "ui.logout", "ui_session", ref, {"channel": "ui", "session": ref})
        db.commit()
        logout_url = "/ui/"
        try:
            end_session = oidc.discovery().get("end_session_endpoint")
        except (AuthUnavailable, LoginFailed):
            end_session = None
        if isinstance(end_session, str):
            params = {"client_id": s.ui_oidc_client_id, "post_logout_redirect_uri": public_origin(s, request) + "/ui/"}
            logout_url = f"{end_session}{'&' if '?' in end_session else '?'}{urllib.parse.urlencode(params)}"
        response = JSONResponse({"logout_url": logout_url}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(ui_sessions.SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    return router
