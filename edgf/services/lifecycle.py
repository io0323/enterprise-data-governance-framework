"""§10 Data Lifecycle: 保持期限による削除の指示 (物理削除は Data Plane が実行する)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from edgf.enums import LIFECYCLE_TRANSITIONS, Certification, LifecycleState
from edgf.models import DataAsset, utcnow
from edgf.services import audit


class LegalHoldError(ValueError):
    """Legal Hold は削除より優先する (§10.2)。"""


def request_delete(db: Session, actor: str, asset: DataAsset, reason: str) -> None:
    """承認済みの削除を指示する。active / deprecated は状態遷移の規則に沿って archived を経由する。"""
    if asset.legal_hold:
        raise LegalHoldError("Legal Hold 中のため削除できません (§10.2)")
    current = LifecycleState(asset.lifecycle_state)
    if current == LifecycleState.DELETED:
        return
    if LifecycleState.DELETED not in LIFECYCLE_TRANSITIONS[current]:
        asset.lifecycle_state, asset.archived_at = LifecycleState.ARCHIVED.value, utcnow()
        audit.record(db, actor, "lifecycle.change", "asset", asset.id,
                     {"from": current.value, "to": LifecycleState.ARCHIVED.value, "reason": reason})
    previous = asset.lifecycle_state
    asset.lifecycle_state = LifecycleState.DELETED.value
    if asset.certification == Certification.CERTIFIED.value:
        asset.certification = Certification.DEPRECATED.value
    db.flush()
    audit.record(db, actor, "lifecycle.delete_requested", "asset", asset.id,
                 {"from": previous, "to": LifecycleState.DELETED.value, "reason": reason})
