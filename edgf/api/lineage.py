"""§9 Data Lineage API (OpenLineage互換取込)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from sqlalchemy import select

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal
from edgf.models import DataAsset, fit_identifier
from edgf.schemas import LineageEdgeIn, OpenLineageEvent
from edgf.services import ai_inherit, audit, lineage

router = APIRouter(prefix="/lineage", tags=["lineage"])


@router.post("/edges", status_code=201)
def add_edge(body: LineageEdgeIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "lineage.write", entity_type="lineage", entity_id=f"{body.source}->{body.target}")
    try:
        e = lineage.upsert_edge(db, body.source, body.target, body.job, body.transformation)
    except ValueError as ex:
        raise HTTPException(422, str(ex)) from ex
    audit.record(db, principal.audit_actor, "lineage.edge", "lineage", f"{e.source}->{e.target}", body.model_dump())
    ai_inherit.refresh(db, principal.audit_actor, [e.target])
    return {"id": e.id, "source": e.source, "target": e.target, "job": e.job}


@router.post("/openlineage", status_code=202)
def ingest_openlineage(event: OpenLineageEvent, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """OpenLineage RunEvent を受信し inputs×outputs のエッジを生成 (COMPLETE のみ確定)。

    データセット識別子は OpenLineage の dataset name を qualified_name として扱う。
    """
    job = fit_identifier(f"{event.job.namespace}/{event.job.name}" if event.job.namespace else event.job.name, 300)
    authz.require(db, principal, "lineage.write", entity_type="job", entity_id=job)
    if event.eventType != "COMPLETE":
        return {"accepted": True, "edges": 0, "note": f"{event.eventType} はエッジ確定対象外"}
    count = 0
    for i in event.inputs:
        for o in event.outputs:
            if i.name != o.name:
                lineage.upsert_edge(db, i.name, o.name, job, run_id=event.run.runId)
                count += 1
    audit.record(db, principal.audit_actor, "lineage.openlineage", "job", job, {"run_id": event.run.runId, "edges": count})
    ai_inherit.refresh(db, principal.audit_actor, [o.name for o in event.outputs])
    return {"accepted": True, "edges": count}


@router.get("/{qualified_name}/upstream")
def upstream(qualified_name: str, depth: int = Query(10, ge=1, le=50), db: Session = Depends(get_session)):
    """Traceability: 任意データから起点まで遡及。"""
    return {"asset": qualified_name, "edges": lineage.traverse(db, qualified_name, "upstream", depth)}


@router.get("/{qualified_name}/downstream")
def downstream(qualified_name: str, depth: int = Query(10, ge=1, le=50), db: Session = Depends(get_session)):
    return {"asset": qualified_name, "edges": lineage.traverse(db, qualified_name, "downstream", depth)}


@router.get("/{qualified_name}/impact")
def impact(qualified_name: str, depth: int = Query(10, ge=1, le=50), db: Session = Depends(get_session),
           principal: Principal = Depends(get_principal)):
    """閲覧できない下流資産 (ABAC) は名前と深さだけを返し、詳細を伏せる。

    通知先 Owner (owners_to_notify) とリスク判定は変更管理 (§16 Change) に必要なため全資産で算出する。
    """
    result = lineage.impact_analysis(db, qualified_name, depth)
    ids = [i["asset_id"] for i in result["impacted"] if i["asset_id"]]
    assets = {a.id: a for a in db.scalars(select(DataAsset).where(DataAsset.id.in_(ids)))} if ids else {}
    redacted = []
    for i in result["impacted"]:
        a = assets.get(i["asset_id"])
        if a is not None and not authz.can_read(db, principal, a):
            i = {"qualified_name": i["qualified_name"], "depth": i["depth"], "registered": True, "redacted": True}
        redacted.append(i)
    result["impacted"] = redacted
    return result
