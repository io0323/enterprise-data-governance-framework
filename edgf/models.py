"""ORM models for the Governance Control Plane.

Control Plane はデータ本体を保持せず、メタデータ・統制状態・証跡のみを保持する (§18.2)。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (JSON, Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer, String, Table, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from edgf.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(value: datetime) -> datetime:
    """DB には naive な UTC で保存する。オフセット付きの日時は UTC に変換する (値を捨てるとずれる)。"""
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def fit_identifier(value: str, max_length: int) -> str:
    """列の長さに収める。連結で生成した識別子が衝突しないよう、切り詰めた部分のハッシュを残す。"""
    if len(value) <= max_length:
        return value
    return f"{value[:max_length - 65]}#{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


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
    # §12 保護手法とパラメータ (k: k-匿名性の k、key_vault_ref: 仮名化キーの分離保管先の識別子)
    privacy_technique: Mapped[str] = mapped_column(String(20), default="none")
    privacy_params: Mapped[dict] = mapped_column(JSON, default=dict)
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


class PrivacyPurpose(Base):
    """§12 処理目的のカタログ。資産とは DataAsset.schema の processing_purposes で関連付く。"""
    __tablename__ = "privacy_purposes"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    default_legal_basis: Mapped[str] = mapped_column(String(30))
    retention_days: Mapped[int | None] = mapped_column(Integer)
    domain: Mapped[str | None] = mapped_column(ForeignKey("domains.name"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Consent(Base):
    """§12 Consent: 追記のみ。現在の状態は subject_ref × purpose の最新の行で決まる。

    データ主体の識別子は保持せず、HMAC の subject_ref だけを持つ (§18.2)。
    """
    __tablename__ = "consents"
    __table_args__ = (Index("ix_consents_subject_purpose", "subject_ref", "purpose_id"),)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_ref: Mapped[str] = mapped_column(String(64))
    purpose_id: Mapped[str] = mapped_column(ForeignKey("privacy_purposes.id"))
    status: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(200), default="")
    recorded_by: Mapped[str] = mapped_column(String(200))
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)


class ErasureRequest(Base):
    """§12 Right to Erasure: 消去要求。データ主体は subject_ref でのみ識別する (§18.2)。"""
    __tablename__ = "erasure_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    subject_ref: Mapped[str] = mapped_column(String(64), index=True)
    identifier_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30), default="received", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    requested_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    due_at: Mapped[datetime] = mapped_column(DateTime)
    scoped_at: Mapped[datetime | None] = mapped_column(DateTime)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    verified_by: Mapped[str | None] = mapped_column(String(200))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    unregistered_downstream: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)


class ErasureTask(Base):
    """対象資産ごとの削除指示。Data Plane (資産の Custodian) が pull して応答する。"""
    __tablename__ = "erasure_tasks"
    __table_args__ = (UniqueConstraint("request_id", "asset_id", name="uq_erasure_task_request_asset"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    request_id: Mapped[str] = mapped_column(ForeignKey("erasure_requests.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id"), index=True)
    qualified_name: Mapped[str] = mapped_column(String(300))
    origin: Mapped[str] = mapped_column(String(20))  # identifier_match / lineage
    depth: Mapped[int] = mapped_column(Integer, default=0)
    columns: Mapped[list] = mapped_column(JSON, default=list)
    golden_ids: Mapped[list] = mapped_column(JSON, default=list)  # MDM Hub 向け: 消去する Golden Record
    status: Mapped[str] = mapped_column(String(20), default="planned", index=True)
    withheld_reason: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    acked_by: Mapped[str | None] = mapped_column(String(200))
    acked_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_by: Mapped[str | None] = mapped_column(String(200))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    completion_request_id: Mapped[str | None] = mapped_column(String(128))
    records_deleted: Mapped[int | None] = mapped_column(Integer)
    evidence_hash: Mapped[str | None] = mapped_column(String(64))  # Data Plane が算出した証跡のハッシュ
    note: Mapped[str] = mapped_column(Text, default="")


class MdmRuleApproval(Base):
    """§8.2 Golden Record 生成規則の Owner 承認。ルール本文のハッシュに対して記録する。"""
    __tablename__ = "mdm_rule_approvals"
    __table_args__ = (UniqueConstraint("entity_type", "rule_hash", name="uq_mdm_rule_approval"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(50))
    rule_hash: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str] = mapped_column(String(200))
    approved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GoldenRecord(Base):
    """§8 Golden Record の識別子と状態。属性値は Data Plane (MDM Hub) が保持する (§18.2)。"""
    __tablename__ = "golden_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    entity_type: Mapped[str] = mapped_column(String(50), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id"), index=True)  # Metadata First
    status: Mapped[str] = mapped_column(String(20), default="active")  # active / tombstoned
    recompose_required: Mapped[bool] = mapped_column(Boolean, default=True)
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    hash_salt: Mapped[str | None] = mapped_column(String(32))  # 消去時に破棄する (content_hash の辞書攻撃を防ぐ)
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime)
    tombstoned_by_request: Mapped[str | None] = mapped_column(String(36))


class MdmXref(Base):
    """ソースレコードと Golden Record の相互参照。キーは HMAC の source_ref でのみ保持する。"""
    __tablename__ = "mdm_xrefs"
    __table_args__ = (UniqueConstraint("entity_type", "source_ref", name="uq_mdm_xref_source"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(50))
    source_ref: Mapped[str] = mapped_column(String(64), index=True)
    source_system: Mapped[str] = mapped_column(String(100))
    golden_id: Mapped[str] = mapped_column(ForeignKey("golden_records.id"), index=True)
    link_type: Mapped[str] = mapped_column(String(20))  # new / auto_merge / steward_merge / split
    linked_by: Mapped[str] = mapped_column(String(200))
    linked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime)


class GoldenVersion(Base):
    """§8 Versioning: 版の情報。値は持たず、内容ハッシュと属性ごとの由来を持つ。追記のみ。"""
    __tablename__ = "golden_versions"
    __table_args__ = (UniqueConstraint("golden_id", "version", name="uq_golden_version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    golden_id: Mapped[str] = mapped_column(ForeignKey("golden_records.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    valid_from: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime)
    content_hash: Mapped[str | None] = mapped_column(String(64))  # merge / split 直後は再構成待ちのため NULL
    source_refs: Mapped[list] = mapped_column(JSON, default=list)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(String(20))  # compose / merge / split / steward_override
    created_by: Mapped[str] = mapped_column(String(200))
    tombstoned: Mapped[bool] = mapped_column(Boolean, default=False)


class MdmReview(Base):
    """Steward の裁定キュー。候補の参照と比較結果だけを持ち、属性値は持たない (§18.2)。

    (entity_type, source_ref, golden_id) で一意。not_match のペアは行が残るので再起票されない。
    """
    __tablename__ = "mdm_reviews"
    __table_args__ = (UniqueConstraint("entity_type", "source_ref", "golden_id", name="uq_mdm_review_pair"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    entity_type: Mapped[str] = mapped_column(String(50))
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id"))
    source_ref: Mapped[str] = mapped_column(String(64), index=True)
    source_system: Mapped[str] = mapped_column(String(100))
    golden_id: Mapped[str] = mapped_column(ForeignKey("golden_records.id"))
    score: Mapped[float] = mapped_column(Float)
    comparisons: Mapped[dict] = mapped_column(JSON, default=dict)  # 属性ごとの match / similar / mismatch
    subject_refs: Mapped[list] = mapped_column(JSON, default=list)  # 裁定後に subject_ref と対応付けるため (HMAC のみ)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    due_at: Mapped[datetime] = mapped_column(DateTime)
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    reason: Mapped[str] = mapped_column(Text, default="")
    new_golden_id: Mapped[str | None] = mapped_column(String(36))  # not_match / split で生まれた Golden Record


class MdmSubjectLink(Base):
    """人を表す Golden Record と Phase 8 の subject_ref の対応 (消去の対象特定に使う)。"""
    __tablename__ = "mdm_subject_links"
    golden_id: Mapped[str] = mapped_column(ForeignKey("golden_records.id"), primary_key=True)
    subject_ref: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    identifier_type: Mapped[str] = mapped_column(String(50))


class RefDataSet(Base):
    """§8 Reference Data Set。Catalog の reference 資産と対応付ける (Metadata First)。"""
    __tablename__ = "reference_data_sets"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RefDataVersion(Base):
    """公開 (published) した版は変更できない。"""
    __tablename__ = "reference_data_versions"
    __table_args__ = (UniqueConstraint("set_id", "version", name="uq_reference_data_version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    set_id: Mapped[str] = mapped_column(ForeignKey("reference_data_sets.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    note: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_by: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)


class RefCode(Base):
    """コード値。Control Plane が値を保持する唯一の例外 (public / internal かつ PII なしの資産に限る)。"""
    __tablename__ = "reference_codes"
    __table_args__ = (UniqueConstraint("version_id", "code", name="uq_reference_code"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("reference_data_versions.id"), index=True)
    code: Mapped[str] = mapped_column(String(100))
    label: Mapped[str] = mapped_column(String(300))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    standard: Mapped[str | None] = mapped_column(String(100))  # 外部標準の出典 (例: ISO 3166-1)
    standard_version: Mapped[str | None] = mapped_column(String(50))  # 例: 2026


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
