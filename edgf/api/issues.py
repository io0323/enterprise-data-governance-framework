"""§16 Issue / Escalation API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal, load_asset
from edgf.enums import IssueStatus
from edgf.models import DataAsset, Issue, utcnow
from edgf.pagination import Page, page_params, paginate
from edgf.schemas import IssueIn, IssueOut, IssueUpdate
from edgf.services import audit, issues, outbox

router = APIRouter(prefix="/issues", tags=["issues"])


def _get(db: Session, issue_id: str) -> Issue:
    i = db.get(Issue, issue_id)
    if not i:
        raise HTTPException(404, "issue not found")
    return i


def _authorize(db: Session, principal: Principal, issue: Issue, action: str) -> None:
    """担当者 (assignee)・対象資産の Owner/Steward・Committee が是正を進められる (§16 Issue)。"""
    asset = db.get(DataAsset, issue.asset_id) if issue.asset_id else None
    rels = {"assignee"} if issue.assignee and issue.assignee == principal.subject else set()
    authz.require(db, principal, action, asset=asset, relations=rels, entity_type="issue", entity_id=issue.id)


@router.post("", response_model=IssueOut, status_code=201)
def create_issue(body: IssueIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    # Consumer を含む全員が起票できる (§4.1 品質問題の報告)。ただし閲覧できる資産に限る
    authz.require(db, principal, "issue.create", entity_type="issue", entity_id=body.asset_id)
    asset = load_asset(db, principal, body.asset_id) if body.asset_id else None
    issue = issues.open_issue(db, principal.audit_actor, asset=asset, kind=body.kind, title=body.title,
                              description=body.description, severity=body.severity.value)
    if body.assignee:
        issue.assignee = body.assignee
    return issue


@router.get("", response_model=list[IssueOut])
def list_issues(request: Request, response: Response, status: str | None = None, asset_id: str | None = None,
                kind: str | None = None, escalation_level: str | None = None,
                page: Page = Depends(page_params()), db: Session = Depends(get_session),
                principal: Principal = Depends(get_principal)):
    readable = select(DataAsset.id).where(authz.readable_clause(principal))
    stmt = select(Issue).where(or_(Issue.asset_id.is_(None), Issue.asset_id.in_(readable))) \
        .order_by(Issue.created_at.desc(), Issue.id)
    for col, val in (("status", status), ("asset_id", asset_id), ("kind", kind), ("escalation_level", escalation_level)):
        if val:
            stmt = stmt.where(getattr(Issue, col) == val)
    return paginate(db, stmt, page, request, response)


@router.post("/escalate-overdue", response_model=list[IssueOut])
def escalate_overdue(db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """スケジューラから定期実行する想定 (SLA超過Issueの自動エスカレーション)。"""
    authz.require(db, principal, "issue.escalate_overdue", entity_type="issue", entity_id="*")
    return issues.escalate_overdue(db, principal.audit_actor)


@router.patch("/{issue_id}", response_model=IssueOut)
def update_issue(issue_id: str, body: IssueUpdate, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    issue = _get(db, issue_id)
    _authorize(db, principal, issue, "issue.update")
    if body.assignee is not None:
        issue.assignee = body.assignee
    if body.status is not None:
        issue.status = body.status.value
        if body.status in (IssueStatus.RESOLVED, IssueStatus.CLOSED) and issue.resolved_at is None:
            issue.resolved_at = utcnow()
        if body.status in (IssueStatus.OPEN, IssueStatus.IN_PROGRESS):
            issue.resolved_at = None
    db.flush()
    outbox.emit(db, "issue.updated", issue.id, {"issue_id": issue.id, "asset_id": issue.asset_id, "status": issue.status})
    audit.record(db, principal.audit_actor, "issue.update", "issue", issue.id, body.model_dump(exclude_unset=True, mode="json"))
    return issue


@router.post("/{issue_id}/escalate", response_model=IssueOut)
def escalate_issue(issue_id: str, reason: str = "", db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    issue = _get(db, issue_id)
    _authorize(db, principal, issue, "issue.escalate")
    try:
        return issues.escalate(db, principal.audit_actor, issue, reason)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
