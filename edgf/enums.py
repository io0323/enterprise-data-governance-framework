"""Controlled vocabularies defined by the design document."""
from enum import Enum


class DataClass(str, Enum):
    """§3 Data Classification"""
    MASTER = "master"
    REFERENCE = "reference"
    TRANSACTIONAL = "transactional"
    ANALYTICAL = "analytical"
    OPERATIONAL = "operational"
    AI = "ai"
    IOT = "iot"
    METADATA = "metadata"


class Sensitivity(str, Enum):
    """§11 機密度4段階"""
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class LifecycleState(str, Enum):
    """§10 Data Lifecycle"""
    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"
    DELETED = "deleted"


LIFECYCLE_TRANSITIONS: dict[LifecycleState, set[LifecycleState]] = {
    LifecycleState.DRAFT: {LifecycleState.ACTIVE, LifecycleState.DELETED},
    LifecycleState.ACTIVE: {LifecycleState.DEPRECATED, LifecycleState.ARCHIVED},
    LifecycleState.DEPRECATED: {LifecycleState.ACTIVE, LifecycleState.ARCHIVED},
    LifecycleState.ARCHIVED: {LifecycleState.ACTIVE, LifecycleState.DELETED},
    LifecycleState.DELETED: set(),
}


class Certification(str, Enum):
    """§7 Certification"""
    DRAFT = "draft"
    CERTIFIED = "certified"
    DEPRECATED = "deprecated"


class QualityDimension(str, Enum):
    """§5 Data Quality Dimensions"""
    ACCURACY = "accuracy"
    COMPLETENESS = "completeness"
    CONSISTENCY = "consistency"
    TIMELINESS = "timeliness"
    VALIDITY = "validity"
    UNIQUENESS = "uniqueness"
    INTEGRITY = "integrity"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_WEIGHT = {Severity.LOW: 1, Severity.MEDIUM: 2, Severity.HIGH: 3, Severity.CRITICAL: 5}


class IssueStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class PiiCategory(str, Enum):
    """§12 PII のカテゴリ"""
    IDENTIFIER = "identifier"
    CONTACT = "contact"
    FINANCIAL = "financial"
    HEALTH = "health"
    LOCATION = "location"
    OTHER = "other"


class LegalBasis(str, Enum):
    """§12 処理の法的根拠 (GDPR 第6条をベースライン)"""
    CONSENT = "consent"
    CONTRACT = "contract"
    LEGAL_OBLIGATION = "legal_obligation"
    VITAL_INTEREST = "vital_interest"
    PUBLIC_TASK = "public_task"
    LEGITIMATE_INTEREST = "legitimate_interest"


class PrivacyTechnique(str, Enum):
    """§12 / §11 保護手法。pseudonymized は個人データのまま、anonymized は基準を満たす場合だけ統制外"""
    NONE = "none"
    MASKED = "masked"
    TOKENIZED = "tokenized"
    PSEUDONYMIZED = "pseudonymized"
    ANONYMIZED = "anonymized"
    AGGREGATED = "aggregated"


class ConsentStatus(str, Enum):
    """§12 Consent"""
    GRANTED = "granted"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"


class ErasureStatus(str, Enum):
    """§12 Right to Erasure の状態遷移"""
    RECEIVED = "received"
    SCOPED = "scoped"
    APPROVED = "approved"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    VERIFIED = "verified"
    CLOSED = "closed"
    REJECTED = "rejected"


class ErasureTaskStatus(str, Enum):
    PLANNED = "planned"              # スコープ済み。承認と発行を待っている
    PENDING = "pending"              # 発行済み。Data Plane の応答待ち
    ACKNOWLEDGED = "acknowledged"
    COMPLETED = "completed"
    FAILED = "failed"
    WITHHELD = "withheld"            # Legal Hold などにより削除しない (§10.2)
    CANCELLED = "cancelled"          # 要求の却下による取消


class AiKind(str, Enum):
    """§14 AI 資産の種別 (data_class=ai)。実体は Data Plane にあり、Control Plane はメタデータだけを持つ (§18.2)"""
    TRAINING_DATASET = "training_dataset"
    EMBEDDING_SET = "embedding_set"
    VECTOR_INDEX = "vector_index"
    RAG_CORPUS = "rag_corpus"
    MODEL = "model"


class DatasetVersionStatus(str, Enum):
    """§14 Training Data: 学習データの版"""
    DRAFT = "draft"
    APPROVED = "approved"
    SUPERSEDED = "superseded"        # 以降の学習には使えない


class ModelVersionStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    DEPLOYED = "deployed"
    SUSPENDED = "suspended"
    RETIRED = "retired"


class TrainingApprovalStatus(str, Enum):
    """§14 Training Data / §13 Approval: 学習利用の承認"""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class EscalationLevel(str, Enum):
    """§16 Escalation: Steward -> Owner -> Committee"""
    STEWARD = "steward"
    OWNER = "owner"
    COMMITTEE = "committee"


ESCALATION_ORDER = [EscalationLevel.STEWARD, EscalationLevel.OWNER, EscalationLevel.COMMITTEE]
