"""§16 Review / §11 定期棚卸 API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_asset_or_404, get_principal
from edgf.models import ApprovalRequest, DataAsset, ReviewCampaign, ReviewItem
from edgf.pagination import Page, page_params, paginate
from edgf.schemas import ReviewCampaignOut, ReviewDecisionIn, ReviewItemOut
from edgf.services import approvals
from edgf.services.approvals import ApprovalConflict

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.get("/campaigns", response_model=list[ReviewCampaignOut])
def list_campaigns(request: Request, response: Response, status: str | None = None, kind: str | None = None,
                   page: Page = Depends(page_params()), db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "review.read", entity_type="review_campaign", entity_id="*")
    stmt = select(ReviewCampaign).order_by(ReviewCampaign.created_at.desc(), ReviewCampaign.id)
    for column, value in ((ReviewCampaign.status, status), (ReviewCampaign.kind, kind)):
        if value is not None:
            stmt = stmt.where(column == value)
    return paginate(db, stmt, page, request, response)


@router.get("/items", response_model=list[ReviewItemOut])
def list_items(request: Request, response: Response, status: str | None = None, campaign_id: str | None = None,
               page: Page = Depends(page_params()), db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """admin と Committee は閲覧できる資産の全項目。それ以外は自分が Owner の資産の項目だけ。"""
    stmt = select(ReviewItem).join(DataAsset, ReviewItem.asset_id == DataAsset.id).order_by(ReviewItem.id)
    if authz.is_allowed(principal, "review.read"):
        stmt = stmt.where(authz.readable_clause(principal))
    else:
        stmt = stmt.where(DataAsset.owner == principal.subject)
    for column, value in ((ReviewItem.status, status), (ReviewItem.campaign_id, campaign_id)):
        if value is not None:
            stmt = stmt.where(column == value)
    return paginate(db, stmt, page, request, response)


@router.post("/items/{item_id}/decision", response_model=ReviewItemOut)
def decide_item(item_id: str, body: ReviewDecisionIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """対象資産の Owner の判断。棚卸は継続か取り消し、証明は正しいか修正が必要か。"""
    item = db.get(ReviewItem, item_id)
    if item is None:
        raise HTTPException(404, f"review item not found: {item_id}")
    asset = get_asset_or_404(db, item.asset_id)
    authz.require(db, principal, "review.decide", asset=asset, entity_type="review_item", entity_id=item.id)
    req = db.get(ApprovalRequest, item.approval_id, with_for_update=True)
    try:
        approvals.decide(db, principal, req, approve=body.keep, comment=body.comment)
    except ApprovalConflict as e:
        raise HTTPException(409, str(e)) from e
    return item
