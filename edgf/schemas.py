"""API schemas (Pydantic v2)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from edgf.enums import (AiKind, Certification, ConsentStatus, DataClass, ErasureStatus, ErasureTaskStatus, EscalationLevel,
                        DatasetVersionStatus, IssueStatus, LegalBasis, LifecycleState, ModelVersionStatus,
                        PiiCategory, PrivacyTechnique, QualityDimension, Sensitivity, Severity, TrainingApprovalStatus,
                        ApprovalStatus, ContractStatus, ProductStatus, RemediationStatus, SubscriptionStatus)

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
    nullable: bool | None = Field(default=None, description="§15 Contract とのずれの検出に使う (省略時は比較しない)")
    description: str = ""
    pii: bool = False
    cde: bool = Field(default=False, description="§5.2 / §9 Critical Data Element のカラム。カラム単位のリネージが必須 (LIN-002)")
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

    @model_validator(mode="after")
    def _derive_is_cde(self) -> AssetCreate:
        """is_cde は「CDE のカラムを含む」。cde のカラムがあれば true を導く (明示の false とは矛盾するので拒否)。"""
        if any(c.cde for c in self.schema_):
            if "is_cde" in self.model_fields_set and not self.is_cde:
                raise ValueError("cde のカラムを持つ資産は is_cde を false にできません")
            self.is_cde = True
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
    facets: dict[str, Any] | None = Field(default=None, description="columnLineage だけを読む (Phase 16)。他の facet は保存しない")


class ColumnEdgeIn(BaseModel):
    """カラム単位のリネージの手動の登録。target_column を省略するとデータセット全体に効く入力 (INDIRECT)。"""
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=300)
    source_column: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=300)
    target_column: str | None = Field(default=None, min_length=1, max_length=200)
    job: str = Field(default="manual", min_length=1, max_length=300)
    type: Literal["DIRECT", "INDIRECT"] = "DIRECT"
    subtype: str | None = Field(default=None, max_length=30)
    masking: bool = False


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
    comment: str = Field(default="", description="却下のときは必須 (Phase 17 から。他の判断の API と同じ)")

    @model_validator(mode="after")
    def _reason_required(self) -> ExceptionDecision:
        if not self.approve and not self.comment.strip():
            raise ValueError("却下には comment が必要です")
        return self


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
    withdrawn_at: datetime | None = None
    withdrawn_reason: str | None = None


class PurposeWithdrawIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000, description="目的を撤回する理由。個人情報を含めないこと")


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
    lineage_exclusions: list[dict[str, Any]] = Field(
        default_factory=list, description="Phase 16: 信頼できる生成元の辺だけを経由し、PII のカラムからの DIRECT 経路が無いため外した資産 (閲覧できるものだけ)")


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
    pii_columns: dict[str, list[dict[str, str]]] = Field(
        default_factory=dict, description="Phase 16: DIRECT 経路で PII のカラムが届くカラムと、その元 (資産とカラム)")
    pii_excluded: list[dict[str, Any]] = Field(
        default_factory=list, description="Phase 16: 信頼できる生成元の辺だけを経由し、PII のカラムからの DIRECT 経路が無いため継承しない上流")


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


# ---------- Data Mesh 連携 (§15) ----------
# Control Plane が持つのは統治のための定義だけ。未定義の項目を黙って捨てず、検証エラーで知らせる
class MeshInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductIn(MeshInput):
    name: str = Field(min_length=1, max_length=200)
    domain: str = Field(max_length=100)
    owner: str = Field(min_length=1, max_length=200, description="単一。そのドメインの Data Owner であること")
    description: str = Field(default="", max_length=4000)
    term_ids: list[str] = Field(default_factory=list, max_length=200, description="Glossary の用語")


class ProductUpdate(MeshInput):
    owner: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    term_ids: list[str] | None = Field(default=None, max_length=200)


class OutputPortIn(MeshInput):
    name: str = Field(min_length=1, max_length=200)
    asset_id: str = Field(max_length=36, description="Catalog 資産 (1 対 1。Product と同じドメイン)")
    port_type: Literal["table", "api", "stream", "file"]
    address_uri: ExternalUri | None = None

    @model_validator(mode="after")
    def _validate(self) -> OutputPortIn:
        check_external_uri(self.address_uri)
        return self


class OutputPortUpdate(MeshInput):
    address_uri: ExternalUri | None = None
    port_type: Literal["table", "api", "stream", "file"] | None = None

    @model_validator(mode="after")
    def _validate(self) -> OutputPortUpdate:
        check_external_uri(self.address_uri)
        return self


class OutputPortOut(BaseModel):
    id: str
    product_id: str
    name: str
    redacted: bool = Field(default=False, description="閲覧できない資産のポートは名前だけを返す (ABAC)")
    asset_id: str | None = None
    port_type: str | None = None
    address_uri: str | None = None
    contract_id: str | None = None


class ProductOut(BaseModel):
    id: str
    name: str
    domain: str
    owner: str
    description: str
    status: ProductStatus
    created_by: str
    created_at: datetime
    updated_at: datetime
    published_by: str | None
    published_at: datetime | None
    deprecated_at: datetime | None
    retired_at: datetime | None
    term_ids: list[str] = Field(default_factory=list)
    ports: list[OutputPortOut] = Field(default_factory=list)


SEMVER_PATTERN = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"


class ContractColumn(MeshInput):
    """カラムの定義。値やサンプルは持たない。"""
    name: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=100)
    nullable: bool = True
    pii: bool = False
    description: str = Field(default="", max_length=2000)
    identifier_type: str | None = Field(default=None, pattern=IDENTIFIER_TYPE_PATTERN, description="ID の標準 (§15 命名/ID標準)")
    cde: bool = Field(default=False, description="Critical Data Element のカラム (ODCS の criticalDataElement)")
    reference_set: str | None = Field(default=None, max_length=120, description="参照する Reference Data (<id>[@version])")
    renamed_from: str | None = Field(default=None, max_length=200, description="名前の変更を明示する (前の版のカラム名)")


class QualitySla(MeshInput):
    min_score: float | None = Field(default=None, ge=0, le=1, description="最低の Quality Score")
    rule_ids: list[str] = Field(default_factory=list, max_length=200, description="既存の品質ルールへの参照")


class ContractContent(MeshInput):
    description: str = Field(default="", max_length=4000)
    schema_: list[ContractColumn] = Field(alias="schema", min_length=1, max_length=2000)
    quality_sla: QualitySla = Field(default_factory=QualitySla)
    freshness_sla_hours: int | None = Field(default=None, ge=1)
    sensitivity: Sensitivity
    purposes: list[str] = Field(default_factory=list, max_length=100, description="利用条件 (Purpose の ID)")
    conforms_to: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+$", max_length=300, description="<cdm_entity>@<version>")
    cdm_mappings: dict[str, str] = Field(default_factory=dict, description="CDM の属性 → Contract のカラム")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _unique_columns(self) -> ContractContent:
        names = [c.name for c in self.schema_]
        if len(names) != len(set(names)):
            raise ValueError("カラム名が重複しています")
        return self


class ContractVersionIn(ContractContent):
    version: str = Field(pattern=SEMVER_PATTERN, max_length=50, description="セマンティックバージョン (例: 1.2.0)")


class ContractIn(MeshInput):
    output_port_id: str = Field(max_length=36)
    name: str | None = Field(default=None, max_length=200, description="省略時は Output Port の名前")


class ContractOut(ORM):
    id: str
    product_id: str
    output_port_id: str
    name: str
    schema_object: str
    external_id: str | None
    created_by: str
    created_at: datetime


class ContractVersionOut(ORM):
    id: str
    contract_id: str
    version: str
    major: int
    status: ContractStatus
    description: str
    schema_: list[dict[str, Any]] = Field(serialization_alias="schema")
    quality_sla: dict[str, Any]
    freshness_sla_hours: int | None
    sensitivity: Sensitivity
    purposes: list[str]
    conforms_to: str | None
    cdm_mappings: dict[str, str]
    created_by: str
    created_at: datetime
    published_by: str | None
    published_at: datetime | None
    deprecated_at: datetime | None
    sunset_at: datetime | None
    sunset_shortened_by: str | None
    sunset_shortened_reason: str | None
    retired_at: datetime | None


class ContractCheckIn(MeshInput):
    """提案の案。省略した項目は最新の公開版の値を引き継ぐ (スキーマだけの案でも判定できる)。"""
    version: str | None = Field(default=None, pattern=SEMVER_PATTERN, max_length=50)
    description: str | None = Field(default=None, max_length=4000)
    schema_: list[ContractColumn] | None = Field(default=None, alias="schema", max_length=2000)
    quality_sla: QualitySla | None = None
    freshness_sla_hours: int | None = Field(default=None, ge=1)
    sensitivity: Sensitivity | None = None
    purposes: list[str] | None = Field(default=None, max_length=100)
    conforms_to: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+$", max_length=300)
    cdm_mappings: dict[str, str] | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SubscriptionIn(MeshInput):
    output_port_id: str = Field(max_length=36)
    purpose_id: str = Field(max_length=80)
    major: int | None = Field(default=None, ge=0, description="Contract のメジャー版。省略時は最新")


class SubscriptionDecisionIn(MeshInput):
    approve: bool
    comment: str = Field(default="", max_length=1000, description="却下のときは必須")

    @model_validator(mode="after")
    def _reason_required(self) -> SubscriptionDecisionIn:
        if not self.approve and not self.comment.strip():
            raise ValueError("却下には comment が必要です")
        return self


class SubscriptionMigrateIn(MeshInput):
    major: int = Field(ge=0, description="付け替え先のメジャー版")
    purpose_id: str | None = Field(default=None, max_length=80, description="利用目的も変える場合に指定")


class SubscriptionOut(ORM):
    id: str
    subscriber: str
    product_id: str
    output_port_id: str
    contract_id: str
    major: int
    purpose_id: str
    status: SubscriptionStatus
    decided_by: str | None
    decided_at: datetime | None
    decision_comment: str
    suspended_reason: str | None
    created_at: datetime


class ExpediteRetireIn(MeshInput):
    reason: str = Field(max_length=1000, description="期間を短縮する理由 (必須)")
    sunset_at: datetime | None = Field(default=None, description="新しい満了日時 (省略時は現在)。今の満了日時より前であること")

    @model_validator(mode="after")
    def _reason_required(self) -> ExpediteRetireIn:
        if not self.reason.strip():
            raise ValueError("期間の短縮には reason が必要です")
        return self


class CdmImportIn(MeshInput):
    entity: str = Field(pattern=r"^[A-Za-z0-9._-]{1,120}$", description="エンティティ名")
    asset_id: str = Field(max_length=36, description="data_class=metadata の Catalog 資産")
    version: str = Field(pattern=r"^[A-Za-z0-9._-]{1,50}$", description="外部で定義された版")
    json_schema: dict[str, Any] = Field(description="JSON Schema の一部 (object / properties / required / type / format)")
    source_uri: ExternalUri | None = None

    @model_validator(mode="after")
    def _validate(self) -> CdmImportIn:
        check_external_uri(self.source_uri)
        return self


class CdmEntityOut(ORM):
    name: str
    asset_id: str
    created_by: str
    created_at: datetime


class CdmVersionOut(ORM):
    entity: str
    version: str
    json_schema: dict[str, Any]
    content_hash: str
    source_uri: str | None
    imported_by: str
    imported_at: datetime


# ---------- Governance Process (§16) ----------
class ApprovalIn(MeshInput):
    """申請。申請できる種別は approval_types で定義する (access_request / external_share)。"""
    type: str = Field(max_length=50)
    asset_id: str = Field(max_length=36)
    reason: str = Field(min_length=1, max_length=1000, description="申請の理由。個人情報を含めないこと")
    purpose: str = Field(min_length=1, max_length=1000, description="利用目的 (社外提供は提供の目的)")
    expires_at: datetime = Field(description="閲覧や提供の期限 (最大 365 日。恒久は不可)")
    recipient: str | None = Field(default=None, max_length=200, description="external_share: 提供先の組織。個人情報を含めないこと")


class ApprovalDecisionIn(MeshInput):
    approve: bool
    comment: str = Field(default="", max_length=1000, description="却下のときは必須。個人情報を含めないこと")

    @model_validator(mode="after")
    def _reason_required(self) -> ApprovalDecisionIn:
        if not self.approve and not self.comment.strip():
            raise ValueError("却下には comment が必要です")
        return self


class ApprovalDecisionOut(ORM):
    step: int
    decided_by: str
    approve: bool
    comment: str
    decided_at: datetime


class ApprovalOut(ORM):
    id: str
    type: str
    target_type: str
    target_id: str
    asset_id: str | None
    requested_by: str
    reason: str
    payload: dict[str, Any]
    steps: list[dict[str, Any]]
    current_step: int
    status: ApprovalStatus
    due_at: datetime
    created_at: datetime
    decided_at: datetime | None
    decisions: list[ApprovalDecisionOut] = Field(default_factory=list)


class PolicyProposalIn(MeshInput):
    changes: dict[str, str | None] = Field(description="相対パス → YAML の本文。null はファイルの削除。指定しないファイルは active の版のまま")
    reason: str = Field(min_length=1, max_length=1000, description="変更の理由")


class PolicyRollbackIn(MeshInput):
    reason: str = Field(min_length=1, max_length=1000, description="切り戻しの理由")


class PolicyBundleOut(ORM):
    version: int
    status: str
    content_hash: str
    base_version: int | None
    scope: str
    domains: list[str]
    reason: str
    proposed_by: str
    created_at: datetime
    approval_id: str | None
    rollback_of: int | None
    approved_at: datetime | None
    published_by: str | None
    published_at: datetime | None
    superseded_at: datetime | None


class PolicyBundleDetail(PolicyBundleOut):
    files: dict[str, str]


class WebhookIn(MeshInput):
    url: str = Field(min_length=1, max_length=2000, description="配信先。本番では https のみ")
    secret_ref: str = Field(max_length=200, description="署名の秘密鍵の外部参照 (env:<環境変数名>)")
    event_types: list[str] = Field(default_factory=list, max_length=100, description="空なら全種別。末尾の * は前方一致 (例: edgf.issue.*)")
    description: str = Field(default="", max_length=1000)


class WebhookUpdate(MeshInput):
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    secret_ref: str | None = Field(default=None, max_length=200)
    event_types: list[str] | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    active: bool | None = None


class WebhookOut(ORM):
    id: str
    url: str
    secret_ref: str
    event_types: list[str]
    description: str
    active: bool
    created_by: str
    created_at: datetime


class DeliveryOut(ORM):
    id: str
    event_id: str
    endpoint_id: str
    status: str
    attempts: int
    next_attempt_at: datetime
    last_error: str | None
    delivered_at: datetime | None
    dead_at: datetime | None


class ReviewDecisionIn(MeshInput):
    keep: bool = Field(description="棚卸: 継続か取り消しか。証明: 正しいか修正が必要か")
    comment: str = Field(default="", max_length=1000, description="取り消しや修正が必要なときは必須")

    @model_validator(mode="after")
    def _reason_required(self) -> ReviewDecisionIn:
        if not self.keep and not self.comment.strip():
            raise ValueError("取り消しや修正が必要なときは comment が必要です")
        return self


class ReviewCampaignOut(ORM):
    id: str
    kind: str
    period: str
    status: str
    due_at: datetime
    grace_until: datetime
    item_count: int
    created_at: datetime
    closed_at: datetime | None


class ReviewItemOut(ORM):
    id: str
    campaign_id: str
    target_type: str
    target_id: str
    asset_id: str
    approval_id: str
    status: str
    decided_by: str | None
    decided_at: datetime | None


class PolicyReviewIn(MeshInput):
    note: str = Field(default="", max_length=1000)


# ---------------------------------------------------------------- §7 Discovery (Phase 15b)
# 値、サンプル、統計は受け付けない (§18.2 Control Plane はデータ本体を持たない)。名前で拒否して理由を明示する
FORBIDDEN_DATA_FIELDS = frozenset({
    "value", "values", "sample", "samples", "sample_values", "example", "examples", "rows", "records", "data", "preview",
    "min", "max", "mean", "avg", "average", "median", "stddev", "sum", "histogram", "distinct_count", "null_count", "top_values",
    "stats", "statistics", "profile", "distribution", "default", "default_value",
})


class _MetadataOnly(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_data(cls, data: Any) -> Any:
        if isinstance(data, dict):
            found = sorted(k for k in data if isinstance(k, str) and k.lower() in FORBIDDEN_DATA_FIELDS)
            if found:
                raise ValueError(f"値・サンプル・統計の項目は受け付けません (メタデータだけを送ってください): {', '.join(found)}")
        return data


class DiscoveredColumn(_MetadataOnly):
    name: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=100)
    nullable: bool | None = None


class DiscoveredAssetIn(_MetadataOnly):
    qualified_name: str = Field(min_length=1, max_length=300)
    kind: str | None = Field(default=None, max_length=30, description="table / view / file / stream など")
    schema_: list[DiscoveredColumn] = Field(default_factory=list, alias="schema", max_length=2000)
    updated_at: datetime | None = Field(default=None, description="Data Plane での最終更新時刻")


class DiscoverySnapshotIn(_MetadataOnly):
    """ソース (namespace) の全量のスナップショット。前回のスナップショットとの差分を取る。"""
    source: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/\-]+$")
    assets: list[DiscoveredAssetIn] = Field(max_length=100_000)

    @model_validator(mode="after")
    def _unique_names(self) -> DiscoverySnapshotIn:
        names = [a.qualified_name for a in self.assets]
        if len(names) != len(set(names)):
            raise ValueError("同じ qualified_name が重複しています")
        return self


class DiscoveredAssetOut(ORM):
    id: str
    namespace: str
    qualified_name: str
    kind: str | None
    schema_: list[dict[str, Any]] = Field(alias="schema")
    source_updated_at: datetime | None
    status: str
    first_seen_at: datetime
    last_seen_at: datetime
    missing_since: datetime | None
    registered: bool = False


class SchemaChangeOut(ORM):
    id: str
    asset_id: str
    namespace: str
    proposed_schema: list[dict[str, Any]]
    changes: list[dict[str, Any]]
    status: str
    issue_id: str | None
    created_at: datetime
    decided_by: str | None
    decided_at: datetime | None


# ---------------------------------------------------------------- §15 Addressable (Phase 15c)
class ReachabilityReportIn(BaseModel):
    """Data Plane からの到達の報告。detail は短い説明だけ (応答の本文や個人情報を入れない)。"""
    model_config = ConfigDict(extra="forbid")
    reachable: bool
    observed_at: datetime | None = Field(default=None, description="確認した時刻 (省略時は受け取った時刻。未来の時刻は受け取った時刻に丸める)")
    detail: str = Field(default="", max_length=200, pattern=r"^[A-Za-z0-9 _.:/\-]*$")


class ReachabilityOut(ORM):
    id: str
    port_id: str
    reachable: bool
    method: str
    observed_at: datetime
    detail: str
    reporter: str


# ---------------------------------------------------------------- §17 Usage (Phase 15d)
# 利用者を特定する項目は受け付けない。集計値 (件数と人数) だけを受け取る
FORBIDDEN_USAGE_FIELDS = frozenset({
    "user", "users", "user_id", "user_ids", "subject", "subjects", "principal", "principals", "account", "accounts", "email", "emails",
    "client_ip", "ip", "ip_address", "query", "query_text", "sql", "session_id", "device_id",
})


class UsageRecordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    qualified_name: str = Field(min_length=1, max_length=300)
    day: date
    query_count: int = Field(ge=0, le=10**12)
    user_count: int = Field(ge=0, le=10**9, description="その日の異なる利用者の数 (人数だけ。ID は送らない)")

    @model_validator(mode="before")
    @classmethod
    def _reject_identities(cls, data: Any) -> Any:
        if isinstance(data, dict):
            found = sorted(k for k in data if isinstance(k, str) and k.lower() in FORBIDDEN_USAGE_FIELDS)
            if found:
                raise ValueError(f"利用者を特定する項目は受け付けません (人数だけを送ってください): {', '.join(found)}")
        return data

    @model_validator(mode="after")
    def _consistent(self) -> UsageRecordIn:
        if self.user_count > self.query_count:
            raise ValueError("user_count は query_count 以下にしてください")
        return self


class UsagePushIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    records: list[UsageRecordIn] = Field(min_length=1, max_length=10_000)


# ---------------------------------------------------------------- §20 ドメイン別の成熟度の評価 (Phase 18)
class MaturityCreateIn(MeshInput):
    domain: str = Field(min_length=1, max_length=100)


class MaturityCancelIn(MeshInput):
    reason: str = Field(min_length=1, max_length=1000)
