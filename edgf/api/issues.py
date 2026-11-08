"""§16 Issue / Escalation API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.db import get_session
from edgf.auth import Principal
from edgf.deps import get_principal
from edgf.enums import IssueStatus
from edgf.models import DataAsset, Issue, utcnow
from edgf.pagination import Page, page_params, paginate
from edgf.schemas import IssueIn, IssueOut, IssueUpdate
from edgf.services import audit, issues

router = APIRouter(prefix="/issues", tags=["issues"])


def _get(db: Session, issue_id: str) -> Issue:
    i = db.get(Issue, issue_id)
    if not i:
        raise HTTPException(404, "issue not found")
    return i


@router.post("", response_model=IssueOut, status_code=201)
def create_issue(body: IssueIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    asset = db.get(DataAsset, body.asset_id) if body.asset_id else None
    if body.asset_id and asset is None:
        raise HTTPException(404, "asset not found")
    issue = issues.open_issue(db, principal.audit_actor, asset=asset, kind=body.kind, title=body.title,
                              description=body.description, severity=body.severity.value)
    if body.assignee:
        issue.assignee = body.assignee
    return issue


@router.get("", response_model=list[IssueOut])
def list_issues(request: Request, response: Response, status: str | None = None, asset_id: str | None = None,
                kind: str | None = None, escalation_level: str | None = None,
                page: Page = Depends(page_params()), db: Session = Depends(get_session)):
    stmt = select(Issue).order_by(Issue.created_at.desc(), Issue.id)
    for col, val in (("status", status), ("asset_id", asset_id), ("kind", kind), ("escalation_level", escalation_level)):
        if val:
            stmt = stmt.where(getattr(Issue, col) == val)
    return paginate(db, stmt, page, request, response)


@router.post("/escalate-overdue", response_model=list[IssueOut])
def escalate_overdue(db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """スケジューラから定期実行する想定 (SLA超過Issueの自動エスカレーション)。"""
    return issues.escalate_overdue(db, principal.audit_actor)


@router.patch("/{issue_id}", response_model=IssueOut)
def update_issue(issue_id: str, body: IssueUpdate, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    issue = _get(db, issue_id)
    if body.assignee is not None:
        issue.assignee = body.assignee
    if body.status is not None:
        issue.status = body.status.value
        if body.status in (IssueStatus.RESOLVED, IssueStatus.CLOSED) and issue.resolved_at is None:
            issue.resolved_at = utcnow()
        if body.status in (IssueStatus.OPEN, IssueStatus.IN_PROGRESS):
            issue.resolved_at = None
    db.flush()
    audit.record(db, principal.audit_actor, "issue.update", "issue", issue.id, body.model_dump(exclude_unset=True, mode="json"))
    return issue


@router.post("/{issue_id}/escalate", response_model=IssueOut)
def escalate_issue(issue_id: str, reason: str = "", db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    issue = _get(db, issue_id)
    try:
        return issues.escalate(db, principal.audit_actor, issue, reason)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
