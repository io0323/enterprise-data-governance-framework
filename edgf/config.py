"""Runtime configuration (12-factor: all settings via environment variables)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    # SQLite by default; set postgresql+psycopg://... for production (Cloud Agnostic).
    database_url: str = field(default_factory=lambda: os.getenv("EDGF_DATABASE_URL", "sqlite:///./edgf.db"))
    policy_dir: Path = field(default_factory=lambda: Path(os.getenv("EDGF_POLICY_DIR", str(ROOT / "policies"))))
    # 設計書 §1.8 / §17: Quality Score 目標 99%
    certification_min_quality: float = float(os.getenv("EDGF_CERT_MIN_QUALITY", "0.99"))
    # 設計書 §16: Issue是正SLA(日)
    issue_sla_days: int = int(os.getenv("EDGF_ISSUE_SLA_DAYS", "30"))


settings = Settings()
