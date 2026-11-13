"""Common FastAPI dependencies."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import AuthError, AuthUnavailable, Principal
from edgf.db import SessionLocal
from edgf.errors import ProblemError
from edgf.models import DataAsset
from edgf.observability import channel_var
from edgf.services import ui_sessions

# OpenAPI (/docs) に Bearer 認証を表示するため。検証は app.state.authenticator が行う
_bearer = HTTPBearer(auto_error=False, description="OIDC の JWT (EDGF_AUTH_MODE=jwt)。dev モードでは X-Actor ヘッダ")


def _has_bearer(request: Request) -> bool:
    """Bearer の経路の資格情報 (dev モードでは X-Actor) が付いているか。"""
    return "authorization" in request.headers or (request.app.state.auth_mode == "dev" and "x-actor" in request.headers)


def _session_principal(request: Request, raw: str) -> Principal:
    """セッションの Cookie の経路 (UI)。GET / HEAD / OPTIONS 以外は CSRF を確かめる (Phase 17)。"""
    from edgf.ui import bff  # 循環 import 回避

    with SessionLocal() as db:
        row = ui_sessions.load(db, raw)
        db.commit()
        if row is None:
            raise ProblemError(401, "ログインしていないか、セッションの期限が切れています", type_="session-expired",
                               headers={"Set-Cookie": bff.clear_cookie_header(ui_sessions.SESSION_COOKIE)})
        if request.method not in ui_sessions.SAFE_METHODS:
            bff.check_csrf(request.app.state.settings, request, row)
        principal = ui_sessions.principal_of(row)
    holder = channel_var.get()
    if holder is not None:  # 監査の payload に経路とセッションの参照を残す
        holder.update({"channel": "ui", "session": principal.session_ref})
    return principal


def get_principal(request: Request, _credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    """リクエストの操作者を確定する。失敗時は 401 (鍵配布元の障害時は 503: 認可は fail-close)。

    Bearer とセッションの Cookie の両方を受け付ける。両方が付いていたら、どちらの主体か決められないので 400。
    """
    raw = request.cookies.get(ui_sessions.SESSION_COOKIE)
    if raw is not None and _has_bearer(request):
        raise ProblemError(400, "Authorization とセッションの Cookie の両方が付いています。どちらか一方にしてください",
                           type_="ambiguous-credentials")
    if raw is not None:
        principal = _session_principal(request, raw)
        request.state.actor = principal.audit_actor
        return principal
    try:
        principal = request.app.state.authenticator.authenticate(request)
    except AuthUnavailable as e:
        raise ProblemError(503, str(e), type_="auth-unavailable") from e
    except AuthError as e:
        raise ProblemError(401, e.detail, type_="unauthenticated",
                           headers={"WWW-Authenticate": e.www_authenticate}) from e
    request.state.actor = principal.audit_actor  # アクセスログ用 (5W1H の Who)
    return principal


def get_asset_or_404(db: Session, asset_id: str) -> DataAsset:
    asset = db.get(DataAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    return asset


def load_asset(db: Session, principal: Principal, asset_id: str, action: str | None = None) -> DataAsset:
    """資産を取得し、ABAC の閲覧可否と (指定時は) 操作権限を判定する。"""
    asset = get_asset_or_404(db, asset_id)
    authz.require_read(db, principal, asset)
    if action:
        authz.require(db, principal, action, asset=asset)
    return asset
