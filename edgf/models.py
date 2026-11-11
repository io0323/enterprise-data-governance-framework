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
    # §14 data_class=ai のときの種別 (training_dataset / embedding_set / vector_index / rag_corpus / model)
    ai_kind: Mapped[str | None] = mapped_column(String(20), index=True)
    # §14 継承した統制属性 (Lineage の上流から計算する)。AI 資産以外は NULL で、ABAC は宣言値を使う
    effective_sensitivity: Mapped[str | None] = mapped_column(String(20))
    effective_contains_pii: Mapped[bool | None] = mapped_column(Boolean)
    inheritance: Mapped[dict] = mapped_column(JSON, default=dict)  # 実効値を決めた上流と解除の根拠 (名前のみ)
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
    # 目的そのものの撤回 (主体ごとの同意の撤回とは別)。撤回後は新しい同意と利用登録を受け付けない
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime)
    withdrawn_by: Mapped[str | None] = mapped_column(String(200))
    withdrawn_reason: Mapped[str | None] = mapped_column(Text)


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
    # §14 消去の影響を受ける AI 資産 (起点の資産名と、Dataset Version / Model Version の ID。主体の情報は含まない)
    ai_impact: Mapped[dict] = mapped_column(JSON, default=dict)


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
    ai_kind: Mapped[str | None] = mapped_column(String(20))  # §14 AI 資産のタスクの種別
    dataset_version_ids: Mapped[list] = mapped_column(JSON, default=list)  # 学習データの該当版
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


class DatasetCard(Base):
    """§14 AI Metadata: Dataset Card。学習データ・Embedding・Vector・RAG コーパスの説明 (値は持たない)。"""
    __tablename__ = "dataset_cards"
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id", ondelete="CASCADE"), primary_key=True)
    purpose: Mapped[str] = mapped_column(Text, default="")
    collection_source: Mapped[str] = mapped_column(Text, default="")
    license: Mapped[str] = mapped_column(String(200), default="")
    consent_purpose_id: Mapped[str | None] = mapped_column(ForeignKey("privacy_purposes.id"))  # 同意の根拠 (§12 Purpose)
    known_biases: Mapped[str] = mapped_column(Text, default="")
    constraints: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ModelCard(Base):
    """§14 AI Metadata: Model Card。重みとレジストリ本体は Data Plane が持つ (§18.2)。"""
    __tablename__ = "model_cards"
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id", ondelete="CASCADE"), primary_key=True)
    intended_use: Mapped[str] = mapped_column(Text, default="")
    out_of_scope_use: Mapped[str] = mapped_column(Text, default="")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)  # 評価指標 (名前 → スカラー値)
    bias_evaluation_ref: Mapped[str] = mapped_column(String(2000), default="")  # 結果の外部参照
    constraints: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DatasetVersion(Base):
    """§14 Training Data の版管理。実データは持たず、Data Plane が計算した manifest のハッシュと件数だけを持つ。"""
    __tablename__ = "dataset_versions"
    __table_args__ = (UniqueConstraint("asset_id", "version", name="uq_dataset_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id"), index=True)
    version: Mapped[str] = mapped_column(String(100))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    record_count: Mapped[int] = mapped_column(Integer)
    period_start: Mapped[datetime | None] = mapped_column(DateTime)
    period_end: Mapped[datetime | None] = mapped_column(DateTime)
    external_uri: Mapped[str | None] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    registered_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime)
    superseded_reason: Mapped[str | None] = mapped_column(String(100))  # 例: erasure:<id>


class DatasetVersionSource(Base):
    """Dataset Version の構成元。kind に応じて version の意味が変わる (資産の版 / MDM / Reference Data / 別の Dataset Version)。"""
    __tablename__ = "dataset_version_sources"
    __table_args__ = (UniqueConstraint("dataset_version_id", "asset_id", "kind", "version", name="uq_dataset_version_source"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id"), index=True)
    qualified_name: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(20), default="asset")  # asset / mdm / reference_data / dataset_version
    version: Mapped[str] = mapped_column(String(100))


model_version_dataset = Table(
    "model_version_datasets",
    Base.metadata,
    Column("model_version_id", String(36), ForeignKey("model_versions.id", ondelete="CASCADE"), primary_key=True),
    Column("dataset_version_id", String(36), ForeignKey("dataset_versions.id"), primary_key=True, index=True),
)


class ModelVersion(Base):
    """§14 AI Metadata / AI Lineage: モデルの版。学習に使った Dataset Version と紐付く。重みは外部参照の URI だけを持つ。"""
    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("asset_id", "version", name="uq_model_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id"), index=True)
    version: Mapped[str] = mapped_column(String(100))
    external_uri: Mapped[str | None] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    registered_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime)
    suspended_reason: Mapped[str | None] = mapped_column(Text)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime)
    datasets: Mapped[list[DatasetVersion]] = relationship(secondary=model_version_dataset)


class ModelUsage(Base):
    """§14 AI Lineage: モデル版の利用先 (デプロイ、RAG、用途)。出力そのものは持たない。"""
    __tablename__ = "model_usages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    model_version_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # deployment / rag / application
    target: Mapped[str] = mapped_column(String(300))
    target_asset_id: Mapped[str | None] = mapped_column(ForeignKey("data_assets.id"))
    description: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TrainingApproval(Base):
    """§14 Training Data: Dataset Version ごとの学習利用の承認。controls の自己申告ではなく、この記録で AI-001 を判定する。"""
    __tablename__ = "training_approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    requested_by: Mapped[str] = mapped_column(String(200))
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    license_status: Mapped[str] = mapped_column(String(20))
    license_ref: Mapped[str] = mapped_column(String(2000), default="")
    bias_evaluation_ref: Mapped[str] = mapped_column(String(2000), default="")
    # Control Plane が計算して添付する (申請者の申告ではない)。値は含まず、カラム名とスコアだけ
    consent_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    quality_summary: Mapped[list] = mapped_column(JSON, default=list)
    decisions: Mapped[list[TrainingApprovalDecision]] = relationship(
        back_populates="approval", cascade="all, delete-orphan", order_by="TrainingApprovalDecision.slot")


class TrainingApprovalDecision(Base):
    """承認の枠。Owner ごとに 1 つ (owner:<subject>)、必要なら Committee に 1 つ (committee)。全枠が揃うと承認になる。"""
    __tablename__ = "training_approval_decisions"
    __table_args__ = (UniqueConstraint("approval_id", "slot", name="uq_training_approval_slot"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey("training_approvals.id", ondelete="CASCADE"), index=True)
    slot: Mapped[str] = mapped_column(String(210))
    required_subject: Mapped[str | None] = mapped_column(String(200))  # Committee の枠は NULL
    asset_ids: Mapped[list] = mapped_column(JSON, default=list)  # この Owner が担当する構成元
    decision: Mapped[str] = mapped_column(String(20), default="pending")  # pending / approved / rejected
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    comment: Mapped[str] = mapped_column(Text, default="")
    approval: Mapped[TrainingApproval] = relationship(back_populates="decisions")


class ModelRemediation(Base):
    """§12 / §14 消去の影響を受けたモデル版の是正。モデルの重みは自動では変えず、Committee が判断する。"""
    __tablename__ = "model_remediations"
    __table_args__ = (UniqueConstraint("model_version_id", "erasure_request_id", name="uq_model_remediation"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    model_version_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id"), index=True)
    erasure_request_id: Mapped[str] = mapped_column(ForeignKey("erasure_requests.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    accept_until: Mapped[datetime | None] = mapped_column(DateTime)
    due_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_by: Mapped[str | None] = mapped_column(String(200))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    replacement_model_version_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id"))
    evidence_hash: Mapped[str | None] = mapped_column(String(64))  # Unlearning の証跡 (Data Plane が算出)


class AiIncident(Base):
    """§14 AI Lineage: データ汚染・リコール。起点は資産または Dataset Version。"""
    __tablename__ = "ai_incidents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(20))
    severity: Mapped[str] = mapped_column(String(10), default="high")
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    origin_asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id"), index=True)
    origin_dataset_version_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_versions.id"))
    affected_model_version_ids: Mapped[list] = mapped_column(JSON, default=list)
    suspended_model_version_ids: Mapped[list] = mapped_column(JSON, default=list)
    issue_ids: Mapped[list] = mapped_column(JSON, default=list)
    reported_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


product_term = Table(
    "data_product_terms",
    Base.metadata,
    Column("product_id", String(36), ForeignKey("data_products.id", ondelete="CASCADE"), primary_key=True),
    Column("term_id", String(36), ForeignKey("glossary_terms.id", ondelete="CASCADE"), primary_key=True),
)


class DataProduct(Base):
    """§15 Data Product の統治のための定義。配信基盤やパイプラインの実行は Data Mesh / Data Plane の責務。"""
    __tablename__ = "data_products"
    __table_args__ = (UniqueConstraint("domain", "name", name="uq_data_product_name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(ForeignKey("domains.name"), index=True)
    owner: Mapped[str] = mapped_column(String(200), index=True)  # 単一 (§4 共同 Owner 禁止)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    published_by: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime)
    terms: Mapped[list[GlossaryTerm]] = relationship(secondary=product_term)
    ports: Mapped[list[OutputPort]] = relationship(back_populates="product", order_by="OutputPort.name")


class OutputPort(Base):
    """Catalog 資産への参照とアドレス URI だけを持つ (配信そのものは Data Plane)。資産とは 1 対 1。"""
    __tablename__ = "output_ports"
    __table_args__ = (UniqueConstraint("product_id", "name", name="uq_output_port_name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("data_products.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id"), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    address_uri: Mapped[str | None] = mapped_column(String(2000))
    port_type: Mapped[str] = mapped_column(String(20))  # table / api / stream / file
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    product: Mapped[DataProduct] = relationship(back_populates="ports")


class DataContract(Base):
    """§15 Standardization: Output Port ごとの Data Contract。内容は版 (ContractVersion) が持つ。"""
    __tablename__ = "data_contracts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("data_products.id"), index=True)
    output_port_id: Mapped[str] = mapped_column(ForeignKey("output_ports.id"), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    schema_object: Mapped[str] = mapped_column(String(200))  # ODCS の schema[].name
    external_id: Mapped[str | None] = mapped_column(String(200))  # ODCS の id (取り込み元の識別子)
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ContractVersion(Base):
    """セマンティックバージョニングの版。公開した版は変更できない。"""
    __tablename__ = "contract_versions"
    __table_args__ = (UniqueConstraint("contract_id", "version", name="uq_contract_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contract_id: Mapped[str] = mapped_column(ForeignKey("data_contracts.id"), index=True)
    version: Mapped[str] = mapped_column(String(50))
    major: Mapped[int] = mapped_column(Integer)
    minor: Mapped[int] = mapped_column(Integer)
    patch: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    schema_: Mapped[list] = mapped_column("schema", JSON, default=list)
    quality_sla: Mapped[dict] = mapped_column(JSON, default=dict)  # min_score / rule_ids
    freshness_sla_hours: Mapped[int | None] = mapped_column(Integer)
    sensitivity: Mapped[str] = mapped_column(String(20))
    purposes: Mapped[list] = mapped_column(JSON, default=list)  # 利用条件 (§12 Purpose の ID)
    conforms_to: Mapped[str | None] = mapped_column(String(300))  # <cdm_entity>@<version>
    cdm_mappings: Mapped[dict] = mapped_column(JSON, default=dict)  # CDM の属性 → Contract のカラム
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_by: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime)
    sunset_at: Mapped[datetime | None] = mapped_column(DateTime)  # 併存期間の満了
    sunset_shortened_by: Mapped[str | None] = mapped_column(String(200))
    sunset_shortened_reason: Mapped[str | None] = mapped_column(Text)
    retired_by: Mapped[str | None] = mapped_column(String(200))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime)


class MeshSubscription(Base):
    """§15 / §4 Data Consumer の利用登録。閲覧権限は与えない。Contract のメジャー版に対して登録する。"""
    __tablename__ = "mesh_subscriptions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    subscriber: Mapped[str] = mapped_column(String(200), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("data_products.id"), index=True)
    output_port_id: Mapped[str] = mapped_column(ForeignKey("output_ports.id"), index=True)
    contract_id: Mapped[str] = mapped_column(ForeignKey("data_contracts.id"), index=True)
    major: Mapped[int] = mapped_column(Integer)
    purpose_id: Mapped[str] = mapped_column(ForeignKey("privacy_purposes.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decision_comment: Mapped[str] = mapped_column(Text, default="")
    suspended_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CdmEntity(Base):
    """外部で定義された CDM のエンティティ。Catalog の metadata 資産と対応付ける (CDM は Governance の被統制対象)。"""
    __tablename__ = "cdm_entities"
    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id"), unique=True)
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CdmVersion(Base):
    """CDM 定義の写し (JSON Schema の一部)。取り込んだ版は変更できない。作成・編集・設計の機能は持たない。"""
    __tablename__ = "cdm_versions"
    __table_args__ = (UniqueConstraint("entity", "version", name="uq_cdm_version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity: Mapped[str] = mapped_column(ForeignKey("cdm_entities.name"), index=True)
    version: Mapped[str] = mapped_column(String(50))
    json_schema: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    source_uri: Mapped[str | None] = mapped_column(String(2000))
    imported_by: Mapped[str] = mapped_column(String(200))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApprovalRequest(Base):
    """§16 Approval: 汎用の承認エンジンの案件。承認のステップは作成時に確定して保持する。"""
    __tablename__ = "approval_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    type: Mapped[str] = mapped_column(String(50), index=True)
    target_type: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[str] = mapped_column(String(300), index=True)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("data_assets.id"), index=True)  # 承認者と ABAC の判定に使う
    requested_by: Mapped[str] = mapped_column(String(200), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    steps: Mapped[list] = mapped_column(JSON, default=list)  # [{name, rule, quorum, domain}]
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


class ApprovalDecision(Base):
    """判断の記録。追記のみで、更新も削除もしない。1 人が同じ案件で判断できるのは 1 回だけ。"""
    __tablename__ = "approval_decisions"
    __table_args__ = (UniqueConstraint("request_id", "decided_by", name="uq_approval_decision"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("approval_requests.id"), index=True)
    step: Mapped[int] = mapped_column(Integer)
    decided_by: Mapped[str] = mapped_column(String(200))
    approve: Mapped[bool] = mapped_column(Boolean)
    comment: Mapped[str] = mapped_column(Text, default="")
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PolicyBundle(Base):
    """§16 Policy / §19 Integrity: Policy の正。YAML 一式を版として保持し、実行時は active の版だけを評価する。"""
    __tablename__ = "policy_bundles"
    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    status: Mapped[str] = mapped_column(String(20), index=True)
    files: Mapped[dict] = mapped_column(JSON)  # 相対パス → YAML の本文
    content_hash: Mapped[str] = mapped_column(String(64))
    base_version: Mapped[int | None] = mapped_column(Integer)
    base_hash: Mapped[str | None] = mapped_column(String(64))  # 提案の元にした active の版 (公開時に変わっていれば拒否する)
    scope: Mapped[str] = mapped_column(String(20), default="global")  # global / domain
    domains: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(Text, default="")
    proposed_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    approval_id: Mapped[str | None] = mapped_column(ForeignKey("approval_requests.id"))
    impact: Mapped[dict] = mapped_column(JSON, default=dict)  # 影響評価 (dry-run) の結果
    rollback_of: Mapped[int | None] = mapped_column(Integer)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    published_by: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime)


class OutboxEvent(Base):
    """§16 Transactional Outbox: 状態変更と同じトランザクションで書き込むイベント (CloudEvents 1.0 の内容)。"""
    __tablename__ = "outbox_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)  # CloudEvents の id (受信側の冪等キー)
    type: Mapped[str] = mapped_column(String(100), index=True)
    subject: Mapped[str] = mapped_column(String(300))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    fanned_out_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)


class WebhookEndpoint(Base):
    """配信先。署名の秘密鍵は持たず、外部の参照 (secret_ref) だけを持つ。"""
    __tablename__ = "webhook_endpoints"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    url: Mapped[str] = mapped_column(String(2000))
    secret_ref: Mapped[str] = mapped_column(String(200))
    event_types: Mapped[list] = mapped_column(JSON, default=list)  # 空なら全種別。末尾の * は前方一致
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WebhookDelivery(Base):
    """イベント × 配信先の配信状況。配信は at-least-once で、最後まで届かなければ dead (デッドレター) に移す。"""
    __tablename__ = "webhook_deliveries"
    __table_args__ = (UniqueConstraint("event_id", "endpoint_id", name="uq_webhook_delivery"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(ForeignKey("outbox_events.id"), index=True)
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("webhook_endpoints.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending / delivered / dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_error: Mapped[str | None] = mapped_column(String(200))  # ステータスや例外の種類だけ (本文や秘密は含めない)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    dead_at: Mapped[datetime | None] = mapped_column(DateTime)


class ReviewCampaign(Base):
    """§16 Review / §11 定期棚卸。同じ種別と期間のキャンペーンは 1 つだけ。"""
    __tablename__ = "review_campaigns"
    __table_args__ = (UniqueConstraint("kind", "period", name="uq_review_campaign"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(30))  # access_review / ownership_attestation / policy_review
    period: Mapped[str] = mapped_column(String(10))  # 2026-Q4 / 2026
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # open / closed
    due_at: Mapped[datetime] = mapped_column(DateTime)
    grace_until: Mapped[datetime] = mapped_column(DateTime)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)


class ReviewItem(Base):
    """レビューの 1 項目。判断は承認エンジンの案件 (approval_id) で行う。"""
    __tablename__ = "review_items"
    __table_args__ = (UniqueConstraint("campaign_id", "target_type", "target_id", name="uq_review_item"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("review_campaigns.id"), index=True)
    target_type: Mapped[str] = mapped_column(String(30))  # access_grant / subscription / asset
    target_id: Mapped[str] = mapped_column(String(36))
    asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id"), index=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey("approval_requests.id"), unique=True)
    # pending / kept / revoked / auto_revoked / attested / disputed / unattested / cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


class PolicyReview(Base):
    """§16 Policy の年次見直しの記録。内容を変えなくても、見直したことを証跡に残す。"""
    __tablename__ = "policy_reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_id: Mapped[str] = mapped_column(String(50), index=True)
    reviewed_by: Mapped[str] = mapped_column(String(200))
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    note: Mapped[str] = mapped_column(Text, default="")


class JobLease(Base):
    """§16 / §19 ジョブのリース。同じジョブが同時に 1 つしか実行されないようにする (外部の依存を増やさず DB で行う)。"""
    __tablename__ = "job_leases"
    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    holder: Mapped[str] = mapped_column(String(100))
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class JobRun(Base):
    """ジョブの実行の記録 (リース・開始・終了・結果・件数)。"""
    __tablename__ = "job_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(50), index=True)
    holder: Mapped[str] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)  # running / succeeded / failed
    count: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(String(200))  # 例外の種類だけ (内部情報を残さない)


class KpiSnapshot(Base):
    """§17 KPI の日次のスナップショット。全社 (scope_type=enterprise) とドメイン別。1 日 1 件。"""
    __tablename__ = "kpi_snapshots"
    __table_args__ = (UniqueConstraint("day", "scope_type", "scope_name", name="uq_kpi_snapshot"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    scope_type: Mapped[str] = mapped_column(String(20))  # enterprise / domain
    scope_name: Mapped[str] = mapped_column(String(100), default="")  # ドメイン名 (全社は空)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UsageDaily(Base):
    """§17 Usage: 資産 × 日の利用の集計値だけ。利用者を特定する情報は受け取らない"""
    __tablename__ = "usage_daily"
    __table_args__ = (UniqueConstraint("asset_id", "day", name="uq_usage_asset_day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(String(36), ForeignKey("data_assets.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    query_count: Mapped[int] = mapped_column(Integer)
    user_count: Mapped[int] = mapped_column(Integer)
    reporter: Mapped[str] = mapped_column(String(200))
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReachabilityReport(Base):
    """§15 Addressable: Output Port に到達できたかの記録。既定は Data Plane からの報告 (push)、任意で Control Plane からの確認 (probe)"""
    __tablename__ = "reachability_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    port_id: Mapped[str] = mapped_column(String(36), ForeignKey("output_ports.id", ondelete="CASCADE"), index=True)
    reachable: Mapped[bool] = mapped_column(Boolean)
    method: Mapped[str] = mapped_column(String(10))  # push / probe
    observed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    detail: Mapped[str] = mapped_column(String(200), default="")
    reporter: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DiscoverySource(Base):
    """§7 Discovery: Data Plane のソース (namespace)。ドメインと Steward は policies/discovery の対応表から決める"""
    __tablename__ = "discovery_sources"
    namespace: Mapped[str] = mapped_column(String(200), primary_key=True)
    domain: Mapped[str] = mapped_column(String(100), index=True)
    steward: Mapped[str] = mapped_column(String(200))
    last_snapshot_id: Mapped[str | None] = mapped_column(String(36))
    last_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime)


class DiscoverySnapshot(Base):
    """資産一覧の受け取りの記録 (件数だけ。一覧そのものは DiscoveredAsset に反映する)"""
    __tablename__ = "discovery_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    namespace: Mapped[str] = mapped_column(String(200), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(200))
    asset_count: Mapped[int] = mapped_column(Integer)
    added_count: Mapped[int] = mapped_column(Integer)
    missing_count: Mapped[int] = mapped_column(Integer)
    changed_count: Mapped[int] = mapped_column(Integer)


class DiscoveredAsset(Base):
    """Data Plane に実在する資産のメタデータ (名前、種類、カラムの名前・型・nullable、更新時刻だけ。値は持たない)"""
    __tablename__ = "discovered_assets"
    __table_args__ = (UniqueConstraint("namespace", "qualified_name", name="uq_discovered_asset"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    namespace: Mapped[str] = mapped_column(String(200), index=True)
    qualified_name: Mapped[str] = mapped_column(String(300), index=True)
    kind: Mapped[str | None] = mapped_column(String(30))
    schema_: Mapped[list] = mapped_column("schema", JSON, default=list)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="present", index=True)  # present / missing
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    missing_since: Mapped[datetime | None] = mapped_column(DateTime)


class SchemaChangeProposal(Base):
    """Discovery で見つかった破壊的なスキーマの変化。Steward / Owner が確認してから Catalog に反映する"""
    __tablename__ = "schema_change_proposals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(String(36), ForeignKey("data_assets.id", ondelete="CASCADE"), index=True)
    namespace: Mapped[str] = mapped_column(String(200))
    proposed_schema: Mapped[list] = mapped_column(JSON, default=list)
    changes: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending / applied / superseded
    issue_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


class IdempotencyRecord(Base):
    """Idempotency-Key による送信の重複の防止 (Data Plane の SDK の再試行)。要求の本文は保存せず、ハッシュだけを持つ"""
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("actor", "key", name="uq_idempotency_actor_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(200))
    key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int | None] = mapped_column(Integer)  # None は処理中
    content_type: Mapped[str | None] = mapped_column(String(100))
    response_body: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    locked_until: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


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


class AuditAnchor(Base):
    """§13 Evidence: 監査チェーンの先頭への署名。正本は DB の外の書き出し先で、この表は参照と照合のための控え"""
    __tablename__ = "audit_anchors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    seq: Mapped[int] = mapped_column(Integer, unique=True)
    hash: Mapped[str] = mapped_column(String(64))
    anchored_at: Mapped[datetime] = mapped_column(DateTime)
    key_id: Mapped[str] = mapped_column(String(32))
    signature: Mapped[str] = mapped_column(String(128))
    sinks: Mapped[dict] = mapped_column(JSON, default=dict)
