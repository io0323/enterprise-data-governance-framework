"""§17 KPI / Metrics, §20 Maturity indicator."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from edgf.enums import DataClass, DatasetVersionStatus, ProductStatus, RemediationStatus, SubscriptionStatus
from edgf.models import (AuditEvent, DataAsset, DataProduct, DatasetVersion, GoldenRecord, Issue, MdmReview, MdmXref, MeshSubscription,
                         ModelRemediation, utcnow)
from edgf.services.ai import card_complete
from edgf.services.ai_inherit import compute as compute_inherited, is_violation
from edgf.services.issues import OPEN_STATES
from edgf.services.mesh_datsis import passes as datsis_passes
from edgf.services.policy import evaluate_asset
from edgf.services.quality import latest_run


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def _ai_metrics(db: Session, assets: list[DataAsset], now: datetime) -> dict:
    """§14 AI 統制の KPI。成熟度の判定 (§20) には使わない。"""
    ai_assets = [a for a in assets if a.data_class == DataClass.AI.value]
    ids = [a.id for a in ai_assets]
    versions = db.scalars(select(DatasetVersion.status).where(DatasetVersion.asset_id.in_(ids),
                                                              DatasetVersion.status != DatasetVersionStatus.SUPERSEDED.value)).all()
    # accept の期限切れは、定期処理で pending に戻る前から未決として数える
    pending = db.scalar(select(func.count()).select_from(ModelRemediation).where(
        ModelRemediation.asset_id.in_(ids),
        or_(ModelRemediation.status == RemediationStatus.PENDING.value,
            and_(ModelRemediation.status == RemediationStatus.ACCEPTED.value, ModelRemediation.accept_until < now)))) or 0
    return {
        "ai_asset_count": len(ai_assets),
        "ai_training_approved_rate": _rate(versions.count(DatasetVersionStatus.APPROVED.value), len(versions)),
        "ai_remediation_pending_count": pending,
        "ai_card_complete_rate": _rate(sum(1 for a in ai_assets if card_complete(db, a)), len(ai_assets)),
        "ai_inheritance_violation_count": sum(1 for a in ai_assets if is_violation(a, compute_inherited(db, a))),
    }


def _mesh_metrics(db: Session, domain: str | None, asset_ids: list[str]) -> dict:
    """§15 Data Mesh の KPI。ドメイン別の内訳は /kpi/domains で返す。成熟度の判定 (§20) には使わない。"""
    stmt = select(DataProduct).where(DataProduct.status != ProductStatus.RETIRED.value)
    if domain:
        stmt = stmt.where(DataProduct.domain == domain)
    products = db.scalars(stmt).all()
    active = select(func.count()).select_from(MeshSubscription).join(DataProduct, MeshSubscription.product_id == DataProduct.id) \
        .where(MeshSubscription.status == SubscriptionStatus.ACTIVE.value)
    drift = select(func.count()).select_from(Issue).where(Issue.source_ref.like("mesh-drift:%"), Issue.status.in_(OPEN_STATES))
    if domain:
        active = active.where(DataProduct.domain == domain)
        drift = drift.where(Issue.asset_id.in_(asset_ids))
    rejected = db.scalars(select(AuditEvent.payload).where(AuditEvent.action == "mesh.contract.publish_rejected")).all()
    return {
        "mesh_product_published_count": sum(1 for p in products if p.status == ProductStatus.PUBLISHED.value),
        "mesh_datsis_pass_rate": _rate(sum(1 for p in products if datsis_passes(db, p)), len(products)),
        "mesh_breaking_rejected_count": sum(1 for r in rejected if r.get("breaking") and (not domain or r.get("domain") == domain)),
        "mesh_subscription_active_count": db.scalar(active) or 0,
        "mesh_schema_drift_issue_count": db.scalar(drift) or 0,
    }


def compute(db: Session, domain: str | None = None) -> dict:
    stmt = select(DataAsset).where(DataAsset.lifecycle_state != "deleted")
    if domain:
        stmt = stmt.where(DataAsset.domain == domain)
    assets = db.scalars(stmt).all()
    ids = [a.id for a in assets]
    n = len(assets)
    now = utcnow()

    owned = sum(1 for a in assets if a.owner)
    meta_complete = sum(1 for a in assets if a.owner and a.steward and a.description and a.domain)
    certified = sum(1 for a in assets if a.certification == "certified")

    scores, cde_scores = [], []
    for a in assets:
        run = latest_run(db, a.id)
        if run:
            scores.append(run.score)
            if a.is_cde:
                cde_scores.append(run.score)

    sla_assets = [a for a in assets if a.freshness_sla_hours]
    fresh = sum(1 for a in sla_assets if a.last_refreshed_at
                and now - a.last_refreshed_at <= timedelta(hours=a.freshness_sla_hours))

    applicable = compliant = 0
    for a in assets:
        res = evaluate_asset(db, a)
        applicable += len(res)
        compliant += sum(1 for r in res if r["status"] != "fail")

    issue_stmt = select(Issue)
    if domain:
        issue_stmt = issue_stmt.where(Issue.asset_id.in_(ids))
    all_issues = db.scalars(issue_stmt).all()
    open_issues = [i for i in all_issues if i.status in OPEN_STATES]
    resolved = [i for i in all_issues if i.resolved_at]
    mttr = (sum((i.resolved_at - i.created_at).total_seconds() for i in resolved) / len(resolved) / 86400) if resolved else None

    since = now - timedelta(days=30)
    usage_stmt = select(AuditEvent).where(AuditEvent.ts >= since, AuditEvent.entity_type == "asset")
    active_assets = {e.entity_id for e in db.scalars(usage_stmt)} & set(ids)

    links = db.scalars(select(MdmXref.link_type).join(GoldenRecord, MdmXref.golden_id == GoldenRecord.id)
                       .where(GoldenRecord.asset_id.in_(ids), MdmXref.tombstoned_at.is_(None))).all()
    auto_merged = links.count("auto_merge")
    steward_merged = links.count("steward_merge")
    pending_reviews = db.scalar(select(func.count()).select_from(MdmReview).where(
        MdmReview.status == "pending", MdmReview.asset_id.in_(ids))) or 0

    return {
        "scope": domain or "enterprise",
        "asset_count": n,
        "owner_assignment_rate": _rate(owned, n),          # 目標 100% (§1.8)
        "metadata_completeness": _rate(meta_complete, n),   # 目標 95%
        "certified_rate": _rate(certified, n),
        "quality_score_avg": round(sum(scores) / len(scores), 4) if scores else None,
        "cde_quality_score_avg": round(sum(cde_scores) / len(cde_scores), 4) if cde_scores else None,  # 目標 99%
        "freshness_rate": _rate(fresh, len(sla_assets)),    # 目標 98%
        "compliance_rate": _rate(compliant, applicable),    # 目標 100%
        "open_issue_count": len(open_issues),
        "overdue_issue_count": sum(1 for i in open_issues if i.due_at and i.due_at < now),
        "mean_time_to_resolve_days": round(mttr, 2) if mttr is not None else None,  # 目標 30日以内
        "governance_activity_assets_30d": len(active_assets),  # Usage 代理指標
        "mdm_review_pending_count": pending_reviews,  # §8 裁定キューの件数
        "mdm_duplicate_rate": _rate(auto_merged + steward_merged, len(links)),  # ソースのうち既存に統合された割合
        "mdm_auto_merge_rate": _rate(auto_merged, auto_merged + steward_merged),  # 統合のうち自動マージの割合
        **_ai_metrics(db, list(assets), now),
        **_mesh_metrics(db, domain, ids),
    }


def maturity_indicator(k: dict) -> dict:
    """§20 成熟度の自動判定 (一次指標。最終評価は Committee が実施)。"""
    def ge(key: str, v: float) -> bool:
        return (k.get(key) or 0) >= v

    level = 0
    if ge("owner_assignment_rate", 0.5):
        level = 1
    if level == 1 and ge("owner_assignment_rate", 0.8) and k.get("quality_score_avg") is not None:
        level = 2
    if level == 2 and ge("metadata_completeness", 0.95) and ge("compliance_rate", 0.9):
        level = 3
    if level == 3 and ge("certified_rate", 0.5) and ge("compliance_rate", 0.98):
        level = 4
    return {"indicative_level": level, "note": "Level 4以上の連邦統治・Level 5の自律統治は組織評価を併用して判定"}
