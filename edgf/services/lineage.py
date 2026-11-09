"""§9 Data Lineage / Impact Analysis / Traceability."""
from __future__ import annotations

from typing import Any

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


TRAVERSE_CHUNK = 500  # 1 回の IN に渡す名前の数 (SQLite の変数の上限に配慮する)


def _edges_from(db: Session, column: Any, names: list[str]) -> dict[str, list[LineageEdge]]:
    grouped: dict[str, list[LineageEdge]] = {name: [] for name in names}
    for start in range(0, len(names), TRAVERSE_CHUNK):
        for edge in db.scalars(select(LineageEdge).where(column.in_(names[start:start + TRAVERSE_CHUNK])).order_by(LineageEdge.id)):
            grouped[getattr(edge, column.key)].append(edge)
    return grouped


def traverse(db: Session, start: str, direction: str, max_depth: int = 10) -> list[dict]:
    """BFS (循環耐性あり)。direction: 'downstream' | 'upstream'。

    1 段ごとにまとめて読むので、クエリの数はノードの数ではなく深さに比例する。
    """
    cur_col = LineageEdge.source if direction == "downstream" else LineageEdge.target
    seen = {start}
    out: list[dict] = []
    frontier = [start]
    depth = 0
    while frontier and depth < max_depth:
        grouped = _edges_from(db, cur_col, frontier)
        following: list[str] = []
        for node in frontier:
            for edge in grouped[node]:
                nxt = edge.target if direction == "downstream" else edge.source
                out.append({"from": edge.source, "to": edge.target, "job": edge.job,
                            "transformation": edge.transformation, "depth": depth + 1})
                if nxt not in seen:
                    seen.add(nxt)
                    following.append(nxt)
        frontier = following
        depth += 1
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


def graph(db: Session, qualified_name: str, max_depth: int = 3) -> dict:
    """UI の描画用: 上流と下流のノードとエッジ。level は起点からの距離 (上流は負、下流は正)。"""
    levels: dict[str, int] = {qualified_name: 0}
    edges: dict[tuple[str, str, str], dict] = {}
    for direction, sign, key in (("upstream", -1, "from"), ("downstream", 1, "to")):
        for edge in traverse(db, qualified_name, direction, max_depth):
            edges[(edge["from"], edge["to"], edge["job"])] = {"from": edge["from"], "to": edge["to"], "job": edge["job"]}
            levels.setdefault(edge[key], sign * edge["depth"])
    assets = {a.qualified_name: a for a in db.scalars(select(DataAsset).where(DataAsset.qualified_name.in_(levels)))}
    nodes = []
    for name, level in sorted(levels.items(), key=lambda kv: (kv[1], kv[0])):
        asset = assets.get(name)
        nodes.append({"qualified_name": name, "level": level, "registered": asset is not None, "asset_id": asset.id if asset else None,
                      "is_cde": bool(asset and asset.is_cde), "certified": bool(asset and asset.certification == "certified"),
                      "data_class": asset.data_class if asset else None})
    return {"asset": qualified_name, "depth": max_depth, "nodes": nodes, "edges": list(edges.values())}
