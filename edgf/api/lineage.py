"""§9 Data Lineage API (OpenLineage互換取込)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from edgf.db import get_session
from edgf.auth import Principal
from edgf.deps import get_principal
from edgf.schemas import LineageEdgeIn, OpenLineageEvent
from edgf.services import audit, lineage

router = APIRouter(prefix="/lineage", tags=["lineage"])


@router.post("/edges", status_code=201)
def add_edge(body: LineageEdgeIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    try:
        e = lineage.upsert_edge(db, body.source, body.target, body.job, body.transformation)
    except ValueError as ex:
        raise HTTPException(422, str(ex)) from ex
    audit.record(db, principal.audit_actor, "lineage.edge", "lineage", f"{e.source}->{e.target}", body.model_dump())
    return {"id": e.id, "source": e.source, "target": e.target, "job": e.job}


@router.post("/openlineage", status_code=202)
def ingest_openlineage(event: OpenLineageEvent, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """OpenLineage RunEvent を受信し inputs×outputs のエッジを生成 (COMPLETE のみ確定)。

    データセット識別子は OpenLineage の dataset name を qualified_name として扱う。
    """
    if event.eventType != "COMPLETE":
        return {"accepted": True, "edges": 0, "note": f"{event.eventType} はエッジ確定対象外"}
    job = f"{event.job.namespace}/{event.job.name}" if event.job.namespace else event.job.name
    count = 0
    for i in event.inputs:
        for o in event.outputs:
            if i.name != o.name:
                lineage.upsert_edge(db, i.name, o.name, job, run_id=event.run.runId)
                count += 1
    audit.record(db, principal.audit_actor, "lineage.openlineage", "job", job, {"run_id": event.run.runId, "edges": count})
    return {"accepted": True, "edges": count}


@router.get("/{qualified_name}/upstream")
def upstream(qualified_name: str, depth: int = Query(10, ge=1, le=50), db: Session = Depends(get_session)):
    """Traceability: 任意データから起点まで遡及。"""
    return {"asset": qualified_name, "edges": lineage.traverse(db, qualified_name, "upstream", depth)}


@router.get("/{qualified_name}/downstream")
def downstream(qualified_name: str, depth: int = Query(10, ge=1, le=50), db: Session = Depends(get_session)):
    return {"asset": qualified_name, "edges": lineage.traverse(db, qualified_name, "downstream", depth)}


@router.get("/{qualified_name}/impact")
def impact(qualified_name: str, depth: int = Query(10, ge=1, le=50), db: Session = Depends(get_session)):
    return lineage.impact_analysis(db, qualified_name, depth)
