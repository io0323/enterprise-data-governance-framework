"""§16 Approval API: 汎用の承認エンジンと共通の承認箱."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_asset_or_404, get_principal
from edgf.enums import ApprovalStatus, LifecycleState
from edgf.models import ApprovalDecision, ApprovalRequest, DataAsset, to_naive_utc, utcnow
from edgf.pagination import Page, page_params, paginate, paginate_list
from edgf.schemas import ApprovalDecisionIn, ApprovalDecisionOut, ApprovalIn, ApprovalOut
from edgf.services import approval_sources, approval_types, approvals
from edgf.services.approvals import ApprovalConflict

router = APIRouter(prefix="/approvals", tags=["approvals"])

MAX_DAYS = 365  # 恒久の承認は不可 (§13 Exception と同じ上限)


def _out(db: Session, req: ApprovalRequest) -> ApprovalOut:
    out = ApprovalOut.model_validate(req)
    out.decisions = [ApprovalDecisionOut.model_validate(d) for d in approvals.decisions_of(db, req)]
    return out


def _get(db: Session, approval_id: str, *, lock: bool = False) -> ApprovalRequest:
    # 判断は行ロックで直列化する (定足数の数え間違いと、同時の承認による確定の漏れを防ぐ)
    req = db.get(ApprovalRequest, approval_id, with_for_update=lock or None)
    if req is None:
        raise HTTPException(404, f"approval not found: {approval_id}")
    return req


@router.post("", response_model=ApprovalOut, status_code=201)
def request_approval(body: ApprovalIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """自分の申請を作る。申請できる種別は approval_types の requestable で定義する。"""
    authz.require(db, principal, "approval.request", entity_type="approval", entity_id=body.asset_id)
    definition = approval_types.TYPES.get(body.type)
    if definition is None or not definition.requestable:
        raise HTTPException(422, f"申請できない種別です: {body.type}")
    asset = get_asset_or_404(db, body.asset_id)
    if asset.lifecycle_state == LifecycleState.DELETED.value:
        raise HTTPException(409, "削除済みの資産には申請できません")
    if not asset.owner:
        raise HTTPException(409, "Owner が未割当の資産には申請できません (§16 承認者不明の禁止)")
    expires = to_naive_utc(body.expires_at)
    now = utcnow()
    if expires <= now or expires > now + timedelta(days=MAX_DAYS):
        raise HTTPException(422, f"expires_at は現在から{MAX_DAYS}日以内の未来である必要があります (恒久の承認は不可)")
    payload = {"purpose": body.purpose, "expires_at": expires.isoformat()}
    if body.type == "external_share":
        if not body.recipient:
            raise HTTPException(422, "external_share には recipient が必要です")
        authz.require_read(db, principal, asset)  # 閲覧できない資産の社外提供は申請できない
        payload["recipient"] = body.recipient
    if db.scalar(select(ApprovalRequest.id).where(ApprovalRequest.type == body.type, ApprovalRequest.asset_id == asset.id,
                                                  ApprovalRequest.requested_by == principal.subject,
                                                  ApprovalRequest.status == ApprovalStatus.PENDING.value)):
        raise HTTPException(409, "同じ資産への未決の申請があります")
    req = approvals.create(db, principal.audit_actor, type_name=body.type, requested_by=principal.subject, target_type="asset",
                           target_id=asset.id, asset=asset, reason=body.reason, payload=payload)
    return _out(db, req)


@router.get("/inbox")
def get_inbox(request: Request, response: Response, source: str | None = None, page: Page = Depends(page_params()),
              db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """自分が今判断できる案件。新しいエンジンと既存の個別の承認を集めて返す (判断は各案件の action で行う)。"""
    return paginate_list(approval_sources.inbox(db, principal, source), page, request, response)


@router.get("", response_model=list[ApprovalOut])
def list_approvals(request: Request, response: Response, status: str | None = None, type: str | None = None,
                   page: Page = Depends(page_params()), db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """自分の申請。admin と Committee は全件 (閲覧できない資産の案件は ABAC で除外する)。"""
    stmt = select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id)
    if authz.is_allowed(principal, "approval.read"):
        readable = select(DataAsset.id).where(authz.readable_clause(principal))
        stmt = stmt.where(or_(ApprovalRequest.asset_id.is_(None), ApprovalRequest.asset_id.in_(readable)))
    else:
        stmt = stmt.where(ApprovalRequest.requested_by == principal.subject)
    for column, value in ((ApprovalRequest.status, status), (ApprovalRequest.type, type)):
        if value is not None:
            stmt = stmt.where(column == value)
    return [_out(db, r) for r in paginate(db, stmt, page, request, response)]


@router.get("/{approval_id}", response_model=ApprovalOut)
def get_approval(approval_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    req = _get(db, approval_id)
    asset = db.get(DataAsset, req.asset_id) if req.asset_id else None
    decider = db.scalar(select(ApprovalDecision.id).where(ApprovalDecision.request_id == req.id, ApprovalDecision.decided_by == principal.subject))
    if principal.subject != req.requested_by and not decider and not approvals.can_decide(db, principal, req):
        authz.require(db, principal, "approval.read", entity_type="approval", entity_id=req.id)
        if asset is not None:
            authz.require_read(db, principal, asset)
    return _out(db, req)


@router.post("/{approval_id}/decision", response_model=ApprovalOut)
def decide_approval(approval_id: str, body: ApprovalDecisionIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    req = _get(db, approval_id, lock=True)
    try:
        approvals.decide(db, principal, req, approve=body.approve, comment=body.comment)
    except ApprovalConflict as e:
        raise HTTPException(409, str(e)) from e
    return _out(db, req)


@router.post("/{approval_id}/cancel", response_model=ApprovalOut)
def cancel_approval(approval_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    req = _get(db, approval_id, lock=True)
    authz.require(db, principal, "approval.cancel", relations=("requester",) if req.requested_by == principal.subject else (),
                  entity_type="approval", entity_id=req.id)
    try:
        approvals.cancel(db, principal.audit_actor, req, principal.subject)
    except ApprovalConflict as e:
        raise HTTPException(409, str(e)) from e
    return _out(db, req)
