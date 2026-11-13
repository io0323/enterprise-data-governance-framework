"""§16 Approval: 汎用の承認エンジン.

判断の記録は追記のみ。職務分離として、申請者本人は承認できず、対象資産の Custodian も承認できない (§4)。
1 人が同じ案件で判断できるのは 1 回だけなので、Owner と Committee の両方が必要な種別を同じ人が埋めることはできない。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.enums import ESCALATION_ORDER, ApprovalStatus, EscalationLevel
from edgf.models import ApprovalDecision, ApprovalRequest, DataAsset, Issue, utcnow
from edgf.services import approval_types, audit, issues, outbox
from edgf.services.approval_types import APPROVER_RULES, Step

A = ApprovalStatus
COMMITTEE_ASSIGNEE = "data-governance-committee"
BATCH = 500


class ApprovalValidationError(ValueError):
    """入力の不備 (422)。"""


class ApprovalConflict(ValueError):
    """現在の状態では実行できない操作 (409)。"""


def decisions_of(db: Session, req: ApprovalRequest) -> Sequence[ApprovalDecision]:
    return db.scalars(select(ApprovalDecision).where(ApprovalDecision.request_id == req.id)
                      .order_by(ApprovalDecision.id)).all()


def create(db: Session, actor: str, *, type_name: str, requested_by: str, target_type: str, target_id: str,
           asset: DataAsset | None = None, reason: str = "", payload: Mapping[str, Any] | None = None,
           steps: Sequence[Step] | None = None, due_at: datetime | None = None) -> ApprovalRequest:
    definition = approval_types.TYPES[type_name]
    resolved = tuple(steps) if steps is not None else definition.steps
    if not resolved:
        raise ApprovalValidationError(f"{type_name}: 承認のステップがありません (§16 承認者不明の禁止)")
    now = utcnow()
    req = ApprovalRequest(type=type_name, target_type=target_type, target_id=target_id, asset_id=asset.id if asset else None,
                          requested_by=requested_by, reason=reason, payload=dict(payload or {}),
                          steps=[s.as_dict() for s in resolved], current_step=0, status=A.PENDING.value,
                          due_at=due_at or approval_types.add_business_days(now, definition.sla_business_days))
    db.add(req)
    db.flush()
    audit.record(db, actor, "approval.request", "approval", req.id, {
        "type": type_name, "target_type": target_type, "target_id": target_id, "reason": reason, "payload": dict(payload or {}),
        "due_at": req.due_at})
    outbox.emit(db, "approval.requested", req.id, _event_data(req))
    return req


def _event_data(req: ApprovalRequest) -> dict[str, Any]:
    return {"approval_id": req.id, "type": req.type, "target_type": req.target_type, "target_id": req.target_id,
            "asset_id": req.asset_id, "status": req.status, "due_at": req.due_at}


def current_step(req: ApprovalRequest) -> dict[str, Any]:
    return req.steps[req.current_step]


def _can_decide_step(principal: Principal, step: Mapping[str, Any], asset: DataAsset | None) -> bool:
    return authz.rule_allows(principal, APPROVER_RULES[step["rule"]], asset=asset, domain=step.get("domain"))


def excluded(db: Session, principal: Principal, req: ApprovalRequest) -> str | None:
    """種別に固有の除外 (成熟度の判定の対象ドメインの Owner / Steward など)。除外の理由を返す。"""
    hook = approval_types.resolve(approval_types.TYPES[req.type].excludes)
    return hook(db, principal, req) if hook else None


def _decidable(principal: Principal, req: ApprovalRequest, asset: DataAsset | None) -> bool:
    if req.status != A.PENDING.value or principal.subject == req.requested_by:
        return False
    if asset is not None and asset.custodian == principal.subject:
        return False
    return _can_decide_step(principal, current_step(req), asset)


def can_decide(db: Session, principal: Principal, req: ApprovalRequest) -> bool:
    """この人が今のステップで判断できるか (inbox の判定と同じ)。"""
    asset = db.get(DataAsset, req.asset_id) if req.asset_id else None
    if not _decidable(principal, req, asset) or excluded(db, principal, req):
        return False
    return db.scalar(select(ApprovalDecision.id).where(ApprovalDecision.request_id == req.id,
                                                       ApprovalDecision.decided_by == principal.subject)) is None


def decidable_pending(db: Session, principal: Principal) -> list[tuple[ApprovalRequest, DataAsset | None]]:
    """今この人が判断できる未決の案件。inbox 用に一括で読む (案件ごとに問い合わせると N+1 になる)。"""
    requests = pending(db)
    asset_ids = sorted({r.asset_id for r in requests if r.asset_id})
    assets: dict[str, DataAsset] = {}
    for start in range(0, len(asset_ids), BATCH):
        assets.update({a.id: a for a in db.scalars(select(DataAsset).where(DataAsset.id.in_(asset_ids[start:start + BATCH])))})
    decided = set(db.scalars(select(ApprovalDecision.request_id).where(ApprovalDecision.decided_by == principal.subject)))
    return [(r, assets.get(r.asset_id)) for r in requests if r.id not in decided and _decidable(principal, r, assets.get(r.asset_id))
            and not excluded(db, principal, r)]


def _require_decider(db: Session, principal: Principal, req: ApprovalRequest) -> None:
    def deny(reason: str, required: Any = None) -> None:
        authz.deny(db, principal, "approval.decide", entity_type="approval", entity_id=req.id, reason=reason, required=required)

    if principal.subject == req.requested_by:
        deny("申請者本人は承認できません (職務分離)")
    asset = db.get(DataAsset, req.asset_id) if req.asset_id else None
    if asset is not None and asset.custodian == principal.subject:
        deny("Custodian は承認者になれません (§4 Business Ownership)")
    step = current_step(req)
    if not _can_decide_step(principal, step, asset):
        deny(f"このステップ ({step['name']}) を判断する権限がありません", APPROVER_RULES[step["rule"]])
    reason = excluded(db, principal, req)
    if reason:
        deny(reason)
    if db.scalar(select(ApprovalDecision.id).where(ApprovalDecision.request_id == req.id,
                                                   ApprovalDecision.decided_by == principal.subject)):
        deny("同じ案件を 2 回判断することはできません (職務分離)")
    authz.require_step_up(principal, "approval.decide")  # 判断者の確認のあと (権限の無い利用者には 403)


def _finish(db: Session, actor: str, req: ApprovalRequest, status: str, decided_by: str, handler: str | None) -> None:
    req.status, req.decided_at = status, utcnow()
    db.flush()
    audit.record(db, actor, f"approval.{status}", "approval", req.id, {"type": req.type, "target_id": req.target_id})
    outbox.emit(db, f"approval.{status}", req.id, _event_data(req))
    callback = approval_types.resolve(handler)
    if callback is not None:
        callback(db, actor, req, decided_by)
    db.flush()


def decide(db: Session, principal: Principal, req: ApprovalRequest, *, approve: bool, comment: str) -> None:
    if req.status != A.PENDING.value:
        raise ApprovalConflict(f"状態が {req.status} の案件は判断できません")
    _require_decider(db, principal, req)
    definition = approval_types.TYPES[req.type]
    step_index = req.current_step
    db.add(ApprovalDecision(request_id=req.id, step=step_index, decided_by=principal.subject, approve=approve, comment=comment))
    db.flush()
    audit.record(db, principal.audit_actor, "approval.decision", "approval", req.id, {
        "type": req.type, "step": current_step(req)["name"], "approve": approve, "comment": comment})
    if not approve:
        _finish(db, principal.audit_actor, req, A.REJECTED.value, principal.subject, definition.on_rejected)
        return
    approved = sum(1 for d in decisions_of(db, req) if d.step == step_index and d.approve)
    if approved < current_step(req)["quorum"]:
        return
    if step_index + 1 < len(req.steps):
        req.current_step = step_index + 1
        db.flush()
        return
    _finish(db, principal.audit_actor, req, A.APPROVED.value, principal.subject, definition.on_approved)


def cancel(db: Session, actor: str, req: ApprovalRequest, cancelled_by: str) -> None:
    if req.status != A.PENDING.value:
        raise ApprovalConflict(f"状態が {req.status} の案件は取り消せません")
    _finish(db, actor, req, A.CANCELLED.value, cancelled_by, approval_types.TYPES[req.type].on_cancelled)


def expire(db: Session, actor: str, req: ApprovalRequest) -> None:
    """期限で閉じる (棚卸の自動取り消しなど)。判断は記録しない。"""
    _finish(db, actor, req, A.EXPIRED.value, actor, None)


def pending(db: Session) -> Sequence[ApprovalRequest]:
    return db.scalars(select(ApprovalRequest).where(ApprovalRequest.status == A.PENDING.value)
                      .order_by(ApprovalRequest.due_at, ApprovalRequest.id)).all()


def escalate_overdue(db: Session, actor: str) -> dict[str, Any]:
    """SLA を超過した案件を Issue で起票する。Issue の期限も過ぎるとエスカレーションする (§16 Escalation)。"""
    now = utcnow()
    opened: list[str] = []
    escalated: list[str] = []
    overdue = [r for r in pending(db) if r.due_at < now and not approval_types.TYPES[r.type].sla_managed_by_source]
    for req in overdue:
        asset = db.get(DataAsset, req.asset_id) if req.asset_id else None
        ref = f"approval:{req.id}"
        committee = current_step(req)["rule"] == "committee" or asset is None
        issue = issues.open_issue(
            db, actor, asset=asset, kind="approval", severity="high", source_ref=ref,
            level=EscalationLevel.COMMITTEE if committee else EscalationLevel.OWNER,
            title=f"[APPROVAL] {req.type}: 承認が SLA を超過しています ({req.target_id})",
            description=f"ステップ: {current_step(req)['name']}。期限: {req.due_at.isoformat()} (§16 Approval)。")
        if issue:
            if issue.assignee is None:
                issue.assignee = COMMITTEE_ASSIGNEE
            issue.due_at = now + timedelta(days=max(approval_types.TYPES[req.type].sla_business_days, 1))
            opened.append(issue.id)
        stale = db.scalars(select(Issue).where(Issue.source_ref == ref, Issue.status.in_(issues.OPEN_STATES), Issue.due_at < now,
                                               Issue.escalation_level != ESCALATION_ORDER[-1].value)).all()
        for old in stale:
            issues.escalate(db, actor, old, "approval SLA overdue")
            escalated.append(old.id)
    db.flush()
    return {"overdue": [r.id for r in overdue], "issues_opened": opened, "issues_escalated": escalated}
