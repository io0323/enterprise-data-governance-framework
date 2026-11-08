"""Common FastAPI dependencies."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import AuthError, AuthUnavailable, Principal
from edgf.errors import ProblemError
from edgf.models import DataAsset

# OpenAPI (/docs) に Bearer 認証を表示するため。検証は app.state.authenticator が行う
_bearer = HTTPBearer(auto_error=False, description="OIDC の JWT (EDGF_AUTH_MODE=jwt)。dev モードでは X-Actor ヘッダ")


def get_principal(request: Request, _credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    """リクエストの操作者を確定する。失敗時は 401 (鍵配布元の障害時は 503: 認可は fail-close)。"""
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
