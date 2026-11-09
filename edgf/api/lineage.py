"""§9 Data Lineage API (OpenLineage互換取込)."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
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


class NameRedactor:
    """閲覧できない資産 (ABAC) の名前を、応答ごとの不透明なラベルに置き換える。

    同じ資産は同じラベルになるので、グラフの繋がりは保たれる。ラベルは応答の外では意味を持たない。
    未登録の名前は資産が無く ABAC の対象にならないので、そのまま返す。
    """

    def __init__(self, db: Session, principal: Principal) -> None:
        self.db, self.principal = db, principal
        self._hidden: dict[str, bool] = {}
        self._labels: dict[str, str] = {}

    def hidden(self, name: str) -> bool:
        if name not in self._hidden:
            asset = self.db.scalar(select(DataAsset).where(DataAsset.qualified_name == name))
            self._hidden[name] = asset is not None and not authz.can_read(self.db, self.principal, asset)
        return self._hidden[name]

    def label(self, name: str) -> str:
        if not self.hidden(name):
            return name
        return self._labels.setdefault(name, f"redacted-{len(self._labels) + 1}")


def _traversal(db: Session, principal: Principal, response: Response, qualified_name: str, direction: str, depth: int) -> dict:
    """非推奨の API。項目は削らず、閲覧できない資産に接するエッジの値だけを伏せる。移行先は /graph。"""
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = f'</api/v1/lineage/{quote(qualified_name, safe="")}/graph>; rel="successor-version"'
    names = NameRedactor(db, principal)
    edges = []
    for edge in lineage.traverse(db, qualified_name, direction, depth):
        secret = names.hidden(edge["from"]) or names.hidden(edge["to"])
        edges.append({**edge, "from": names.label(edge["from"]), "to": names.label(edge["to"]),
                      "job": None if secret else edge["job"], "transformation": None if secret else edge["transformation"]})
    return {"asset": names.label(qualified_name), "edges": edges}


@router.get("/{qualified_name}/upstream")
def upstream(qualified_name: str, response: Response, depth: int = Query(10, ge=1, le=50), db: Session = Depends(get_session),
             principal: Principal = Depends(get_principal)):
    """Traceability: 任意データから起点まで遡及 (非推奨。/graph を使ってください)。"""
    return _traversal(db, principal, response, qualified_name, "upstream", depth)


@router.get("/{qualified_name}/downstream")
def downstream(qualified_name: str, response: Response, depth: int = Query(10, ge=1, le=50), db: Session = Depends(get_session),
               principal: Principal = Depends(get_principal)):
    """非推奨。/graph を使ってください。"""
    return _traversal(db, principal, response, qualified_name, "downstream", depth)


@router.get("/{qualified_name}/graph")
def graph(qualified_name: str, depth: int = Query(3, ge=1, le=50), db: Session = Depends(get_session),
          principal: Principal = Depends(get_principal)):
    """UI の描画用のグラフ。閲覧できない資産 (ABAC) は名前と深さだけを返し、そこに接するエッジの job も伏せる。"""
    result = lineage.graph(db, qualified_name, depth)
    names = NameRedactor(db, principal)
    nodes = []
    for node in result["nodes"]:
        if names.hidden(node["qualified_name"]):
            node = {"qualified_name": names.label(node["qualified_name"]), "level": node["level"], "registered": True, "redacted": True}
        nodes.append(node)
    result["nodes"] = nodes
    result["edges"] = [{"from": names.label(e["from"]), "to": names.label(e["to"]),
                        "job": None if names.hidden(e["from"]) or names.hidden(e["to"]) else e["job"]} for e in result["edges"]]
    result["asset"] = names.label(qualified_name)
    return result


def redacted_impact(db: Session, principal: Principal, qualified_name: str, depth: int = 10) -> dict:
    """閲覧できない下流資産 (ABAC) は深さだけを返し、名前も詳細も伏せる。

    通知先 Owner (owners_to_notify) とリスク判定は変更管理 (§16 Change) に必要なため全資産で算出する。
    """
    result = lineage.impact_analysis(db, qualified_name, depth)
    names = NameRedactor(db, principal)
    impacted = []
    for item in result["impacted"]:
        if names.hidden(item["qualified_name"]):
            item = {"qualified_name": names.label(item["qualified_name"]), "depth": item["depth"], "registered": True, "redacted": True}
        impacted.append(item)
    result["impacted"] = impacted
    result["changed_asset"] = names.label(qualified_name)
    return result


@router.get("/{qualified_name}/impact")
def impact(qualified_name: str, depth: int = Query(10, ge=1, le=50), db: Session = Depends(get_session),
           principal: Principal = Depends(get_principal)):
    return redacted_impact(db, principal, qualified_name, depth)
