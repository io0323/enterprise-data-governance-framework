"""§17 Usage: Data Plane からの利用実績 (集計値) の受け取り."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal
from edgf.errors import ProblemError
from edgf.models import DataAsset, utcnow
from edgf.schemas import UsagePushIn
from edgf.services import usage

router = APIRouter(prefix="/usage", tags=["usage"])
MAX_AGE_DAYS = 400


@router.post("/daily")
def push_usage(body: UsagePushIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """資産 × 日の集計値を受け取る。同じ資産と日の再送は上書きする。Catalog に無い資産は保存せずに返す。"""
    today = utcnow().date()
    for r in body.records:
        if r.day > today or (today - r.day).days > MAX_AGE_DAYS:
            raise ProblemError(422, f"day は今日から {MAX_AGE_DAYS} 日前までの日付にしてください", type_="validation-error")
    names = sorted({r.qualified_name for r in body.records})
    assets = {a.qualified_name: a for a in db.scalars(select(DataAsset).where(DataAsset.qualified_name.in_(names),
                                                                             DataAsset.lifecycle_state != "deleted"))}
    for asset in assets.values():  # 送れるのは資産のドメインの custodian と admin
        authz.require(db, principal, "usage.push", asset=asset)
    seen: dict[tuple[str, object], tuple] = {}
    for r in body.records:
        if r.qualified_name in assets:
            seen[(r.qualified_name, r.day)] = (assets[r.qualified_name], r.day, r.query_count, r.user_count)  # 同じ本文の中では後勝ち
    result = usage.store(db, principal.audit_actor, list(seen.values())) if seen else {"created": 0, "updated": 0}
    return {**result, "unregistered": [n for n in names if n not in assets]}
