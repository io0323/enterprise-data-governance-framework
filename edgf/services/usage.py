"""§17 Usage: 利用実績の集計値の取り込み、KPI、未使用の資産の検出.

受け取るのは「資産 × 日 × 問い合わせの件数 × 利用者の数」だけ。利用者を特定する ID は入力の検証で拒否する。
同じ資産と日の再送は上書きする (Data Plane が集計し直して送り直せるように冪等にする)。
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from edgf.config import settings
from edgf.enums import EscalationLevel
from edgf.models import DataAsset, UsageDaily, utcnow
from edgf.services import audit, issues

SYSTEM_ACTOR = "system:usage"
ISSUE_KIND = "usage"
INACTIVE_STATES = ("deleted", "archived")


def store(db: Session, actor: str, rows: Sequence[tuple[DataAsset, date, int, int]]) -> dict[str, int]:
    created = updated = 0
    for asset, day, queries, users in rows:
        existing = db.scalar(select(UsageDaily).where(UsageDaily.asset_id == asset.id, UsageDaily.day == day))
        if existing is None:
            db.add(UsageDaily(asset_id=asset.id, day=day, query_count=queries, user_count=users, reporter=actor))
            created += 1
        else:
            existing.query_count, existing.user_count, existing.reporter, existing.received_at = queries, users, actor, utcnow()
            updated += 1
    db.flush()
    audit.record(db, actor, "usage.push", "usage", "-", {"records": len(rows), "created": created, "updated": updated,
                                                        "assets": len({a.id for a, *_ in rows})})
    return {"created": created, "updated": updated}


def _scoped_assets(db: Session, asset_ids: Sequence[str] | None) -> list[DataAsset]:
    stmt = select(DataAsset).where(DataAsset.lifecycle_state.not_in(INACTIVE_STATES))
    if asset_ids is not None:
        stmt = stmt.where(DataAsset.id.in_(list(asset_ids)))
    return list(db.scalars(stmt))


def tracking_started(db: Session) -> bool:
    """利用実績が 1 件でもあるか。無ければ未使用の判定はしない (送っていない環境で全資産を未使用にしない)。"""
    return db.scalar(select(UsageDaily.id).limit(1)) is not None


def last_used(db: Session, asset_ids: Sequence[str]) -> dict[str, date]:
    if not asset_ids:
        return {}
    rows = db.execute(select(UsageDaily.asset_id, func.max(UsageDaily.day)).where(UsageDaily.asset_id.in_(list(asset_ids)), UsageDaily.query_count > 0)
                      .group_by(UsageDaily.asset_id)).all()
    return dict(rows)


def unused_assets(db: Session, asset_ids: Sequence[str] | None = None, today: date | None = None) -> list[DataAsset]:
    """EDGF_UNUSED_DAYS のあいだ利用の無い資産。登録から日が浅い資産は含めない。"""
    today = today or utcnow().date()
    since = today - timedelta(days=settings.unused_days)
    assets = [a for a in _scoped_assets(db, asset_ids) if a.created_at.date() <= since]
    used = last_used(db, [a.id for a in assets])
    return [a for a in assets if used.get(a.id) is None or used[a.id] < since]


def metrics(db: Session, asset_ids: Sequence[str], today: date | None = None) -> dict[str, Any]:
    """KPI (§17 Usage)。利用実績がまだ無ければ値は None。"""
    if not tracking_started(db):
        return {"usage_active_assets_30d": None, "usage_query_count_30d": None, "unused_asset_count": None}
    today = today or utcnow().date()
    since = today - timedelta(days=30)
    window = select(UsageDaily).where(UsageDaily.asset_id.in_(list(asset_ids)), UsageDaily.day > since, UsageDaily.day <= today) if asset_ids else None
    rows = list(db.scalars(window)) if window is not None else []
    return {"usage_active_assets_30d": len({r.asset_id for r in rows if r.query_count > 0}),
            "usage_query_count_30d": sum(r.query_count for r in rows),
            "unused_asset_count": len(unused_assets(db, asset_ids, today))}


def open_unused_issues(db: Session, actor: str = SYSTEM_ACTOR, today: date | None = None) -> dict[str, int]:
    """未使用の資産を、廃止の候補として Owner に Issue で知らせる (重複起票はしない)。"""
    if not tracking_started(db):
        return {"candidates": 0, "opened": 0}
    candidates = unused_assets(db, None, today)
    opened = 0
    for asset in candidates:
        issue = issues.open_issue(db, actor, asset=asset, kind=ISSUE_KIND, title=f"{settings.unused_days} 日間使われていません (廃止の候補): {asset.qualified_name}",
                                  description="利用実績の集計で、この期間に問い合わせがありませんでした。廃止するか、必要な理由を記録してください (§17 Usage、§10 Lifecycle)",
                                  severity="low", source_ref="usage-unused", level=EscalationLevel.OWNER)
        opened += issue is not None
    return {"candidates": len(candidates), "opened": opened}
