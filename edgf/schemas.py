"""API schemas (Pydantic v2)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from edgf.enums import (Certification, ConsentStatus, DataClass, ErasureStatus, ErasureTaskStatus, EscalationLevel,
                        IssueStatus, LegalBasis, LifecycleState,
                        PiiCategory, PrivacyTechnique, QualityDimension, Sensitivity, Severity)

IDENTIFIER_TYPE_PATTERN = r"^[a-z][a-z0-9_]{0,49}$"
# 担当者の列は 200 文字。SQLite は長さを検査せず PostgreSQL は DataError になるので、DB に依存せず入力で検証する
Person = Annotated[str | None, Field(default=None, max_length=200)]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---------- Domain ----------
class DomainIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    owner: Person = None


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
    privacy_technique: PrivacyTechnique | None = Field(default=None, description="カラム単位の保護手法 (§12)")
    privacy_params: dict[str, Any] = Field(default_factory=dict, description="k / key_vault_ref など")

    @model_validator(mode="after")
    def _infer_pii(self) -> ColumnSpec:
        if self.pii_category is not None or self.identifier_type is not None:
            self.pii = True  # カテゴリや識別子種別を持つカラムは PII
        return self


class AssetBase(BaseModel):
    name: str = Field(max_length=200)
    description: str = ""
    domain: str | None = Field(default=None, max_length=100)
    data_class: DataClass
    sensitivity: Sensitivity
    owner: Person = None
    steward: Person = None
    custodian: Person = None
    is_cde: bool = False
    contains_pii: bool = False
    retention_days: int | None = Field(default=None, ge=1)
    freshness_sla_hours: int | None = Field(default=None, ge=1)
    schema_: list[ColumnSpec] = Field(default_factory=list, alias="schema")
    tags: list[str] = Field(default_factory=list)
    controls: list[str] = Field(default_factory=list)
    privacy_technique: PrivacyTechnique = PrivacyTechnique.NONE
    privacy_params: dict[str, Any] = Field(default_factory=dict, description="k (k-匿名性) / key_vault_ref (仮名化キーの分離保管先)")

    model_config = ConfigDict(populate_by_name=True)


class AssetCreate(AssetBase):
    qualified_name: str = Field(min_length=1, max_length=300, description="例: sales.orders")


class AssetUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    domain: str | None = Field(default=None, max_length=100)
    data_class: DataClass | None = None
    sensitivity: Sensitivity | None = None
    owner: Person = None
    steward: Person = None
    custodian: Person = None
    is_cde: bool | None = None
    contains_pii: bool | None = None
    retention_days: int | None = Field(default=None, ge=1)
    freshness_sla_hours: int | None = Field(default=None, ge=1)
    schema_: list[ColumnSpec] | None = Field(default=None, alias="schema")
    tags: list[str] | None = None
    controls: list[str] | None = None
    privacy_technique: PrivacyTechnique | None = None
    privacy_params: dict[str, Any] | None = None


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
    owner: Person = None
    steward: Person = None
    custodian: Person = None


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
    name: str = Field(max_length=200)
    definition: str
    steward: Person = None
    synonyms: list[str] = Field(default_factory=list)


class TermOut(ORM, TermIn):
    id: str
    asset_ids: list[str] = Field(default_factory=list)


# ---------- Quality ----------
class QualityRuleIn(BaseModel):
    name: str = Field(max_length=200)
    dimension: QualityDimension
    rule_type: str = Field(description="not_null|unique|regex|range|allowed_values|freshness|referential|comparison|reference_match。"
                                       "allowed_values / referential は params.reference_set (<id>[@version]) で Reference Data を参照できる")
    column: str | None = Field(default=None, max_length=200)
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
    kind: str = Field(default="other", max_length=20)
    title: str = Field(max_length=300)
    description: str = ""
    severity: Severity = Severity.MEDIUM
    assignee: Person = None


class IssueUpdate(BaseModel):
    status: IssueStatus | None = None
    assignee: Person = None
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
    source: str = Field(max_length=300)
    target: str = Field(max_length=300)
    job: str = Field(default="manual", max_length=300)
    transformation: str = ""


class OLDataset(BaseModel):
    namespace: str = ""
    name: str = Field(max_length=300)


class OLJob(BaseModel):
    namespace: str = ""
    name: str


class OLRun(BaseModel):
    runId: str = Field(max_length=100)


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


# ---------- Privacy ----------
class SubjectIdentifier(BaseModel):
    """データ主体の識別子。受け取った時点で subject_ref に変換して破棄する (レスポンスにもログにも出さない)。"""
    identifier_type: str = Field(pattern=IDENTIFIER_TYPE_PATTERN, description="識別子の種別 (例: customer_id, email)")
    identifier: SecretStr = Field(min_length=1, max_length=512, description="データ主体の識別子 (保存されません)")


class PurposeIn(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$", description="目的 ID (例: marketing)")
    description: str = Field(min_length=1)
    default_legal_basis: LegalBasis
    retention_days: int | None = Field(default=None, ge=1)
    domain: str | None = None


class PurposeUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=1)
    default_legal_basis: LegalBasis | None = None
    retention_days: int | None = Field(default=None, ge=1)


class PurposeOut(ORM):
    id: str
    description: str
    default_legal_basis: LegalBasis
    retention_days: int | None
    domain: str | None
    created_at: datetime
    updated_at: datetime


class ConsentIn(SubjectIdentifier):
    purpose_id: str
    status: ConsentStatus
    source: str = Field(default="", max_length=200, description="取得元 (同意画面やシステムの ID。個人情報を含めないこと)")
    expires_at: datetime | None = Field(default=None, description="同意の有効期限 (status=granted のときのみ)")


class ConsentOut(ORM):
    seq: int
    subject_ref: str
    purpose_id: str
    status: ConsentStatus
    source: str
    recorded_by: str
    recorded_at: datetime
    expires_at: datetime | None


class ConsentRecorded(ConsentOut):
    issues_opened: list[str] = Field(default_factory=list)


class ConsentCheckIn(SubjectIdentifier):
    purpose_id: str


class ErasureRequestIn(SubjectIdentifier):
    reason: str = Field(default="", max_length=1000, description="受付の根拠 (チケット番号など。個人情報を含めないこと)")


class ErasureRejectIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000, description="却下の理由 (法的義務による保持など)")


class ErasureTaskCompleteIn(BaseModel):
    outcome: Literal["completed", "failed"] = "completed"
    records_deleted: int = Field(default=0, ge=0)
    evidence_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$",
                                      description="Data Plane 側の削除証跡の SHA-256 (完了時は必須)")
    note: str = Field(default="", max_length=500, description="個人情報を含めないこと")

    @model_validator(mode="after")
    def _evidence_required(self) -> ErasureTaskCompleteIn:
        if self.outcome == "completed" and not self.evidence_hash:
            raise ValueError("outcome=completed には evidence_hash が必要です")
        return self


class ErasureTaskOut(ORM):
    id: str
    request_id: str
    asset_id: str
    qualified_name: str
    origin: str
    depth: int
    columns: list[str]
    golden_ids: list[str] = Field(default_factory=list, description="MDM Hub 向け: 消去する Golden Record")
    status: ErasureTaskStatus
    withheld_reason: str | None
    approved_by: str | None
    approved_at: datetime | None
    acked_at: datetime | None
    completed_by: str | None
    completed_at: datetime | None
    records_deleted: int | None
    evidence_hash: str | None
    note: str


class ErasureWorkItem(ErasureTaskOut):
    """Data Plane に渡すタスク。subject_ref を自分の識別子の HMAC と突き合わせて対象レコードを特定する。"""
    subject_ref: str
    identifier_type: str
    due_at: datetime


class ErasureRequestOut(ORM):
    id: str
    subject_ref: str
    identifier_type: str
    status: ErasureStatus
    reason: str
    requested_by: str
    created_at: datetime
    due_at: datetime
    scoped_at: datetime | None
    approved_at: datetime | None
    dispatched_at: datetime | None
    finished_at: datetime | None
    verified_at: datetime | None
    verified_by: str | None
    closed_at: datetime | None
    decided_by: str | None
    decision_reason: str | None
    unregistered_downstream: list[str]
    warnings: list[str]


class ErasureRequestDetail(ErasureRequestOut):
    tasks: list[ErasureTaskOut] = Field(default_factory=list)
    hidden_task_count: int = Field(default=0, description="閲覧権限が無く表示していないタスク数 (ABAC)")


# ---------- MDM ----------
class MdmSourceRecord(BaseModel):
    """ソースレコード。値は評価に使うだけで保持しない (§18.2)。"""
    source_system: str = Field(min_length=1, max_length=100)
    key: SecretStr = Field(min_length=1, max_length=512, description="ソース側のキー (HMAC に変換して破棄)")
    attributes: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime | None = None


class MdmCandidate(BaseModel):
    golden_id: str
    attributes: dict[str, Any] = Field(default_factory=dict, description="MDM Hub が保持する現在の値 (評価後に破棄)")


class MdmMatchIn(BaseModel):
    record: MdmSourceRecord
    candidates: list[MdmCandidate] = Field(default_factory=list, max_length=500,
                                           description="比較対象の Golden Record。絞り込みは MDM Hub の責務")


class MdmMatchOut(BaseModel):
    decision: Literal["new", "linked", "review"]
    golden_id: str | None
    source_ref: str
    stage: str
    score: float | None
    recompose_required: bool
    review_ids: list[str] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list, description="比較結果 (値は含まない)")


class MdmComposeIn(BaseModel):
    records: list[MdmSourceRecord] = Field(min_length=1, max_length=1000)
    overrides: dict[str, str] = Field(default_factory=dict, description="属性 → source_system。指定には裁定の権限が必要")


class MdmReviewOut(ORM):
    """裁定キューの項目。属性値は含まない。Steward は値を Data Plane から直接参照する。"""
    id: str
    entity_type: str
    asset_id: str
    source_ref: str
    source_system: str
    golden_id: str
    score: float
    comparisons: dict[str, Any]
    status: str
    created_at: datetime
    due_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    reason: str
    new_golden_id: str | None


class MdmReviewDecision(BaseModel):
    decision: Literal["merge", "not_match", "split"]
    reason: str = Field(default="", max_length=1000, description="個人情報を含めないこと")


class MdmSplitIn(BaseModel):
    source_refs: list[str] = Field(min_length=1, description="別の Golden Record に分割するソース")
    reason: str = Field(min_length=1, max_length=1000, description="個人情報を含めないこと")


class MdmVerifyIn(BaseModel):
    attributes: dict[str, Any] = Field(description="Data Plane が保持している値 (検証後に破棄)")
    version: int | None = Field(default=None, ge=1, description="省略時は現在の版")


class MdmXrefLookupIn(BaseModel):
    source_system: str = Field(min_length=1, max_length=100)
    key: SecretStr = Field(min_length=1, max_length=512, description="ソース側のキー (HMAC に変換して破棄)")


# ---------- Reference Data ----------
class RefDataSetIn(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$", description="Set ID (例: iso3166-1)")
    asset_id: str = Field(description="対応する reference 資産")
    name: str = Field(min_length=1, max_length=200)
    description: str = ""


class RefDataSetOut(ORM):
    id: str
    asset_id: str
    name: str
    description: str
    created_by: str
    created_at: datetime


class RefCodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=300)
    valid_from: date | None = None
    valid_to: date | None = None
    standard: str | None = Field(default=None, max_length=100, description="外部標準の出典 (例: ISO 3166-1)")
    standard_version: str | None = Field(default=None, max_length=50, description="外部標準の版 (例: 2026)")


class RefDataVersionIn(BaseModel):
    codes: list[RefCodeIn] = Field(default_factory=list)
    note: str = Field(default="", max_length=1000)
