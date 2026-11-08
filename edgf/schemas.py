"""API schemas (Pydantic v2)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from edgf.enums import (AiKind, Certification, ConsentStatus, DataClass, ErasureStatus, ErasureTaskStatus, EscalationLevel,
                        DatasetVersionStatus, IssueStatus, LegalBasis, LifecycleState, ModelVersionStatus,
                        PiiCategory, PrivacyTechnique, QualityDimension, Sensitivity, Severity, TrainingApprovalStatus,
                        RemediationStatus)

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
    ai_kind: AiKind | None = Field(default=None, description="§14 data_class=ai の種別")

    model_config = ConfigDict(populate_by_name=True)


class AssetCreate(AssetBase):
    qualified_name: str = Field(min_length=1, max_length=300, description="例: sales.orders")

    @model_validator(mode="after")
    def _ai_kind_only_for_ai(self) -> AssetCreate:
        if self.ai_kind is not None and self.data_class != DataClass.AI:
            raise ValueError("ai_kind は data_class=ai の資産にだけ指定できます")
        return self


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
    ai_kind: AiKind | None = None


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
    effective_sensitivity: Sensitivity | None = Field(default=None, description="§14 継承を含む実効機密度 (AI 資産のみ)。ABAC はこの値で判定する")
    effective_contains_pii: bool | None = Field(default=None, description="§14 継承を含む実効の contains_pii (AI 資産のみ)")
    inheritance: dict[str, Any] = Field(default_factory=dict, description="実効値を決めた上流と継承の解除の根拠")


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
    ai_kind: AiKind | None = Field(default=None, description="§14 AI 資産のタスクの種別")
    dataset_version_ids: list[str] = Field(default_factory=list, description="学習データの該当版")
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
    ai_impact: dict[str, Any] = Field(default_factory=dict, description="§14 影響を受ける Dataset Version / Model Version")


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


# ---------- Data Governance & AI (§14) ----------
# 学習データ・Embedding・ベクトル・サンプルは Data Plane の責務 (§18.2)。誤って送られた値を黙って捨てるのではなく
# 検証エラーにして、呼び出し側に気付かせる (エラーの応答とログに値は含まれない)
class AiInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


ExternalUri = Annotated[str, Field(min_length=1, max_length=2000, description="Data Plane 上の実体への外部参照")]
LongText = Annotated[str, Field(max_length=4000)]


def check_external_uri(uri: str | None) -> str | None:
    """外部参照だけを受け付ける。data: は値そのものを URI に埋め込めるので拒否し、認証情報を含む URI も保存しない。"""
    if uri is None:
        return None
    parts = urlsplit(uri)
    if not parts.scheme or parts.scheme.lower() == "data" or not (parts.netloc or parts.path):
        raise ValueError("external_uri は Data Plane の実体を指す外部参照で指定してください (data: は不可)")
    if parts.username or parts.password:
        raise ValueError("external_uri に認証情報を含めることはできません")
    return uri


class DatasetCardIn(AiInput):
    purpose: LongText = ""
    collection_source: LongText = ""
    license: str = Field(default="", max_length=200)
    consent_purpose_id: str | None = Field(default=None, max_length=80, description="同意の根拠 (Purpose の ID。標準は ai_training)")
    known_biases: LongText = ""
    constraints: LongText = ""


class DatasetCardOut(ORM):
    asset_id: str
    purpose: str
    collection_source: str
    license: str
    consent_purpose_id: str | None
    known_biases: str
    constraints: str
    updated_by: str
    updated_at: datetime


class ModelCardIn(AiInput):
    intended_use: LongText = ""
    out_of_scope_use: LongText = ""
    metrics: dict[str, float | int | Annotated[str, Field(max_length=200)]] = Field(
        default_factory=dict, max_length=100, description="評価指標 (名前 → スカラー値。配列は受け付けない)")
    bias_evaluation_ref: str = Field(default="", max_length=2000, description="バイアス評価の結果の外部参照")
    constraints: LongText = ""


class ModelCardOut(ORM):
    asset_id: str
    intended_use: str
    out_of_scope_use: str
    metrics: dict[str, Any]
    bias_evaluation_ref: str
    constraints: str
    updated_by: str
    updated_at: datetime


class DatasetSourceIn(AiInput):
    asset_id: str = Field(max_length=36)
    kind: Literal["asset", "mdm", "reference_data", "dataset_version"] = "asset"
    version: str = Field(min_length=1, max_length=100, description="構成元の版 (reference_data は版番号、dataset_version は版の名前)")


class DatasetSourceOut(ORM):
    asset_id: str
    qualified_name: str
    kind: str
    version: str


class DatasetVersionIn(AiInput):
    version: str = Field(min_length=1, max_length=100)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$", description="Data Plane が計算した manifest の SHA-256")
    record_count: int = Field(ge=0)
    period_start: datetime | None = None
    period_end: datetime | None = None
    external_uri: ExternalUri | None = None
    sources: list[DatasetSourceIn] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _validate(self) -> DatasetVersionIn:
        check_external_uri(self.external_uri)
        if self.period_start and self.period_end and self.period_start > self.period_end:
            raise ValueError("period_start は period_end 以前にしてください")
        return self


class DatasetVersionOut(ORM):
    id: str
    asset_id: str
    version: str
    manifest_hash: str
    record_count: int
    period_start: datetime | None
    period_end: datetime | None
    external_uri: str | None
    status: DatasetVersionStatus
    registered_by: str
    created_at: datetime
    approved_at: datetime | None
    superseded_at: datetime | None
    superseded_reason: str | None
    sources: list[DatasetSourceOut] = Field(default_factory=list)


class ModelVersionIn(AiInput):
    version: str = Field(min_length=1, max_length=100)
    external_uri: ExternalUri | None = None
    dataset_version_ids: list[str] = Field(min_length=1, max_length=100, description="学習に使った Dataset Version")

    @model_validator(mode="after")
    def _validate(self) -> ModelVersionIn:
        check_external_uri(self.external_uri)
        return self


class ModelVersionOut(ORM):
    id: str
    asset_id: str
    version: str
    external_uri: str | None
    status: ModelVersionStatus
    registered_by: str
    created_at: datetime
    approved_by: str | None
    approved_at: datetime | None
    deployed_at: datetime | None
    suspended_at: datetime | None
    suspended_reason: str | None
    retired_at: datetime | None
    dataset_version_ids: list[str] = Field(default_factory=list)


class TrainingApprovalIn(AiInput):
    license_status: Literal["verified", "unverified", "incompatible"] = Field(description="ライセンス確認の結果")
    license_ref: str = Field(default="", max_length=2000, description="確認結果の外部参照")
    bias_evaluation_ref: str = Field(default="", max_length=2000, description="バイアス評価の外部参照")


class TrainingDecisionIn(AiInput):
    approve: bool
    comment: str = Field(default="", max_length=1000, description="却下のときは必須。個人情報を含めないこと")

    @model_validator(mode="after")
    def _reason_required(self) -> TrainingDecisionIn:
        if not self.approve and not self.comment.strip():
            raise ValueError("却下には comment が必要です")
        return self


class TrainingDecisionOut(ORM):
    slot: str
    required_subject: str | None
    asset_ids: list[str]
    decision: str
    decided_by: str | None
    decided_at: datetime | None
    comment: str


class TrainingApprovalOut(ORM):
    id: str
    dataset_version_id: str
    status: TrainingApprovalStatus
    requested_by: str
    requested_at: datetime
    decided_at: datetime | None
    license_status: str
    license_ref: str
    bias_evaluation_ref: str
    consent_summary: dict[str, Any]
    quality_summary: list[dict[str, Any]]
    decisions: list[TrainingDecisionOut]


class ModelSuspendIn(AiInput):
    reason: str = Field(min_length=1, max_length=1000, description="停止の理由。個人情報を含めないこと")


class ModelUsageIn(AiInput):
    kind: Literal["deployment", "rag", "application"]
    target: str = Field(min_length=1, max_length=300, description="利用先の名前 (環境、サービス、用途)")
    target_asset_id: str | None = Field(default=None, max_length=36, description="利用先が Catalog の資産ならその ID")
    description: str = Field(default="", max_length=1000)


class ModelUsageOut(ORM):
    id: str
    model_version_id: str
    kind: str
    target: str
    target_asset_id: str | None
    description: str
    created_by: str
    created_at: datetime


class RagUser(AiInput):
    subject: str = Field(min_length=1, max_length=200, description="検索の利用者 (JWT の subject)")
    roles: list[str] = Field(default_factory=list, max_length=50)
    clearance: Sensitivity | None = Field(default=None, description="省略時はロールから導く")


class RagAuthorizeIn(AiInput):
    """検索語やチャンクの本文、Embedding は受け取らない (§18.2)。ヒットした構成元の資産 ID だけを渡す。"""
    corpus_asset_id: str = Field(max_length=36)
    user: RagUser
    hit_asset_ids: list[str] = Field(max_length=1000, description="検索でヒットした構成元の資産")


class InheritanceOut(BaseModel):
    asset_id: str
    declared_sensitivity: str
    effective_sensitivity: str
    declared_contains_pii: bool
    effective_contains_pii: bool
    violation: bool
    sensitivity_from: list[str]
    pii_from: list[str]
    released_by: list[str]
    unregistered_upstream: list[str]


class RemediationDecisionIn(AiInput):
    decision: Literal["retrain", "unlearn", "accept"]
    reason: str = Field(default="", max_length=1000, description="accept のときは必須。個人情報を含めないこと")
    accept_until: datetime | None = Field(default=None, description="accept のときは必須 (最大 365 日。恒久的な許容は不可)")

    @model_validator(mode="after")
    def _accept_rules(self) -> RemediationDecisionIn:
        if self.decision == "accept" and (not self.reason.strip() or self.accept_until is None):
            raise ValueError("accept には reason と accept_until が必要です")
        if self.decision != "accept" and self.accept_until is not None:
            raise ValueError("accept_until は accept のときだけ指定できます")
        return self


class RemediationCompleteIn(AiInput):
    replacement_model_version_id: str | None = Field(default=None, max_length=36, description="retrain: 再学習した版")
    evidence_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", description="unlearn: Data Plane が算出した証跡の SHA-256")


class RemediationOut(ORM):
    id: str
    model_version_id: str
    asset_id: str
    erasure_request_id: str
    status: RemediationStatus
    reason: str
    accept_until: datetime | None
    due_at: datetime
    created_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    completed_by: str | None
    completed_at: datetime | None
    replacement_model_version_id: str | None
    evidence_hash: str | None


class IncidentIn(AiInput):
    kind: Literal["data_poisoning", "recall", "other"]
    severity: Severity = Severity.HIGH
    title: str = Field(min_length=1, max_length=200, description="個人情報やデータの値を含めないこと")
    description: str = Field(default="", max_length=2000, description="個人情報やデータの値を含めないこと")
    origin_asset_id: str | None = Field(default=None, max_length=36)
    origin_dataset_version_id: str | None = Field(default=None, max_length=36)
    suspend_model_version_ids: list[str] = Field(default_factory=list, max_length=100, description="停止するモデル版 (任意)")

    @model_validator(mode="after")
    def _one_origin(self) -> IncidentIn:
        if bool(self.origin_asset_id) == bool(self.origin_dataset_version_id):
            raise ValueError("origin_asset_id と origin_dataset_version_id のどちらか一方を指定してください")
        return self


class IncidentOut(ORM):
    id: str
    kind: str
    severity: Severity
    title: str
    description: str
    origin_asset_id: str
    origin_dataset_version_id: str | None
    affected_model_version_ids: list[str]
    suspended_model_version_ids: list[str]
    issue_ids: list[str]
    reported_by: str
    created_at: datetime
