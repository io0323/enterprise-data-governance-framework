"""§9 Data Lineage / Impact Analysis / Traceability."""
from __future__ import annotations

from collections import deque

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.models import DataAsset, LineageEdge, utcnow


def upsert_edge(db: Session, source: str, target: str, job: str = "manual",
                transformation: str = "", run_id: str | None = None) -> LineageEdge:
    if source == target:
        raise ValueError("self-loop edge is not allowed")
    edge = db.scalar(select(LineageEdge).where(LineageEdge.source == source, LineageEdge.target == target,
                                               LineageEdge.job == job))
    if edge is None:
        edge = LineageEdge(source=source, target=target, job=job, transformation=transformation, last_run_id=run_id)
        db.add(edge)
    else:
        edge.last_run_id = run_id or edge.last_run_id
        edge.transformation = transformation or edge.transformation
        edge.updated_at = utcnow()
    db.flush()
    return edge


def traverse(db: Session, start: str, direction: str, max_depth: int = 10) -> list[dict]:
    """BFS (循環耐性あり)。direction: 'downstream' | 'upstream'"""
    nxt_col, cur_col = (LineageEdge.target, LineageEdge.source) if direction == "downstream" else (LineageEdge.source, LineageEdge.target)
    seen = {start}
    out: list[dict] = []
    queue = deque([(start, 0)])
    while queue:
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for edge in db.scalars(select(LineageEdge).where(cur_col == node)):
            nxt = edge.target if direction == "downstream" else edge.source
            out.append({"from": edge.source, "to": edge.target, "job": edge.job,
                        "transformation": edge.transformation, "depth": depth + 1})
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, depth + 1))
    return out


def has_upstream(db: Session, qualified_name: str) -> bool:
    return db.scalar(select(LineageEdge.id).where(LineageEdge.target == qualified_name).limit(1)) is not None


def impact_analysis(db: Session, qualified_name: str, max_depth: int = 10) -> dict:
    """変更・廃止時の下流影響 (§9, §16 Change)。通知先 Owner と CDE/Certified 影響を返す。"""
    edges = traverse(db, qualified_name, "downstream", max_depth)
    depth_by_node: dict[str, int] = {}
    for e in edges:
        depth_by_node[e["to"]] = min(depth_by_node.get(e["to"], 10**6), e["depth"])
    assets = {a.qualified_name: a for a in db.scalars(
        select(DataAsset).where(DataAsset.qualified_name.in_(list(depth_by_node))))} if depth_by_node else {}
    impacted = []
    for qn, depth in sorted(depth_by_node.items(), key=lambda kv: (kv[1], kv[0])):
        a = assets.get(qn)
        impacted.append({
            "qualified_name": qn, "depth": depth, "registered": a is not None,
            "asset_id": a.id if a else None, "owner": a.owner if a else None,
            "domain": a.domain if a else None, "is_cde": bool(a and a.is_cde),
            "certified": bool(a and a.certification == "certified"),
            "data_class": a.data_class if a else None,
        })
    owners = sorted({i["owner"] for i in impacted if i["owner"]})
    risk = "high" if any(i["is_cde"] or i["certified"] for i in impacted) else ("medium" if impacted else "none")
    return {
        "changed_asset": qualified_name, "impacted_count": len(impacted), "risk": risk,
        "owners_to_notify": owners,
        "unregistered_downstream": [i["qualified_name"] for i in impacted if not i["registered"]],
        "impacted": impacted,
    }
