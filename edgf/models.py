"""ORM models for the Governance Control Plane.

Control Plane はデータ本体を保持せず、メタデータ・統制状態・証跡のみを保持する (§18.2)。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Table, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from edgf.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid.uuid4())


term_asset = Table(
    "glossary_term_asset",
    Base.metadata,
    Column("term_id", String(36), ForeignKey("glossary_terms.id", ondelete="CASCADE"), primary_key=True),
    Column("asset_id", String(36), ForeignKey("data_assets.id", ondelete="CASCADE"), primary_key=True),
)


class Domain(Base):
    """Data Mesh Domain (§15)"""
    __tablename__ = "domains"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DataAsset(Base):
    """Catalog entry = Data Product page (§6, §7)"""
    __tablename__ = "data_assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    qualified_name: Mapped[str] = mapped_column(String(300), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    domain: Mapped[str | None] = mapped_column(ForeignKey("domains.name"))
    data_class: Mapped[str] = mapped_column(String(20))
    sensitivity: Mapped[str] = mapped_column(String(20))
    # Ownership (§4)
    owner: Mapped[str | None] = mapped_column(String(200), index=True)
    steward: Mapped[str | None] = mapped_column(String(200))
    custodian: Mapped[str | None] = mapped_column(String(200))
    # State
    lifecycle_state: Mapped[str] = mapped_column(String(20), default="draft")
    certification: Mapped[str] = mapped_column(String(20), default="draft")
    is_cde: Mapped[bool] = mapped_column(Boolean, default=False)
    contains_pii: Mapped[bool] = mapped_column(Boolean, default=False)
    legal_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    retention_days: Mapped[int | None] = mapped_column(Integer)
    freshness_sla_hours: Mapped[int | None] = mapped_column(Integer)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Technical / Business metadata
    schema_: Mapped[list] = mapped_column("schema", JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    controls: Mapped[list] = mapped_column(JSON, default=list)  # 実装済セキュリティ統制 (§11)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)

    terms: Mapped[list[GlossaryTerm]] = relationship(secondary=term_asset, back_populates="assets")
    quality_rules: Mapped[list[QualityRule]] = relationship(back_populates="asset", cascade="all, delete-orphan")


class GlossaryTerm(Base):
    """Business Glossary (§6.2)"""
    __tablename__ = "glossary_terms"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    definition: Mapped[str] = mapped_column(Text)
    steward: Mapped[str | None] = mapped_column(String(200))
    synonyms: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    assets: Mapped[list[DataAsset]] = relationship(secondary=term_asset, back_populates="terms")


class QualityRule(Base):
    """§5 Data Quality Rule"""
    __tablename__ = "quality_rules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    dimension: Mapped[str] = mapped_column(String(20))
    rule_type: Mapped[str] = mapped_column(String(30))
    column: Mapped[str | None] = mapped_column(String(200))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    threshold: Mapped[float] = mapped_column(Float, default=1.0)
    severity: Mapped[str] = mapped_column(String(10), default="medium")
    asset: Mapped[DataAsset] = relationship(back_populates="quality_rules")


class QualityRun(Base):
    __tablename__ = "quality_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id", ondelete="CASCADE"), index=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    record_count: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    dimension_scores: Mapped[dict] = mapped_column(JSON, default=dict)
    results: Mapped[list] = mapped_column(JSON, default=list)


class Issue(Base):
    """§16 Issue / Escalation"""
    __tablename__ = "issues"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("data_assets.id", ondelete="SET NULL"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # quality / policy / access / other
    source_ref: Mapped[str | None] = mapped_column(String(100), index=True)  # rule_id / policy_id (重複起票防止)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(10), default="medium")
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    assignee: Mapped[str | None] = mapped_column(String(200))
    escalation_level: Mapped[str] = mapped_column(String(20), default="steward")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    due_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


class LineageEdge(Base):
    """§9 Lineage (source -> target via job)"""
    __tablename__ = "lineage_edges"
    __table_args__ = (UniqueConstraint("source", "target", "job", name="uq_lineage_edge"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(300), index=True)
    target: Mapped[str] = mapped_column(String(300), index=True)
    job: Mapped[str] = mapped_column(String(300), default="manual")
    transformation: Mapped[str] = mapped_column(Text, default="")
    last_run_id: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PolicyException(Base):
    """§13 Exception (期限付き必須・恒久例外禁止)"""
    __tablename__ = "policy_exceptions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    policy_id: Mapped[str] = mapped_column(String(50), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    risk_assessment: Mapped[str] = mapped_column(Text, default="")
    requested_by: Mapped[str] = mapped_column(String(200))
    approved_by: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/approved/rejected
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AccessGrant(Base):
    """§4.2 アクセス承認 (Owner A/R) / §11 Access Control: 資産メタデータの閲覧承認 (期限付き)。

    restricted 資産は Owner の承認が無い限り当事者・Committee 以外に表示しない (edgf/authz.py)。
    """
    __tablename__ = "access_grants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(200), index=True)
    purpose: Mapped[str] = mapped_column(Text)
    granted_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_by: Mapped[str | None] = mapped_column(String(200))


class AuditEvent(Base):
    """§13 Evidence: append-only, hash-chained"""
    __tablename__ = "audit_events"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str] = mapped_column(String(300), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # §19 Auditability: ログ・トレースとの突合キー (ハッシュ対象。Phase 6 以前のイベントは NULL)
    request_id: Mapped[str | None] = mapped_column(String(128), index=True)
    prev_hash: Mapped[str] = mapped_column(String(64), unique=True)  # 並行書込みによるチェーン分岐をDB制約で防止
    hash: Mapped[str] = mapped_column(String(64), unique=True)
