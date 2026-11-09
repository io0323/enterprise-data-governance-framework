"""承認エンジンの種別ごとの処理 (approval_types.py から名前で参照する)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from edgf.models import AccessGrant, ApprovalRequest, DataAsset
from edgf.services import audit, lifecycle, outbox
from edgf.services.approvals import ApprovalConflict


def grant_access(db: Session, actor: str, req: ApprovalRequest, decided_by: str) -> None:
    """access_request: AccessGrant を作る (既存の閲覧承認と同じ記録)。"""
    grant = AccessGrant(asset_id=req.asset_id, subject=req.requested_by, purpose=req.payload["purpose"], granted_by=decided_by,
                        expires_at=datetime.fromisoformat(req.payload["expires_at"]))
    db.add(grant)
    db.flush()
    audit.record(db, actor, "access.grant", "asset", req.asset_id, {
        "grant_id": grant.id, "subject": grant.subject, "purpose": grant.purpose, "expires_at": grant.expires_at,
        "approval_id": req.id})
    outbox.emit(db, "access.granted", grant.id, {"grant_id": grant.id, "asset_id": grant.asset_id, "subject": grant.subject,
                                                 "expires_at": grant.expires_at})


def approve_share(db: Session, actor: str, req: ApprovalRequest, decided_by: str) -> None:
    """external_share: 実際の提供は Data Plane が行う。ここでは承認の証跡を残す。"""
    audit.record(db, actor, "share.approved", "asset", req.asset_id, {
        "approval_id": req.id, "recipient": req.payload["recipient"], "purpose": req.payload["purpose"],
        "expires_at": req.payload["expires_at"], "approved_by": decided_by})
    outbox.emit(db, "share.approved", req.id, {"approval_id": req.id, "asset_id": req.asset_id, "recipient": req.payload["recipient"],
                                               "expires_at": req.payload["expires_at"]})


def delete_asset(db: Session, actor: str, req: ApprovalRequest, decided_by: str) -> None:
    """retention_delete: 既存の lifecycle.delete_requested につなぐ。Legal Hold は削除より優先する (§10.2)。"""
    try:
        lifecycle.request_delete(db, actor, db.get(DataAsset, req.asset_id), f"保持期限による削除 (承認 {req.id})")
    except lifecycle.LegalHoldError as e:
        raise ApprovalConflict(str(e)) from e
