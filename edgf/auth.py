"""§4 Data Ownership / §11 Access Control: 認証と Principal の解決.

jwt と dev の 2 モードを同じ `Authenticator` インターフェースで扱う。
- jwt: OIDC の JWT を検証する (署名・iss・aud・exp・nbf)。署名鍵は JWKS から取得し、
       キャッシュする。未知の kid が来たら JWKS を再取得するので、鍵のローテーションに追従する。
       IdP 製品には依存せず、claim 名とロール名の対応は設定で与える (edgf/config.py)。
- dev: X-Actor (必須) / X-Roles / X-Domains / X-Clearance ヘッダで操作者を指定する。
       移行期間・開発専用。production では起動を拒否し、監査ログの actor は "dev:<名前>" になる。

認可の判定 (RBAC / ABAC) は edgf/authz.py が担う。本モジュールは「誰か」を確定させるだけ。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import jwt
from starlette.requests import Request

from edgf.config import Settings
from edgf.enums import Sensitivity

log = logging.getLogger("edgf.auth")

AUTH_MODES = ("jwt", "dev")

# §4.1 の役割 + プラットフォーム管理者
ADMIN, COMMITTEE, OWNER, STEWARD, CUSTODIAN, CONSUMER = "admin", "committee", "owner", "steward", "custodian", "consumer"
# 同意取得システム (CMP など) のサービスアカウント専用。privacy.consent.* だけを許可する最小権限のロール
CONSENT_COLLECTOR = "consent_collector"
ROLES: tuple[str, ...] = (ADMIN, COMMITTEE, OWNER, STEWARD, CUSTODIAN, CONSUMER, CONSENT_COLLECTOR)
# 自動化の主体 (system:autonomy) だけが持つロール (Phase 19。docs/adr/0001)。IdP や開発用のヘッダからは得られない (ROLES に含めない)
AUTONOMY = "autonomy"
SYSTEM_ROLES: tuple[str, ...] = (AUTONOMY,)
AUTONOMY_SUBJECT = "system:autonomy"

SENSITIVITY_LEVELS: tuple[str, ...] = tuple(s.value for s in Sensitivity)  # public < internal < confidential < restricted

# clearance claim が無いときにロールから導く既定値 (宣言的定義)
ROLE_DEFAULT_CLEARANCE: Mapping[str, str] = {
    ADMIN: "restricted", COMMITTEE: "restricted",
    OWNER: "confidential", STEWARD: "confidential", CUSTODIAN: "confidential",
    CONSUMER: "internal",
    CONSENT_COLLECTOR: "public",
}

# JWKS の公開鍵で検証するため非対称鍵アルゴリズムのみ許可する (HS* による鍵混同攻撃を防ぐ)
ASYMMETRIC_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
                                   "ES256", "ES384", "ES512", "EdDSA"})

# 監査の actor 列は 200 文字。dev モードは "dev:" を付けて記録するので 196 文字まで
MAX_SUBJECT_LENGTH = 200
# 署名の検証より前に拒否する上限。正規の IdP のトークンはこの大きさにならない
MAX_TOKEN_BYTES = 16 * 1024
_DEV_ACTOR = re.compile(r"^[A-Za-z0-9._@:+\-]{1,196}$")


class AuthConfigError(RuntimeError):
    """認証設定の不備。起動を停止させる (fail fast)。"""


class AuthError(Exception):
    """認証失敗 (401)。"""

    def __init__(self, detail: str, error: str | None = "invalid_token") -> None:
        super().__init__(detail)
        self.detail = detail
        self.error = error

    @property
    def www_authenticate(self) -> str:  # RFC 6750 §3
        return f'Bearer error="{self.error}"' if self.error else "Bearer"


class AuthUnavailable(Exception):
    """鍵配布元 (JWKS) に到達できない。認可は fail-close のため 503 で拒否する (§19)。"""


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[str]
    clearance: str
    domains: frozenset[str]
    auth_mode: str = "jwt"
    # Phase 17: 経路 ("api" は Bearer / dev、"ui" はセッションの Cookie)。ui のときはログインの時刻とセッションの参照 (ハッシュの先頭) を持つ
    channel: str = "api"
    auth_time: datetime | None = None
    session_ref: str | None = None
    write_request: bool = False  # ui の経路の GET / HEAD / OPTIONS 以外の要求 (ステップアップの判定の対象)

    @property
    def audit_actor(self) -> str:
        """監査ログに記録する actor。dev モードの操作は JWT 由来と区別できるよう接頭辞を付ける。"""
        return f"dev:{self.subject}" if self.auth_mode == "dev" else self.subject

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))

    def in_domain(self, domain: str | None) -> bool:
        return "*" in self.domains or (domain is not None and domain in self.domains)


class Authenticator(Protocol):
    mode: str

    def authenticate(self, request: Request) -> Principal: ...


class KeyResolver(Protocol):
    """JWT の署名検証鍵を返す (外部の鍵配布元との境界)。"""

    def signing_key(self, token: str) -> Any: ...


# ---------------------------------------------------------------- claim の正規化
def _as_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [x for x in re.split(r"[,\s]+", raw) if x]
    if isinstance(raw, Iterable):
        return [str(x) for x in raw if str(x)]
    return []


def normalize_roles(raw: Any, role_map: Mapping[str, str] | None = None) -> frozenset[str]:
    """IdP のロール/グループを edgf ロールへ写像する。未知のロールは捨て、consumer を付与する。

    consent_collector だけを持つサービスアカウントには consumer を付けない (最小権限)。
    """
    role_map = role_map or {}
    mapped = {role_map.get(r, r) for r in _as_list(raw)}
    roles = frozenset(r for r in mapped if r in ROLES)
    return roles if roles == {CONSENT_COLLECTOR} else roles | {CONSUMER}


def resolve_clearance(raw: Any, roles: Iterable[str]) -> str:
    if isinstance(raw, str) and raw.lower() in SENSITIVITY_LEVELS:
        return raw.lower()
    defaults = [ROLE_DEFAULT_CLEARANCE[r] for r in roles if r in ROLE_DEFAULT_CLEARANCE] or ["internal"]
    return max(defaults, key=SENSITIVITY_LEVELS.index)


def get_claim(claims: Mapping[str, Any], path: str) -> Any:
    cur: Any = claims
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


# ---------------------------------------------------------------- dev モード
class DevAuthenticator:
    mode = "dev"

    def authenticate(self, request: Request) -> Principal:
        actor = request.headers.get("X-Actor", "").strip()
        if not actor:
            raise AuthError("dev モードでは X-Actor ヘッダが必要です", error="invalid_request")
        if not _DEV_ACTOR.match(actor):
            raise AuthError("X-Actor の形式が不正です (英数字と ._@:+- の 196 文字以内)", error="invalid_request")
        roles = normalize_roles(request.headers.get("X-Roles"))
        return Principal(
            subject=actor, roles=roles,
            clearance=resolve_clearance(request.headers.get("X-Clearance"), roles),
            domains=frozenset(_as_list(request.headers.get("X-Domains"))),
            auth_mode="dev",
        )


# ---------------------------------------------------------------- jwt モード
class JwksKeyResolver:
    """JWKS (RFC 7517) から署名鍵を取得するアダプタ。PyJWKClient がキャッシュと kid ミス時の再取得を担う。"""

    def __init__(self, url: str, cache_seconds: int = 300, refresh_cooldown_seconds: float = 30,
                 timeout: float = 5) -> None:
        self._client = jwt.PyJWKClient(url, cache_jwk_set=True, lifespan=cache_seconds,
                                       cooldown_duration=refresh_cooldown_seconds, timeout=timeout)

    def signing_key(self, token: str) -> Any:
        try:
            return self._client.get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientConnectionError as e:
            log.error("JWKS endpoint unreachable", extra={"fields": {"error": str(e)}})
            raise AuthUnavailable("認証鍵の配布元に到達できません") from e
        except (jwt.PyJWKClientError, jwt.PyJWKSetError, jwt.DecodeError) as e:
            raise AuthError("トークンの署名鍵を特定できません") from e


@dataclass(frozen=True)
class JwtConfig:
    issuer: str
    audience: str
    algorithms: tuple[str, ...] = ("RS256",)
    leeway_seconds: int = 30
    claim_subject: str = "sub"
    claim_roles: str = "roles"
    claim_clearance: str = "clearance"
    claim_domains: str = "domains"
    role_map: tuple[tuple[str, str], ...] = ()


class JwtAuthenticator:
    mode = "jwt"

    def __init__(self, config: JwtConfig, keys: KeyResolver) -> None:
        self.config = config
        self.keys = keys
        self._role_map = dict(config.role_map)

    def authenticate(self, request: Request) -> Principal:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise AuthError("Authorization: Bearer <token> が必要です", error=None)
        return self.principal_from_token(token)

    def principal_from_token(self, token: str) -> Principal:
        """アクセストークンを検証して Principal を作る (API の Bearer と、UI のログインの BFF で共通)。"""
        if len(token) > MAX_TOKEN_BYTES:
            raise AuthError("トークンが大きすぎます")
        key = self.keys.signing_key(token)
        c = self.config
        try:
            claims = jwt.decode(
                token, key, algorithms=list(c.algorithms), audience=c.audience, issuer=c.issuer,
                leeway=c.leeway_seconds, options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as e:
            raise AuthError("トークンの有効期限が切れています") from e
        except jwt.ImmatureSignatureError as e:
            raise AuthError("トークンはまだ有効になっていません (nbf)") from e
        except jwt.InvalidTokenError as e:
            raise AuthError(f"トークンを検証できません ({type(e).__name__})") from e
        subject = get_claim(claims, c.claim_subject)
        if not isinstance(subject, str) or not subject.strip():
            raise AuthError(f"subject claim '{c.claim_subject}' がありません")
        if len(subject.strip()) > MAX_SUBJECT_LENGTH:
            raise AuthError(f"subject claim は {MAX_SUBJECT_LENGTH} 文字以内にしてください")
        roles = normalize_roles(get_claim(claims, c.claim_roles), self._role_map)
        return Principal(
            subject=subject.strip(), roles=roles,
            clearance=resolve_clearance(get_claim(claims, c.claim_clearance), roles),
            domains=frozenset(_as_list(get_claim(claims, c.claim_domains))),
            auth_mode="jwt",
        )


# ---------------------------------------------------------------- 起動時の検証と組み立て
def validate_auth_settings(s: Settings) -> None:
    if s.auth_mode not in AUTH_MODES:
        raise AuthConfigError(f"EDGF_AUTH_MODE は {AUTH_MODES} のいずれかです: {s.auth_mode!r}")
    if s.auth_mode == "dev":
        if s.env == "production":
            raise AuthConfigError("EDGF_ENV=production では EDGF_AUTH_MODE=dev を使用できません (起動を中止します)")
        return
    missing = [name for name, v in (("EDGF_OIDC_ISSUER", s.oidc_issuer), ("EDGF_OIDC_AUDIENCE", s.oidc_audience),
                                    ("EDGF_OIDC_JWKS_URL", s.oidc_jwks_url)) if not v]
    if missing:
        raise AuthConfigError(f"jwt モードには {missing} の設定が必要です "
                              "(ローカル開発では EDGF_AUTH_MODE=dev / docker-compose.dev.yml を使用)")
    bad = [a for a in s.oidc_algorithms if a not in ASYMMETRIC_ALGORITHMS]
    if bad or not s.oidc_algorithms:
        raise AuthConfigError(f"EDGF_OIDC_ALGORITHMS には非対称鍵アルゴリズムのみ指定できます: {bad or '(空)'}")
    if s.env == "production" and not s.oidc_jwks_url.lower().startswith("https://"):
        raise AuthConfigError("EDGF_ENV=production では EDGF_OIDC_JWKS_URL に https が必要です")


def build_authenticator(s: Settings, keys: KeyResolver | None = None) -> Authenticator:
    validate_auth_settings(s)
    if s.auth_mode == "dev":
        log.warning("EDGF_AUTH_MODE=dev: X-Actor ヘッダによる認証が有効です。本番環境では使用しないでください",
                    extra={"fields": {"auth_mode": "dev", "env": s.env}})
        return DevAuthenticator()
    config = JwtConfig(
        issuer=s.oidc_issuer, audience=s.oidc_audience, algorithms=s.oidc_algorithms,
        leeway_seconds=s.jwt_leeway_seconds, claim_subject=s.claim_subject, claim_roles=s.claim_roles,
        claim_clearance=s.claim_clearance, claim_domains=s.claim_domains, role_map=s.role_map,
    )
    resolver = keys or JwksKeyResolver(s.oidc_jwks_url, s.jwks_cache_seconds, s.jwks_refresh_cooldown_seconds)
    return JwtAuthenticator(config, resolver)
