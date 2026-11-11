"""§16 Issue / Escalation."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.config import settings
from edgf.enums import ESCALATION_ORDER, EscalationLevel, IssueStatus
from edgf.models import DataAsset, Issue, utcnow
from edgf.services import audit, outbox

MAX_TITLE_LENGTH = 300
OPEN_STATES = (IssueStatus.OPEN.value, IssueStatus.IN_PROGRESS.value)


def assignee_for(asset: DataAsset | None, level: EscalationLevel) -> str | None:
    if asset is None:
        return None
    if level == EscalationLevel.STEWARD:
        return asset.steward or asset.owner
    if level == EscalationLevel.OWNER:
        return asset.owner
    return "data-governance-committee"


def open_issue(db: Session, actor: str, *, asset: DataAsset | None, kind: str, title: str,
               description: str = "", severity: str = "medium", source_ref: str | None = None,
               level: EscalationLevel = EscalationLevel.STEWARD, assignee: str | None = None) -> Issue | None:
    """重複起票防止: 同一 asset+source_ref の未解決Issueがあれば作成しない。

    assignee は資産に結び付かない Issue (未登録の資産など) の担当者。指定が無ければ資産の担当者から決める。
    """
    if source_ref:
        dup = db.scalar(select(Issue).where(Issue.asset_id == (asset.id if asset else None),
                                            Issue.source_ref == source_ref, Issue.status.in_(OPEN_STATES)))
        if dup:
            return None
    title = title if len(title) <= MAX_TITLE_LENGTH else f"{title[:MAX_TITLE_LENGTH - 1]}…"  # 連結で列の長さを超えないように
    issue = Issue(asset_id=asset.id if asset else None, kind=kind, title=title, description=description,
                  severity=severity, source_ref=source_ref,
                  assignee=assignee or assignee_for(asset, level) or assignee_for(asset, EscalationLevel.STEWARD),
                  escalation_level=level.value,
                  due_at=utcnow() + timedelta(days=settings.issue_sla_days))
    db.add(issue)
    db.flush()
    audit.record(db, actor, "issue.open", "issue", issue.id, {"title": title, "kind": kind, "asset_id": issue.asset_id})
    outbox.emit(db, "issue.opened", issue.id, {"issue_id": issue.id, "kind": kind, "severity": severity, "asset_id": issue.asset_id,
                                               "escalation_level": issue.escalation_level, "due_at": issue.due_at})
    return issue


def escalate(db: Session, actor: str, issue: Issue, reason: str) -> Issue:
    idx = ESCALATION_ORDER.index(EscalationLevel(issue.escalation_level))
    if idx == len(ESCALATION_ORDER) - 1:
        raise ValueError("already at committee level")
    nxt = ESCALATION_ORDER[idx + 1]
    asset = db.get(DataAsset, issue.asset_id) if issue.asset_id else None
    issue.escalation_level = nxt.value
    issue.assignee = assignee_for(asset, nxt) or issue.assignee
    issue.due_at = utcnow() + timedelta(days=max(settings.issue_sla_days // 3, 1))
    db.flush()
    audit.record(db, actor, "issue.escalate", "issue", issue.id, {"to": nxt.value, "reason": reason})
    outbox.emit(db, "issue.escalated", issue.id, {"issue_id": issue.id, "asset_id": issue.asset_id, "escalation_level": nxt.value})
    return issue


def escalate_overdue(db: Session, actor: str) -> list[Issue]:
    now = utcnow()
    targets = db.scalars(select(Issue).where(Issue.status.in_(OPEN_STATES), Issue.due_at < now,
                                             Issue.escalation_level != EscalationLevel.COMMITTEE.value)).all()
    return [escalate(db, actor, i, "SLA overdue") for i in targets]
