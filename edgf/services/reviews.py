"""§16 Review / §11 定期棚卸: レビューキャンペーン.

- access_review (四半期): 有効な AccessGrant と利用登録を、対象資産の Owner が継続か取り消しか判断する
- ownership_attestation (年次): 各資産の Owner が、Owner・Steward・分類・保持期間が正しいことを証明する
- policy_review (年次): 365 日以上見直されていない Policy の Issue を起票する

判断は承認エンジンの案件で行うので、inbox にそのまま出る。期限までに対応されなければ Issue でエスカレーションし、
猶予期間を過ぎた権限は自動で取り消す (棚卸なき権限の堆積の防止。§11.2)。処理はすべて冪等。
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.config import settings
from edgf.enums import ESCALATION_ORDER, BundleStatus, EscalationLevel, IssueStatus, LifecycleState, SubscriptionStatus
from edgf.models import (AccessGrant, ApprovalRequest, DataAsset, Issue, MeshSubscription, OutputPort, PolicyBundle, PolicyReview,
                         ReviewCampaign, ReviewItem, utcnow)
from edgf.services import approvals, audit, issues, outbox, policy

ACCESS, OWNERSHIP, POLICY = "access_review", "ownership_attestation", "policy_review"
SYSTEM = "system:jobs"
POLICY_REVIEW_DAYS = 365
GRACE_FREE = (OWNERSHIP,)  # 猶予を過ぎても権限を取り消さない種別


def period_of(kind: str, now: datetime) -> str:
    return f"{now.year}-Q{(now.month - 1) // 3 + 1}" if kind == ACCESS else str(now.year)


def _subscription_asset(db: Session, row: MeshSubscription) -> str:
    return db.get(OutputPort, row.output_port_id).asset_id


def _add_item(db: Session, actor: str, campaign: ReviewCampaign, type_name: str, target_type: str, target_id: str, asset: DataAsset,
              reason: str, payload: dict[str, Any]) -> None:
    req = approvals.create(db, actor, type_name=type_name, requested_by=SYSTEM, target_type=target_type, target_id=target_id, asset=asset,
                           reason=reason, payload=payload, due_at=campaign.due_at)
    db.add(ReviewItem(campaign_id=campaign.id, target_type=target_type, target_id=target_id, asset_id=asset.id, approval_id=req.id))
    campaign.item_count += 1


def _build_access_items(db: Session, actor: str, campaign: ReviewCampaign, now: datetime) -> None:
    grants = db.scalars(select(AccessGrant).where(AccessGrant.revoked_at.is_(None), AccessGrant.expires_at > now).order_by(AccessGrant.id)).all()
    for grant in grants:
        _add_item(db, actor, campaign, ACCESS, "access_grant", grant.id, db.get(DataAsset, grant.asset_id),
                  f"権限の棚卸 ({campaign.period})", {"subject": grant.subject, "purpose": grant.purpose, "expires_at": grant.expires_at.isoformat()})
    rows = db.scalars(select(MeshSubscription).where(MeshSubscription.status == SubscriptionStatus.ACTIVE.value).order_by(MeshSubscription.id)).all()
    for row in rows:
        _add_item(db, actor, campaign, ACCESS, "subscription", row.id, db.get(DataAsset, _subscription_asset(db, row)),
                  f"権限の棚卸 ({campaign.period})", {"subject": row.subscriber, "purpose_id": row.purpose_id, "major": row.major})


def _build_ownership_items(db: Session, actor: str, campaign: ReviewCampaign) -> None:
    assets = db.scalars(select(DataAsset).where(DataAsset.lifecycle_state != LifecycleState.DELETED.value).order_by(DataAsset.qualified_name)).all()
    for asset in assets:
        _add_item(db, actor, campaign, OWNERSHIP, "asset", asset.id, asset, f"Owner の証明 ({campaign.period})", {
            "owner": asset.owner, "steward": asset.steward, "sensitivity": asset.sensitivity, "retention_days": asset.retention_days})


def ensure_campaign(db: Session, actor: str, kind: str, now: datetime | None = None) -> ReviewCampaign | None:
    """今の期間のキャンペーンを作る。既にあれば何もしない (冪等)。"""
    now = now or utcnow()
    period = period_of(kind, now)
    if db.scalar(select(ReviewCampaign.id).where(ReviewCampaign.kind == kind, ReviewCampaign.period == period)):
        return None
    due = now + timedelta(days=settings.review_due_days)
    campaign = ReviewCampaign(kind=kind, period=period, due_at=due, grace_until=due + timedelta(days=settings.access_review_grace_days))
    db.add(campaign)
    db.flush()
    if kind == ACCESS:
        _build_access_items(db, actor, campaign, now)
    elif kind == OWNERSHIP:
        _build_ownership_items(db, actor, campaign)
    db.flush()
    audit.record(db, actor, "review.campaign_create", "review_campaign", campaign.id,
                 {"kind": kind, "period": period, "items": campaign.item_count, "due_at": due})
    outbox.emit(db, "review.campaign_created", campaign.id, {"campaign_id": campaign.id, "kind": kind, "period": period, "due_at": due})
    return campaign


# ---------------------------------------------------------------- 判断の結果
def item_of(db: Session, approval_id: str) -> ReviewItem:
    return db.scalar(select(ReviewItem).where(ReviewItem.approval_id == approval_id))


def _ref(item: ReviewItem) -> str:
    return f"review:{item.id}"


def _close(db: Session, item: ReviewItem, status: str, decided_by: str | None) -> None:
    """判断されたら項目を閉じ、期限超過の Issue も解決する (期限なき Issue の滞留を防ぐ)。"""
    item.status, item.decided_by, item.decided_at = status, decided_by, utcnow()
    for issue in db.scalars(select(Issue).where(Issue.source_ref == _ref(item), Issue.status.in_(issues.OPEN_STATES))):
        issue.status, issue.resolved_at = IssueStatus.RESOLVED.value, utcnow()
    db.flush()


def _revoke_target(db: Session, actor: str, item: ReviewItem, revoked_by: str, reason: str) -> None:
    now = utcnow()
    if item.target_type == "access_grant":
        grant = db.get(AccessGrant, item.target_id)
        if grant.revoked_at is not None:
            return
        grant.revoked_at, grant.revoked_by = now, revoked_by
        subject = grant.subject
    else:
        row = db.get(MeshSubscription, item.target_id)
        if row.status not in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.PENDING.value):
            return
        row.status, row.suspended_reason = SubscriptionStatus.CANCELLED.value, reason
        subject = row.subscriber
    db.flush()
    audit.record(db, actor, "access.revoke", "asset", item.asset_id, {
        "target_type": item.target_type, "target_id": item.target_id, "subject": subject, "reason": reason, "review_item_id": item.id})
    outbox.emit(db, "access.revoked", item.target_id, {"target_type": item.target_type, "target_id": item.target_id,
                                                       "asset_id": item.asset_id, "subject": subject, "review_item_id": item.id})


def on_keep(db: Session, actor: str, req: ApprovalRequest, decided_by: str) -> None:
    _close(db, item_of(db, req.id), "kept", decided_by)


def on_revoke(db: Session, actor: str, req: ApprovalRequest, decided_by: str) -> None:
    item = item_of(db, req.id)
    _revoke_target(db, actor, item, decided_by, "棚卸で取り消し")
    _close(db, item, "revoked", decided_by)


def on_attested(db: Session, actor: str, req: ApprovalRequest, decided_by: str) -> None:
    _close(db, item_of(db, req.id), "attested", decided_by)


def on_disputed(db: Session, actor: str, req: ApprovalRequest, decided_by: str) -> None:
    """Owner が内容を正しくないと判断した。修正は Issue で追跡する。"""
    item = item_of(db, req.id)
    _close(db, item, "disputed", decided_by)
    asset = db.get(DataAsset, item.asset_id)
    issues.open_issue(db, actor, asset=asset, kind="review", severity="medium", source_ref=f"review-dispute:{item.id}",
                      title=f"[REVIEW] {asset.qualified_name}: Owner の証明で修正が必要と判断されました",
                      description="Owner・Steward・分類・保持期間を確認して修正してください (§16 Review)。")


def on_cancelled(db: Session, actor: str, req: ApprovalRequest, decided_by: str) -> None:
    _close(db, item_of(db, req.id), "cancelled", decided_by)


# ---------------------------------------------------------------- 期限の処理
def _open_overdue_issue(db: Session, actor: str, item: ReviewItem, campaign: ReviewCampaign) -> str | None:
    asset = db.get(DataAsset, item.asset_id)
    issue = issues.open_issue(
        db, actor, asset=asset, kind="review", severity="high", source_ref=_ref(item), level=EscalationLevel.OWNER,
        title=f"[REVIEW] {asset.qualified_name}: {campaign.kind} ({campaign.period}) が期限を超過しています",
        description=f"猶予の期限: {campaign.grace_until.isoformat()}。" + (
            "証明が無いまま猶予を過ぎると Committee にエスカレーションします。" if campaign.kind in GRACE_FREE else "猶予を過ぎると権限を自動で取り消します。"))
    if issue is None:
        return None
    issue.due_at = campaign.grace_until
    return issue.id


def _past_grace(db: Session, actor: str, item: ReviewItem, campaign: ReviewCampaign) -> str:
    req = db.get(ApprovalRequest, item.approval_id)
    approvals.expire(db, actor, req)
    if campaign.kind in GRACE_FREE:
        item.status = "unattested"
        result = "unattested"
    else:
        _revoke_target(db, actor, item, actor, "棚卸の期限を過ぎたため自動で取り消し")
        item.status = "auto_revoked"
        result = "auto_revoked"
        audit.record(db, actor, "review.auto_revoke", "review_item", item.id, {
            "target_type": item.target_type, "target_id": item.target_id, "asset_id": item.asset_id, "campaign_id": campaign.id})
    item.decided_at = utcnow()
    db.flush()
    return result


def process_deadlines(db: Session, actor: str, now: datetime | None = None) -> dict[str, Any]:
    """期限を過ぎた項目を処理する。冪等で、同じ項目に Issue を重ねて起票しない。"""
    now = now or utcnow()
    counts = {"issues_opened": 0, "issues_escalated": 0, "auto_revoked": 0, "unattested": 0, "campaigns_closed": 0}
    for campaign in db.scalars(select(ReviewCampaign).where(ReviewCampaign.status == "open").order_by(ReviewCampaign.created_at)).all():
        items = db.scalars(select(ReviewItem).where(ReviewItem.campaign_id == campaign.id, ReviewItem.status == "pending")).all()
        if now > campaign.grace_until:
            for item in items:
                counts[_past_grace(db, actor, item, campaign)] += 1
                for issue in db.scalars(select(Issue).where(Issue.source_ref == _ref(item), Issue.status.in_(issues.OPEN_STATES),
                                                            Issue.escalation_level != ESCALATION_ORDER[-1].value)).all():
                    issues.escalate(db, actor, issue, "review grace period ended")
                    counts["issues_escalated"] += 1
        elif now > campaign.due_at:
            counts["issues_opened"] += sum(1 for item in items if _open_overdue_issue(db, actor, item, campaign))
        remaining = db.scalar(select(ReviewItem.id).where(ReviewItem.campaign_id == campaign.id, ReviewItem.status == "pending").limit(1))
        if remaining is None and (campaign.item_count or now > campaign.due_at):
            campaign.status, campaign.closed_at = "closed", now
            counts["campaigns_closed"] += 1
    db.flush()
    return counts


# ---------------------------------------------------------------- Policy の見直し
def policy_reviewed_at(db: Session) -> dict[str, datetime]:
    """active の Policy ごとの最後の見直しの日時。定義が変わった版の公開か、見直しの記録のうち新しい方。"""
    bundles = db.scalars(select(PolicyBundle).where(PolicyBundle.published_at.is_not(None)).order_by(PolicyBundle.published_at, PolicyBundle.version)).all()
    since: dict[str, datetime] = {}
    previous: dict[str, dict] = {}
    active: Sequence[str] = []
    for bundle in bundles:
        definitions = {p.id: p.as_dict() for p in policy.parse_files(bundle.files)}
        for pid, definition in definitions.items():
            if previous.get(pid) != definition:
                since[pid] = bundle.published_at
        previous = definitions
        if bundle.status == BundleStatus.ACTIVE.value:
            active = list(definitions)
    reviewed = {pid: since[pid] for pid in active}
    for row in db.scalars(select(PolicyReview)):
        if row.policy_id in reviewed and row.reviewed_at > reviewed[row.policy_id]:
            reviewed[row.policy_id] = row.reviewed_at
    return reviewed


def policy_review_scan(db: Session, actor: str, now: datetime | None = None) -> list[str]:
    """365 日以上見直されていない Policy の Issue を起票する (重複起票しない)。"""
    now = now or utcnow()
    opened = []
    for pid, reviewed in sorted(policy_reviewed_at(db).items()):
        if now - reviewed < timedelta(days=POLICY_REVIEW_DAYS):
            continue
        issue = issues.open_issue(db, actor, asset=None, kind="review", severity="medium", source_ref=f"policy-review:{pid}",
                                  level=EscalationLevel.COMMITTEE, title=f"[REVIEW] {pid}: {POLICY_REVIEW_DAYS} 日以上見直されていません",
                                  description=f"最後の見直し: {reviewed.date().isoformat()}。POST /policies/{pid}/review で記録してください (§16 Policy)。")
        if issue:
            issue.assignee = approvals.COMMITTEE_ASSIGNEE
            opened.append(issue.id)
    db.flush()
    return opened


def record_policy_review(db: Session, actor: str, policy_id: str, *, reviewed_by: str, note: str) -> PolicyReview:
    row = PolicyReview(policy_id=policy_id, reviewed_by=reviewed_by, note=note)
    db.add(row)
    now = utcnow()
    for issue in db.scalars(select(Issue).where(Issue.source_ref == f"policy-review:{policy_id}", Issue.status.in_(issues.OPEN_STATES))):
        issue.status, issue.resolved_at = IssueStatus.RESOLVED.value, now
    db.flush()
    audit.record(db, actor, "policy.review", "policy", policy_id, {"note": note, "reviewed_by": reviewed_by})
    return row


def run_campaigns(db: Session, actor: str, now: datetime | None = None) -> dict[str, Any]:
    """定期実行の入口。キャンペーンを作り、期限を処理する。何度実行しても結果は同じ。"""
    now = now or utcnow()
    created = [c.kind for c in (ensure_campaign(db, actor, kind, now) for kind in (ACCESS, OWNERSHIP, POLICY)) if c is not None]
    opened = policy_review_scan(db, actor, now)
    return {"campaigns_created": created, "policy_issues_opened": len(opened), **process_deadlines(db, actor, now)}
