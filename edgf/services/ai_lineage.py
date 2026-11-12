"""§14 AI Lineage: 構成元の資産 (版) → Dataset Version → Model Version → 利用先.

版の情報があるエッジは版単位で精密に判定する。版の情報が無いエッジ (手動登録や OpenLineage) で繋がる場合は、
どの版が影響を受けるか分からないので全版を対象にする (消去と汚染の影響範囲から漏らさないため)。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.enums import AiKind, DataClass
from edgf.models import DataAsset, DatasetVersion, DatasetVersionSource, ModelUsage, ModelVersion, model_version_dataset
from edgf.services import ai, lineage, lineage_trust

MAX_DEPTH = 50
VERSIONED_JOBS = (ai.DATASET_JOB, ai.MODEL_JOB)


@dataclass
class Impact:
    dataset_versions: list[DatasetVersion] = field(default_factory=list)
    model_versions: list[ModelVersion] = field(default_factory=list)
    unversioned_models: list[str] = field(default_factory=list)  # 版が未登録で影響を特定できないモデル
    downstream: set[str] = field(default_factory=set)
    excluded: list[dict[str, Any]] = field(default_factory=list)  # カラム単位のリネージで外した資産 (信頼できる生成元のみ)
    excluded_ids: list[str] = field(default_factory=list)  # そのうち Catalog に登録済みの資産の ID


def _models_using(db: Session, dataset_ids: Iterable[str]) -> list[ModelVersion]:
    ids = list(dataset_ids)
    if not ids:
        return []
    stmt = select(ModelVersion).join(model_version_dataset, ModelVersion.id == model_version_dataset.c.model_version_id) \
        .where(model_version_dataset.c.dataset_version_id.in_(ids)).distinct()
    return list(db.scalars(stmt))


def affected_by_assets(db: Session, names: Iterable[str], *,
                       root_columns: Mapping[str, Iterable[str] | None] | None = None) -> Impact:
    """起点の資産の下流にある Dataset Version と Model Version を特定する。

    root_columns (起点の PII のカラム) を渡すと、信頼できる生成元の辺だけを経由して PII が流れない資産を外す (消去)。
    """
    roots = set(names)
    reached = set(roots)
    taint = lineage_trust.forward(db, root_columns, MAX_DEPTH) if root_columns is not None else None
    unversioned_edge: set[str] = set()  # 版の情報と対応しないエッジで到達した資産
    for root in roots:
        for edge in lineage.traverse(db, root, "downstream", MAX_DEPTH):
            if taint is not None and edge["to"] not in roots and not taint.tainted(edge["to"]):
                continue
            reached.add(edge["to"])
            if edge["job"] not in VERSIONED_JOBS:
                unversioned_edge.add(edge["to"])
    assets = db.scalars(select(DataAsset).where(DataAsset.qualified_name.in_(reached), DataAsset.data_class == DataClass.AI.value)).all()

    datasets: dict[str, DatasetVersion] = {}
    for asset in assets:
        if asset.ai_kind != AiKind.TRAINING_DATASET.value:
            continue
        whole = asset.qualified_name in roots or asset.qualified_name in unversioned_edge
        for version in db.scalars(select(DatasetVersion).where(DatasetVersion.asset_id == asset.id)):
            if whole or any(s.qualified_name in reached for s in ai.sources_of(db, version)):
                datasets[version.id] = version

    models = {m.id: m for m in _models_using(db, datasets)}
    unversioned_models = []
    for asset in assets:
        if asset.ai_kind != AiKind.MODEL.value:
            continue
        versions = db.scalars(select(ModelVersion).where(ModelVersion.asset_id == asset.id)).all()
        if not versions:
            unversioned_models.append(asset.qualified_name)
        if asset.qualified_name in roots or asset.qualified_name in unversioned_edge:
            models.update({m.id: m for m in versions})
    excluded = taint.excluded if taint is not None else []
    names = [e["qualified_name"] for e in excluded]
    excluded_ids = sorted(db.scalars(select(DataAsset.id).where(DataAsset.qualified_name.in_(names)))) if names else []
    return Impact(dataset_versions=sorted(datasets.values(), key=lambda v: (v.asset_id, v.created_at, v.id)),
                  model_versions=sorted(models.values(), key=lambda v: (v.asset_id, v.created_at, v.id)),
                  unversioned_models=sorted(unversioned_models), downstream=reached - roots,
                  excluded=excluded, excluded_ids=excluded_ids)


def affected_by_dataset_version(db: Session, version: DatasetVersion) -> Impact:
    """起点が特定の版の場合。その版を構成元にする版 (入れ子) とモデル版だけを対象にする。"""
    found = {version.id: version}
    queue = [version]
    while queue:
        current = queue.pop()
        derived = db.scalars(select(DatasetVersion).join(
            DatasetVersionSource, DatasetVersionSource.dataset_version_id == DatasetVersion.id).where(
            DatasetVersionSource.asset_id == current.asset_id, DatasetVersionSource.kind == "dataset_version",
            DatasetVersionSource.version == current.version))
        for row in derived:
            if row.id not in found:
                found[row.id] = row
                queue.append(row)
    models = _models_using(db, found)
    return Impact(dataset_versions=sorted(found.values(), key=lambda v: (v.asset_id, v.created_at, v.id)),
                  model_versions=sorted(models, key=lambda v: (v.asset_id, v.created_at, v.id)))


def model_views(db: Session, impact: Impact) -> list[dict[str, Any]]:
    affected = {d.id for d in impact.dataset_versions}
    out = []
    for version in impact.model_versions:
        asset = db.get(DataAsset, version.asset_id)
        out.append({"model_version_id": version.id, "asset_id": asset.id, "qualified_name": asset.qualified_name,
                    "version": version.version, "status": version.status, "owner": asset.owner,
                    "via_dataset_version_ids": sorted(d.id for d in version.datasets if d.id in affected)})
    return out


def _dataset_view(db: Session, version: DatasetVersion) -> dict[str, Any]:
    asset = db.get(DataAsset, version.asset_id)
    return {"dataset_version_id": version.id, "asset_id": asset.id, "qualified_name": asset.qualified_name,
            "version": version.version, "status": version.status, "manifest_hash": version.manifest_hash,
            "record_count": version.record_count}


def trace_model(db: Session, version: ModelVersion) -> dict[str, Any]:
    """モデル版から起点の資産まで遡る。"""
    model = db.get(DataAsset, version.asset_id)
    datasets: dict[str, dict[str, Any]] = {}
    sources: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    pending = list(version.datasets)
    while pending:
        dataset = pending.pop()
        if dataset.id in datasets:
            continue
        datasets[dataset.id] = _dataset_view(db, dataset)
        for src in ai.sources_of(db, dataset):
            sources[(dataset.id, src.asset_id, src.kind, src.version)] = {
                "dataset_version_id": dataset.id, "asset_id": src.asset_id, "qualified_name": src.qualified_name,
                "kind": src.kind, "version": src.version}
            if src.kind == "dataset_version":
                parent = db.scalar(select(DatasetVersion).where(DatasetVersion.asset_id == src.asset_id,
                                                                DatasetVersion.version == src.version))
                if parent is not None:
                    pending.append(parent)
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for name in {s["qualified_name"] for s in sources.values()}:
        for edge in lineage.traverse(db, name, "upstream", MAX_DEPTH):
            edges[(edge["from"], edge["to"], edge["job"])] = edge
    targets = {e["to"] for e in edges.values()}
    nodes = {e["from"] for e in edges.values()} | {s["qualified_name"] for s in sources.values()}
    usages = db.scalars(select(ModelUsage).where(ModelUsage.model_version_id == version.id).order_by(ModelUsage.created_at)).all()
    return {
        "model_version": {"model_version_id": version.id, "asset_id": model.id, "qualified_name": model.qualified_name,
                          "version": version.version, "status": version.status},
        "datasets": sorted(datasets.values(), key=lambda d: (d["qualified_name"], d["version"])),
        "sources": sorted(sources.values(), key=lambda s: (s["qualified_name"], s["version"], s["dataset_version_id"])),
        "upstream_edges": [{"from": e["from"], "to": e["to"], "job": e["job"]} for e in edges.values()],
        "origins": sorted(nodes - targets),
        "usages": [{"kind": u.kind, "target": u.target, "target_asset_id": u.target_asset_id} for u in usages],
    }
