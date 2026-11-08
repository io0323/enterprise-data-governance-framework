"""Runtime configuration (12-factor: all settings via environment variables)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


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


settings = Settings()
