"""§4 / §11 UI のログインのセッション (BFF。Phase 17).

- セッション ID は 256 ビットの乱数。DB にはハッシュだけを持ち、ログインのたびに新しく発行する (固定攻撃の対策)
- CSRF の同期トークンはセッション ID から導く (HMAC)。DB にはハッシュだけを持ち、照合は定数時間で行う
- 失効: 無操作が EDGF_SESSION_IDLE_MINUTES、ログインから EDGF_SESSION_MAX_HOURS、ログアウト
- IdP のトークンは保存しない。ログイン時に検証した Principal の写しと auth_time だけを持つ
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_
from sqlalchemy.orm import Session

from edgf.auth import Principal
from edgf.config import settings
from edgf.models import UiLoginState, UiSession, utcnow

SESSION_COOKIE = "__Host-edgf_session"
LOGIN_COOKIE = "__Host-edgf_login"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOGIN_STATE_SECONDS = 600
TOUCH_SECONDS = 60  # 最終操作の時刻は 1 分ごとにだけ書く (読み取りのたびに書き込まない)
REF_LENGTH = 12  # 監査に残すセッションの参照 (ID のハッシュの先頭)
_CSRF_CONTEXT = b"edgf-csrf-v1"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_secret() -> str:
    """256 ビットの乱数 (URL で安全な文字列)。"""
    return secrets.token_urlsafe(32)


def csrf_token_for(raw_id: str) -> str:
    """セッションの CSRF のトークン。同じセッションなら同じ値になるので、複数のタブで使える。"""
    mac = hmac.new(raw_id.encode(), _CSRF_CONTEXT, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def session_ref(id_hash: str) -> str:
    return id_hash[:REF_LENGTH]


@dataclass(frozen=True)
class Limits:
    idle: timedelta
    max_age: timedelta

    @classmethod
    def from_settings(cls) -> Limits:
        return cls(timedelta(minutes=settings.session_idle_minutes), timedelta(hours=settings.session_max_hours))


def create(db: Session, principal: Principal, *, auth_time: datetime, name: str = "", now: datetime | None = None,
           limits: Limits | None = None) -> str:
    """新しいセッションを作り、Cookie に入れる生の ID を返す (DB にはハッシュだけ)。"""
    now = now or utcnow()
    limits = limits or Limits.from_settings()
    raw = new_secret()
    db.add(UiSession(id_hash=digest(raw), csrf_hash=digest(csrf_token_for(raw)), subject=principal.subject,
                     roles=sorted(principal.roles), clearance=principal.clearance, domains=sorted(principal.domains),
                     name=name[:200], auth_time=auth_time, created_at=now, last_seen_at=now, expires_at=now + limits.max_age))
    db.flush()
    return raw


def _expired(row: UiSession, now: datetime, limits: Limits) -> bool:
    return now >= row.expires_at or now - row.last_seen_at >= limits.idle


def load(db: Session, raw: str | None, *, now: datetime | None = None, limits: Limits | None = None) -> UiSession | None:
    """有効なセッション。期限切れなら削除して None を返す。最終操作の時刻を更新する。"""
    if not raw or len(raw) > 100:
        return None
    now = now or utcnow()
    limits = limits or Limits.from_settings()
    row = db.get(UiSession, digest(raw))
    if row is None:
        return None
    if _expired(row, now, limits):
        db.delete(row)
        db.flush()
        return None
    if now - row.last_seen_at >= timedelta(seconds=TOUCH_SECONDS):
        row.last_seen_at = now
        db.flush()
    return row


def principal_of(row: UiSession, *, write_request: bool = False) -> Principal:
    return Principal(subject=row.subject, roles=frozenset(row.roles or []), clearance=row.clearance,
                     domains=frozenset(row.domains or []), auth_mode="jwt", channel="ui", auth_time=row.auth_time,
                     session_ref=session_ref(row.id_hash), write_request=write_request)


def csrf_valid(row: UiSession, token: str | None) -> bool:
    """送られたトークンのハッシュを、保存したハッシュと定数時間で比べる。"""
    if not token or len(token) > 100:
        return False
    return hmac.compare_digest(digest(token), row.csrf_hash)


def end(db: Session, raw: str | None) -> bool:
    if not raw:
        return False
    row = db.get(UiSession, digest(raw))
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def view(row: UiSession, raw: str, limits: Limits | None = None) -> dict[str, Any]:
    """GET /ui/session の応答。CSRF のトークンはここでだけ返す (JavaScript から読めるのはこの値だけで、セッション ID は読めない)。"""
    limits = limits or Limits.from_settings()
    return {"subject": row.subject, "name": row.name or row.subject, "roles": sorted(row.roles or []), "clearance": row.clearance,
            "domains": sorted(row.domains or []), "auth_time": row.auth_time, "expires_at": row.expires_at,
            "idle_expires_at": min(row.expires_at, row.last_seen_at + limits.idle), "csrf_token": csrf_token_for(raw)}


def step_up_fresh(principal: Principal, *, now: datetime | None = None) -> bool:
    """✓ の操作の前提: UI の経路の書き込みなら、ログインから EDGF_STEP_UP_MINUTES 以内であること。Bearer の経路には適用しない。"""
    if principal.channel != "ui" or not principal.write_request:
        return True
    now = now or utcnow()
    return principal.auth_time is not None and now - principal.auth_time <= timedelta(minutes=settings.step_up_minutes)


# ---------------------------------------------------------------- ログインの途中の状態
@dataclass(frozen=True)
class LoginStart:
    ref: str  # Cookie に入れる参照の値
    state: str
    nonce: str
    code_verifier: str


def start_login(db: Session, *, return_to: str, max_age: int | None, now: datetime | None = None) -> LoginStart:
    now = now or utcnow()
    start = LoginStart(new_secret(), new_secret(), new_secret(), secrets.token_urlsafe(64))
    db.add(UiLoginState(ref_hash=digest(start.ref), state=start.state, nonce=start.nonce, code_verifier=start.code_verifier,
                        return_to=return_to, max_age=max_age, created_at=now, expires_at=now + timedelta(seconds=LOGIN_STATE_SECONDS)))
    db.flush()
    return start


def take_login(db: Session, ref: str | None, *, now: datetime | None = None) -> UiLoginState | None:
    """1 回だけ使える。期限切れや再利用は None。"""
    if not ref or len(ref) > 100:
        return None
    row = db.get(UiLoginState, digest(ref))
    if row is None:
        return None
    db.delete(row)
    db.flush()
    return row if (now or utcnow()) < row.expires_at else None


def cleanup(db: Session, *, now: datetime | None = None, limits: Limits | None = None) -> dict[str, int]:
    """期限切れのセッションとログインの途中の状態を削除する (ジョブ ui_session_cleanup)。"""
    now = now or utcnow()
    limits = limits or Limits.from_settings()
    sessions = db.execute(delete(UiSession).where(or_(UiSession.expires_at <= now, UiSession.last_seen_at <= now - limits.idle)))
    states = db.execute(delete(UiLoginState).where(UiLoginState.expires_at <= now))
    db.flush()
    return {"sessions_deleted": sessions.rowcount or 0, "login_states_deleted": states.rowcount or 0}
