"""§9 Data Lineage API (OpenLineage互換取込)."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from sqlalchemy import select

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal
from edgf.models import DataAsset, fit_identifier
from edgf.errors import ProblemError
from edgf.schemas import ColumnEdgeIn, LineageEdgeIn, OpenLineageEvent
from edgf.services import ai_inherit, audit, column_lineage, lineage

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
    出力の columnLineage facet があれば、カラム単位の辺も取り込む (facet が無いイベントの結果は従来と同じ)。
    """
    job = fit_identifier(f"{event.job.namespace}/{event.job.name}" if event.job.namespace else event.job.name, 300)
    authz.require(db, principal, "lineage.write", entity_type="job", entity_id=job)
    facets = [(o.name, o.facets["columnLineage"]) for o in event.outputs if o.facets and "columnLineage" in o.facets]
    if facets:
        authz.require(db, principal, "lineage.column_write", entity_type="job", entity_id=job)
    if event.eventType != "COMPLETE":
        return {"accepted": True, "edges": 0, "note": f"{event.eventType} はエッジ確定対象外"}
    parsed = []
    for output, facet in facets:  # 先に全部検証する (一部だけ取り込まない)
        try:
            parsed.append(column_lineage.parse_facet(output, facet))
        except column_lineage.ColumnLineageError as e:
            raise ProblemError(422, str(e), type_="column-lineage-invalid") from e
    count = 0
    for i in event.inputs:
        for o in event.outputs:
            if i.name != o.name:
                lineage.upsert_edge(db, i.name, o.name, job, run_id=event.run.runId)
                count += 1
    if not facets:
        audit.record(db, principal.audit_actor, "lineage.openlineage", "job", job, {"run_id": event.run.runId, "edges": count})
        ai_inherit.refresh(db, principal.audit_actor, [o.name for o in event.outputs])
        return {"accepted": True, "edges": count}
    column_edges, warnings = 0, []
    for specs, parse_warnings in parsed:
        stored, ingest_warnings = column_lineage.ingest(db, specs, job, event.run.runId, principal.audit_actor)
        column_edges += stored
        warnings.extend([*parse_warnings, *ingest_warnings])
    audit.record(db, principal.audit_actor, "lineage.openlineage", "job", job, {"run_id": event.run.runId, "edges": count,
                                                                                "column_edges": column_edges, "warnings": len(warnings)})
    ai_inherit.refresh(db, principal.audit_actor, [o.name for o in event.outputs])
    return {"accepted": True, "edges": count, "column_edges": column_edges, "warnings": warnings}


@router.post("/column-edges", status_code=201)
def add_column_edge(body: ColumnEdgeIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """カラム単位の辺の手動の登録。対応するデータセット単位の辺も作る。"""
    entity = f"{body.source}.{body.source_column}->{body.target}.{body.target_column or '*'}"
    authz.require(db, principal, "lineage.column_write", entity_type="lineage", entity_id=entity)
    if body.source == body.target:
        raise HTTPException(422, "同じデータセットの中の辺は登録できません (データセット単位の辺が自己ループになるため)")
    warnings: list[dict] = []
    edge_type, subtype = column_lineage._normalize(body.type, body.subtype, warnings, "subtype")
    if body.target_column is None:
        edge_type = column_lineage.INDIRECT
    spec = column_lineage.EdgeSpec(body.source, body.source_column, body.target, body.target_column, edge_type, subtype, body.masking)
    edge = column_lineage.upsert(db, spec, body.job, via="manual", recorded_by=principal.audit_actor)
    warnings += column_lineage.unregistered_columns(db, [spec])
    audit.record(db, principal.audit_actor, "lineage.column_edge", "lineage", entity, {"type": edge.type, "subtype": edge.subtype, "masking": edge.masking})
    ai_inherit.refresh(db, principal.audit_actor, [body.target])
    return {"id": edge.id, "source": edge.source, "source_column": edge.source_column, "target": edge.target, "target_column": edge.target_column,
            "job": edge.job, "type": edge.type, "subtype": edge.subtype, "masking": edge.masking, "warnings": warnings}


CHUNK = 500


class NameRedactor:
    """閲覧できない資産 (ABAC) の名前を、応答ごとの不透明なラベルに置き換える。

    同じ資産は同じラベルになるので、グラフの繋がりは保たれる。ラベルは応答の外では意味を持たない。
    未登録の名前は資産が無く ABAC の対象にならないので、そのまま返す。
    """

    def __init__(self, db: Session, principal: Principal) -> None:
        self.db, self.principal = db, principal
        self._hidden: dict[str, bool] = {}
        self._labels: dict[str, str] = {}

    def preload(self, names: Iterable[str]) -> None:
        """まとめて判定する (名前ごとに問い合わせると N+1 になる)。readable_clause は can_read と同じ判定。"""
        pending = sorted({n for n in names if n not in self._hidden})
        for start in range(0, len(pending), CHUNK):
            chunk = pending[start:start + CHUNK]
            registered = set(self.db.scalars(select(DataAsset.qualified_name).where(DataAsset.qualified_name.in_(chunk))))
            readable = set(self.db.scalars(select(DataAsset.qualified_name).where(DataAsset.qualified_name.in_(chunk), authz.readable_clause(self.principal))))
            self._hidden.update({name: name in registered and name not in readable for name in chunk})

    def hidden(self, name: str) -> bool:
        if name not in self._hidden:
            self.preload([name])
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
    traversed = lineage.traverse(db, qualified_name, direction, depth)
    names.preload([qualified_name, *(e["from"] for e in traversed), *(e["to"] for e in traversed)])
    edges = []
    for edge in traversed:
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
    names.preload(n["qualified_name"] for n in result["nodes"])
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
    names.preload([qualified_name, *(i["qualified_name"] for i in result["impacted"])])
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


# ---------------------------------------------------------------- カラム単位 (Phase 16b)
def _column_view(names: NameRedactor, asset: str, column: str | None) -> dict[str, Any]:
    """閲覧できない資産は名前をラベルにし、カラム名も伏せる。"""
    if names.hidden(asset):
        return {"qualified_name": names.label(asset), "column": None, "redacted": True}
    return {"qualified_name": asset, "column": column}


def _column_traversal(db: Session, principal: Principal, qualified_name: str, column: str, direction: str, depth: int) -> dict:
    edges = column_lineage.traverse(db, qualified_name, column, direction, depth)
    names = NameRedactor(db, principal)
    names.preload([qualified_name, *(e["from"] for e in edges), *(e["to"] for e in edges)])
    out = []
    for e in edges:
        secret = names.hidden(e["from"]) or names.hidden(e["to"])
        source, target = _column_view(names, e["from"], e["from_column"]), _column_view(names, e["to"], e["to_column"])
        out.append({"from": source["qualified_name"], "from_column": source["column"], "to": target["qualified_name"], "to_column": target["column"],
                    "from_redacted": "redacted" in source, "to_redacted": "redacted" in target,
                    "job": None if secret else e["job"], "type": e["type"], "subtype": e["subtype"], "masking": e["masking"],
                    "depth": e["depth"], "path": e["path"]})
    root = _column_view(names, qualified_name, column)
    return {"asset": root["qualified_name"], "column": root["column"], "depth": depth, "edges": out}


@router.get("/{qualified_name}/columns")
def column_summary(qualified_name: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """資産の各カラムの 1 段の上流と下流。閲覧できない資産はラベルにし、カラム名も伏せる。"""
    found = column_lineage.neighbors(db, qualified_name)
    names = NameRedactor(db, principal)
    names.preload([qualified_name, *(n["qualified_name"] for v in found.values() for side in v.values() for n in side)])
    if names.hidden(qualified_name):
        return {"asset": names.label(qualified_name), "redacted": True, "columns": []}
    columns = []
    for column, sides in sorted(found.items()):
        view = {"column": None if column == "*" else column}
        for side, items in sides.items():
            view[side] = [{**_column_view(names, i["qualified_name"], i["column"]), "type": i["type"], "subtype": i["subtype"]} for i in items]
        columns.append(view)
    return {"asset": qualified_name, "columns": columns}


@router.get("/{qualified_name}/columns/{column}/upstream")
def column_upstream(qualified_name: str, column: str, depth: int = Query(10, ge=1, le=50), db: Session = Depends(get_session),
                    principal: Principal = Depends(get_principal)):
    """カラムの上流 (Traceability)。データセット全体に効く入力 (to_column が null) も含む。"""
    return _column_traversal(db, principal, qualified_name, column, "upstream", depth)


@router.get("/{qualified_name}/columns/{column}/downstream")
def column_downstream(qualified_name: str, column: str, depth: int = Query(10, ge=1, le=50), db: Session = Depends(get_session),
                      principal: Principal = Depends(get_principal)):
    return _column_traversal(db, principal, qualified_name, column, "downstream", depth)


def redacted_column_impact(db: Session, principal: Principal, qualified_name: str, column: str, depth: int = 10) -> dict:
    """閲覧できない下流の資産は、深さと経路の種類だけを返す。通知先の Owner とリスクは全資産で算出する (/impact と同じ)。"""
    result = column_lineage.impact(db, qualified_name, column, depth)
    names = NameRedactor(db, principal)
    names.preload([qualified_name, *(i["qualified_name"] for i in result["impacted"])])
    impacted = []
    for item in result["impacted"]:
        if names.hidden(item["qualified_name"]):
            item = {"qualified_name": names.label(item["qualified_name"]), "column": None, "depth": item["depth"], "path": item["path"],
                    "registered": True, "redacted": True}
        impacted.append(item)
    root = _column_view(names, qualified_name, column)
    return {**result, "changed_asset": root["qualified_name"], "changed_column": root["column"], "impacted": impacted}


@router.get("/{qualified_name}/columns/{column}/impact")
def column_impact(qualified_name: str, column: str, depth: int = Query(10, ge=1, le=50), db: Session = Depends(get_session),
                  principal: Principal = Depends(get_principal)):
    """カラムの変更の下流への影響。DIRECT と INDIRECT の両方を辿り、path で区別する。"""
    return redacted_column_impact(db, principal, qualified_name, column, depth)
