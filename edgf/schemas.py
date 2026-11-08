"""API schemas (Pydantic v2)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from edgf.enums import (Certification, DataClass, EscalationLevel, IssueStatus, LegalBasis, LifecycleState,
                        PiiCategory, QualityDimension, Sensitivity, Severity)

IDENTIFIER_TYPE_PATTERN = r"^[a-z][a-z0-9_]{0,49}$"


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---------- Domain ----------
class DomainIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    owner: str | None = None


class DomainOut(ORM, DomainIn):
    created_at: datetime


# ---------- Catalog ----------
class ColumnSpec(BaseModel):
    name: str
    type: str = "string"
    description: str = ""
    pii: bool = False
    # §12 PII Inventory (いずれも任意。既存の pii フラグと互換)
    pii_category: PiiCategory | None = None
    identifier_type: str | None = Field(default=None, pattern=IDENTIFIER_TYPE_PATTERN,
                                        description="データ主体の識別子の種別 (例: customer_id, email)。消去の対象特定に使う")
    processing_purposes: list[str] = Field(default_factory=list, description="処理目的 (Purpose カタログの ID)")
    legal_basis: LegalBasis | None = None

    @model_validator(mode="after")
    def _infer_pii(self) -> ColumnSpec:
        if self.pii_category is not None or self.identifier_type is not None:
            self.pii = True  # カテゴリや識別子種別を持つカラムは PII
        return self


class AssetBase(BaseModel):
    name: str
    description: str = ""
    domain: str | None = None
    data_class: DataClass
    sensitivity: Sensitivity
    owner: str | None = None
    steward: str | None = None
    custodian: str | None = None
    is_cde: bool = False
    contains_pii: bool = False
    retention_days: int | None = Field(default=None, ge=1)
    freshness_sla_hours: int | None = Field(default=None, ge=1)
    schema_: list[ColumnSpec] = Field(default_factory=list, alias="schema")
    tags: list[str] = Field(default_factory=list)
    controls: list[str] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class AssetCreate(AssetBase):
    qualified_name: str = Field(min_length=1, max_length=300, description="例: sales.orders")


class AssetUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str | None = None
    description: str | None = None
    domain: str | None = None
    data_class: DataClass | None = None
    sensitivity: Sensitivity | None = None
    owner: str | None = None
    steward: str | None = None
    custodian: str | None = None
    is_cde: bool | None = None
    contains_pii: bool | None = None
    retention_days: int | None = Field(default=None, ge=1)
    freshness_sla_hours: int | None = Field(default=None, ge=1)
    schema_: list[ColumnSpec] | None = Field(default=None, alias="schema")
    tags: list[str] | None = None
    controls: list[str] | None = None


class AssetOut(ORM, AssetBase):
    id: str
    qualified_name: str
    lifecycle_state: LifecycleState
    certification: Certification
    legal_hold: bool
    last_refreshed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class LifecycleChange(BaseModel):
    target_state: LifecycleState
    reason: str = ""


class CertificationChange(BaseModel):
    certification: Certification
    reason: str = ""


class LegalHoldChange(BaseModel):
    legal_hold: bool
    reason: str


class OwnershipChange(BaseModel):
    owner: str | None = None
    steward: str | None = None
    custodian: str | None = None


class AccessGrantIn(BaseModel):
    subject: str = Field(min_length=1, max_length=200, description="閲覧を承認する利用者 (JWT の subject)")
    purpose: str = Field(min_length=1, description="利用目的 (§12 目的内利用)")
    expires_at: datetime = Field(description="期限 (恒久承認は不可。最大365日)")


class AccessGrantOut(ORM):
    id: str
    asset_id: str
    subject: str
    purpose: str
    granted_by: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    revoked_by: str | None


# ---------- Glossary ----------
class TermIn(BaseModel):
    name: str
    definition: str
    steward: str | None = None
    synonyms: list[str] = Field(default_factory=list)


class TermOut(ORM, TermIn):
    id: str
    asset_ids: list[str] = Field(default_factory=list)


# ---------- Quality ----------
class QualityRuleIn(BaseModel):
    name: str
    dimension: QualityDimension
    rule_type: str = Field(description="not_null|unique|regex|range|allowed_values|freshness|referential|comparison|reference_match")
    column: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    threshold: float = Field(default=1.0, ge=0, le=1)
    severity: Severity = Severity.MEDIUM


class QualityRuleOut(ORM, QualityRuleIn):
    id: str
    asset_id: str


class QualityRunIn(BaseModel):
    records: list[dict[str, Any]] = Field(description="Data Planeから送られるサンプル/検査対象レコード")


class QualityRunOut(ORM):
    id: str
    asset_id: str
    executed_at: datetime
    record_count: int
    score: float
    dimension_scores: dict[str, float]
    results: list[dict[str, Any]]
    issues_opened: list[str] = Field(default_factory=list)


# ---------- Issue ----------
class IssueIn(BaseModel):
    asset_id: str | None = None
    kind: str = "other"
    title: str
    description: str = ""
    severity: Severity = Severity.MEDIUM
    assignee: str | None = None


class IssueUpdate(BaseModel):
    status: IssueStatus | None = None
    assignee: str | None = None
    comment: str = ""


class IssueOut(ORM):
    id: str
    asset_id: str | None
    kind: str
    source_ref: str | None
    title: str
    description: str
    severity: Severity
    status: IssueStatus
    assignee: str | None
    escalation_level: EscalationLevel
    created_at: datetime
    due_at: datetime | None
    resolved_at: datetime | None


# ---------- Lineage ----------
class LineageEdgeIn(BaseModel):
    source: str
    target: str
    job: str = "manual"
    transformation: str = ""


class OLDataset(BaseModel):
    namespace: str = ""
    name: str


class OLJob(BaseModel):
    namespace: str = ""
    name: str


class OLRun(BaseModel):
    runId: str


class OpenLineageEvent(BaseModel):
    """OpenLineage RunEvent (必要最小サブセット)"""
    eventType: str = "COMPLETE"
    eventTime: datetime | None = None
    run: OLRun
    job: OLJob
    inputs: list[OLDataset] = Field(default_factory=list)
    outputs: list[OLDataset] = Field(default_factory=list)


# ---------- Policy ----------
class ExceptionIn(BaseModel):
    policy_id: str
    asset_id: str
    reason: str
    risk_assessment: str = ""
    expires_at: datetime


class ExceptionDecision(BaseModel):
    approve: bool
    comment: str = ""


class ExceptionOut(ORM):
    id: str
    policy_id: str
    asset_id: str
    reason: str
    risk_assessment: str
    requested_by: str
    approved_by: str | None
    status: str
    expires_at: datetime
    created_at: datetime
