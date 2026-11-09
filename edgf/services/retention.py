"""§10.2 Retention / §12 Retention: 保持期限の自動執行.

- Purpose の retention_days を過ぎた Consent を expired にする。Consent は追記のみなので、expired の行を追記する
- 資産の保持期限が来たら、削除の承認申請を作る。実際の削除指示は Owner の承認の後で行う。Legal Hold は削除より優先する
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from edgf.config import settings
from edgf.enums import ApprovalStatus, ConsentStatus, EscalationLevel, LifecycleState
from edgf.models import ApprovalRequest, Consent, DataAsset, PrivacyPurpose, utcnow
from edgf.services import approvals, audit, issues, outbox

SYSTEM = "system:jobs"
RETENTION_TYPE = "retention_delete"


def expire_consents(db: Session, actor: str, now: datetime | None = None) -> dict[str, Any]:
    """保持期間を過ぎた同意を失効させる。監査とイベントには件数だけを記録する (subject_ref も残さない。§12)。"""
    now = now or utcnow()
    latest = select(func.max(Consent.seq).label("seq")).group_by(Consent.subject_ref, Consent.purpose_id).subquery()
    rows = db.execute(select(Consent, PrivacyPurpose).join(latest, Consent.seq == latest.c.seq)
                      .join(PrivacyPurpose, Consent.purpose_id == PrivacyPurpose.id)
                      .where(Consent.status == ConsentStatus.GRANTED.value, PrivacyPurpose.retention_days.is_not(None))).all()
    counts: Counter[str] = Counter()
    for consent, purpose in rows:
        if consent.recorded_at + timedelta(days=purpose.retention_days) < now:
            db.add(Consent(subject_ref=consent.subject_ref, purpose_id=purpose.id, status=ConsentStatus.EXPIRED.value,
                           source="retention", recorded_by=SYSTEM))
            counts[purpose.id] += 1
    db.flush()
    for purpose_id, count in sorted(counts.items()):
        audit.record(db, actor, "consent.expire", "purpose", purpose_id, {"purpose_id": purpose_id, "count": count})
        outbox.emit(db, "consent.expired", purpose_id, {"purpose_id": purpose_id, "count": count})
    return {"expired": sum(counts.values()), "purposes": sorted(counts)}


def _open_request(db: Session, asset_id: str) -> ApprovalRequest | None:
    return db.scalar(select(ApprovalRequest).where(ApprovalRequest.type == RETENTION_TYPE, ApprovalRequest.asset_id == asset_id,
                                                   ApprovalRequest.status == ApprovalStatus.PENDING.value))


def _recently_rejected(db: Session, asset_id: str, now: datetime) -> bool:
    since = now - timedelta(days=settings.retention_reject_cooldown_days)
    return db.scalar(select(ApprovalRequest.id).where(
        ApprovalRequest.type == RETENTION_TYPE, ApprovalRequest.asset_id == asset_id, ApprovalRequest.status == ApprovalStatus.REJECTED.value,
        ApprovalRequest.decided_at >= since).limit(1)) is not None


def detect_due_assets(db: Session, actor: str, now: datetime | None = None) -> dict[str, Any]:
    """保持期限が来た資産の削除の承認申請を作る。何度実行しても申請は 1 件だけ。"""
    now = now or utcnow()
    created, no_owner = [], []
    assets = db.scalars(select(DataAsset).where(DataAsset.retention_days.is_not(None), DataAsset.lifecycle_state != LifecycleState.DELETED.value)
                        .order_by(DataAsset.qualified_name)).all()
    cancelled = []
    for asset in assets:
        current = _open_request(db, asset.id)
        if asset.legal_hold:
            if current is not None:  # 申請の後に Legal Hold が設定された
                approvals.cancel(db, actor, current, SYSTEM)
                cancelled.append(current.id)
            continue
        if current is not None or asset.created_at + timedelta(days=asset.retention_days) > now or _recently_rejected(db, asset.id, now):
            continue
        if not asset.owner:  # 承認者がいないので申請を作れない。Committee に知らせる
            if issues.open_issue(db, actor, asset=asset, kind="retention", severity="high", source_ref="retention:no-owner",
                                 level=EscalationLevel.COMMITTEE, title=f"[RETENTION] {asset.qualified_name}: 保持期限が来ましたが Owner が未割当です",
                                 description="Owner を割り当ててください。削除の承認申請を作れません (§10.2)。"):
                no_owner.append(asset.id)
            continue
        req = approvals.create(db, actor, type_name=RETENTION_TYPE, requested_by=SYSTEM, target_type="asset", target_id=asset.id, asset=asset,
                               reason="保持期限の到来 (§10.2)", payload={"retention_days": asset.retention_days, "created_at": asset.created_at.isoformat()})
        created.append(req.id)
    db.flush()
    return {"requests_created": created, "requests_cancelled": cancelled, "no_owner": no_owner}
