"""§12 Consent Registry: 同意の履歴は追記のみで、現在の状態は最新の行で決める."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.enums import ConsentStatus, EscalationLevel, LegalBasis, LifecycleState
from edgf.models import Consent, DataAsset, PrivacyPurpose, utcnow
from edgf.services import issues
from edgf.services.privacy import pii_columns

NO_RECORD = "none"


def latest(db: Session, subject_ref: str, purpose_id: str) -> Consent | None:
    return db.scalar(select(Consent).where(Consent.subject_ref == subject_ref, Consent.purpose_id == purpose_id)
                     .order_by(Consent.seq.desc()).limit(1))


def effective_status(row: Consent | None, now: datetime | None = None) -> str:
    if row is None:
        return NO_RECORD
    if row.status == ConsentStatus.GRANTED.value and row.expires_at and row.expires_at <= (now or utcnow()):
        return ConsentStatus.EXPIRED.value  # 期限切れは読み取り時に判定する (行は更新しない)
    return row.status


def check(db: Session, subject_ref: str, purpose: PrivacyPurpose) -> dict[str, Any]:
    """目的内利用かどうかを判定する。同意以外の法的根拠を持つ目的は同意不要。"""
    base = {"purpose_id": purpose.id, "legal_basis": purpose.default_legal_basis}
    if purpose.default_legal_basis != LegalBasis.CONSENT.value:
        return {**base, "consent_required": False, "allowed": True, "status": None,
                "reason": f"法的根拠が {purpose.default_legal_basis} の目的のため、同意は不要です"}
    status = effective_status(latest(db, subject_ref, purpose.id))
    reason = {
        ConsentStatus.GRANTED.value: "有効な同意があります",
        ConsentStatus.WITHDRAWN.value: "同意は撤回されています",
        ConsentStatus.EXPIRED.value: "同意の期限が切れています",
        NO_RECORD: "同意の記録がありません",
    }[status]
    return {**base, "consent_required": True, "allowed": status == ConsentStatus.GRANTED.value, "status": status,
            "reason": reason}


def _relies_on_consent(column: dict[str, Any], purpose: PrivacyPurpose) -> bool:
    if purpose.id not in (column.get("processing_purposes") or []):
        return False
    return (column.get("legal_basis") or purpose.default_legal_basis) == LegalBasis.CONSENT.value


def assets_processing(db: Session, purpose: PrivacyPurpose) -> list[DataAsset]:
    """この目的を同意に基づいて処理している資産。"""
    assets = db.scalars(select(DataAsset).where(DataAsset.contains_pii.is_(True),
                                                DataAsset.lifecycle_state != LifecycleState.DELETED.value)
                        .order_by(DataAsset.qualified_name))
    return [a for a in assets if any(_relies_on_consent(c, purpose) for c in pii_columns(a.schema_))]


def open_withdrawal_issues(db: Session, actor: str, purpose: PrivacyPurpose) -> list[str]:
    """撤回を処理する資産の Owner に Issue を起票する (資産 × 目的で重複起票しない)。

    Issue にはデータ主体の情報を含めない。
    """
    opened = []
    for asset in assets_processing(db, purpose):
        issue = issues.open_issue(
            db, actor, asset=asset, kind="privacy", severity="high", source_ref=f"consent:{purpose.id}",
            level=EscalationLevel.OWNER,
            title=f"[CONSENT] {asset.qualified_name}: 目的 {purpose.id} の同意が撤回されました",
            description="撤回した主体のデータを、この目的で処理しないようにしてください (§12 目的外利用の遮断)。")
        if issue:
            opened.append(issue.id)
    return opened


def record(db: Session, *, subject_ref: str, purpose_id: str, status: str, recorded_by: str, source: str,
           expires_at: datetime | None) -> Consent:
    row = Consent(subject_ref=subject_ref, purpose_id=purpose_id, status=status, recorded_by=recorded_by,
                  source=source, expires_at=expires_at)
    db.add(row)
    db.flush()
    return row
