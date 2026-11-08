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


class ConsentStatus(str, Enum):
    """§12 Consent"""
    GRANTED = "granted"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"


class EscalationLevel(str, Enum):
    """§16 Escalation: Steward -> Owner -> Committee"""
    STEWARD = "steward"
    OWNER = "owner"
    COMMITTEE = "committee"


ESCALATION_ORDER = [EscalationLevel.STEWARD, EscalationLevel.OWNER, EscalationLevel.COMMITTEE]
