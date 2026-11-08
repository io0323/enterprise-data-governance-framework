"""Runtime configuration (12-factor: all settings via environment variables)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, default).split(",") if x.strip())


def _env_pairs(name: str) -> tuple[tuple[str, str], ...]:
    pairs = []
    for item in _env_list(name):
        k, sep, v = item.partition("=")
        if sep and k.strip() and v.strip():
            pairs.append((k.strip(), v.strip()))
    return tuple(pairs)


@dataclass(frozen=True)
class Settings:
    # SQLite by default; set postgresql+psycopg://... for production (Cloud Agnostic).
    database_url: str = field(default_factory=lambda: os.getenv("EDGF_DATABASE_URL", "sqlite:///./edgf.db"))
    policy_dir: Path = field(default_factory=lambda: Path(os.getenv("EDGF_POLICY_DIR", str(ROOT / "policies"))))
    # 設計書 §1.8 / §17: Quality Score 目標 99%
    certification_min_quality: float = float(os.getenv("EDGF_CERT_MIN_QUALITY", "0.99"))
    # 設計書 §16: Issue是正SLA(日)
    issue_sla_days: int = int(os.getenv("EDGF_ISSUE_SLA_DAYS", "30"))
    # §19 Observability
    log_level: str = field(default_factory=lambda: os.getenv("EDGF_LOG_LEVEL", "INFO").upper())
    log_format: str = field(default_factory=lambda: os.getenv("EDGF_LOG_FORMAT", "json").lower())  # json | text
    otel_enabled: bool = field(default_factory=lambda: _env_bool("EDGF_OTEL_ENABLED"))
    service_name: str = field(default_factory=lambda: os.getenv("OTEL_SERVICE_NAME", "edgf"))
    # §19 Maintainability: 起動時に alembic upgrade head を実行する (開発用。本番はデプロイ手順で明示実行)
    auto_migrate: bool = field(default_factory=lambda: _env_bool("EDGF_AUTO_MIGRATE"))

    # §4 / §11 認証 (edgf/auth.py)。jwt = OIDC JWT 検証 (既定) / dev = X-Actor ヘッダ (移行期間・開発専用)
    env: str = field(default_factory=lambda: os.getenv("EDGF_ENV", "development").strip().lower())
    auth_mode: str = field(default_factory=lambda: os.getenv("EDGF_AUTH_MODE", "jwt").strip().lower())
    oidc_issuer: str = field(default_factory=lambda: os.getenv("EDGF_OIDC_ISSUER", ""))
    oidc_audience: str = field(default_factory=lambda: os.getenv("EDGF_OIDC_AUDIENCE", ""))
    oidc_jwks_url: str = field(default_factory=lambda: os.getenv("EDGF_OIDC_JWKS_URL", ""))
    oidc_algorithms: tuple[str, ...] = field(default_factory=lambda: _env_list("EDGF_OIDC_ALGORITHMS", "RS256"))
    jwt_leeway_seconds: int = field(default_factory=lambda: int(os.getenv("EDGF_JWT_LEEWAY_SECONDS", "30")))
    jwks_cache_seconds: int = field(default_factory=lambda: int(os.getenv("EDGF_JWKS_CACHE_SECONDS", "300")))
    jwks_refresh_cooldown_seconds: float = field(
        default_factory=lambda: float(os.getenv("EDGF_JWKS_REFRESH_COOLDOWN_SECONDS", "30")))
    # IdP 製品に依存しないよう claim 名は設定で対応付ける (ドット区切りでネスト指定可: realm_access.roles)
    claim_subject: str = field(default_factory=lambda: os.getenv("EDGF_CLAIM_SUBJECT", "sub"))
    claim_roles: str = field(default_factory=lambda: os.getenv("EDGF_CLAIM_ROLES", "roles"))
    claim_clearance: str = field(default_factory=lambda: os.getenv("EDGF_CLAIM_CLEARANCE", "clearance"))
    claim_domains: str = field(default_factory=lambda: os.getenv("EDGF_CLAIM_DOMAINS", "domains"))
    # §12 subject_ref (HMAC-SHA256) の秘密鍵。production では必須 (edgf/services/subject_ref.py)
    subject_key: str = field(default_factory=lambda: os.getenv("EDGF_SUBJECT_KEY", ""))
    # §12 Right to Erasure の対応期限 (日)
    erasure_sla_days: int = field(default_factory=lambda: int(os.getenv("EDGF_ERASURE_SLA_DAYS", "30")))
    # §14 ModelRemediation の判断と実施の期限 (日)
    ai_remediation_sla_days: int = field(default_factory=lambda: int(os.getenv("EDGF_AI_REMEDIATION_SLA_DAYS", "30")))
    # §12 Anonymization: anonymized と宣言するために必要な k-匿名性の下限 (PRV-003)
    min_k_anonymity: int = field(default_factory=lambda: int(os.getenv("EDGF_MIN_K_ANONYMITY", "5")))
    # §8 Steward の裁定キューの対応期限 (日)
    mdm_review_sla_days: int = field(default_factory=lambda: int(os.getenv("EDGF_MDM_REVIEW_SLA_DAYS", "7")))
    # IdP 側のロール/グループ名 → edgf ロール (例: "dg-committee=committee,dg-admins=admin")
    role_map: tuple[tuple[str, str], ...] = field(default_factory=lambda: _env_pairs("EDGF_ROLE_MAP"))


settings = Settings()
