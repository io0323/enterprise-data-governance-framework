"""§9 カラム単位のリネージ: OpenLineage の columnLineage facet の取り込みと、手動の登録.

受け付ける範囲 (Phase 16 の確定事項):
- fields.<出力カラム>.inputFields[] の namespace / name / field と、transformations[] の type / subtype / masking
- 未知の subtype は OTHER として保存し、警告を返す (拒否しない)
- transformationDescription (自由記述) は保存しない。SQL の断片にリテラル値 (PII など) が含まれうるため
- 廃止予定の field 単位の transformationType は、transformations が無いときに限って type に読み替える
- dataset 配列は、データセット単位の INDIRECT として保存する (出力カラムは NULL)
- Catalog に無いカラムは拒否せず、unregistered_column として警告を返す
- _schemaURL のメジャー版が 1 の facet だけを受け付ける

カラム単位の辺があれば、対応するデータセット単位の辺も必ず作る。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.models import ColumnLineageEdge, DataAsset, utcnow
from edgf.services import lineage

DIRECT, INDIRECT = "DIRECT", "INDIRECT"
TYPES = (DIRECT, INDIRECT)
# OpenLineage 1-2 の subtype。これ以外は OTHER として保存する
SUBTYPES = frozenset({"IDENTITY", "TRANSFORMATION", "AGGREGATION", "JOIN", "GROUP_BY", "FILTER", "SORT", "WINDOW", "CONDITIONAL"})
OTHER = "OTHER"
SCHEMA_URL = re.compile(r"/(\d+)-(\d+)-(\d+)/ColumnLineageDatasetFacet\.json")
MAX_EDGES = 20_000
NAME_MAX, FIELD_MAX = 300, 200


class ColumnLineageError(ValueError):
    """facet を受け付けられない (版が違う、形が不正)。422 にする。"""


@dataclass(frozen=True)
class EdgeSpec:
    source: str
    source_column: str
    target: str
    target_column: str | None
    type: str
    subtype: str | None
    masking: bool


def flows_values(edge_type: str, subtype: str | None) -> bool:
    """値が下流に流れる経路か (機密度・PII の継承と消去の対象に使う)。AGGREGATION も DIRECT として扱う。"""
    return edge_type == DIRECT or subtype == "AGGREGATION"


def _text(value: Any, limit: int, where: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ColumnLineageError(f"{where} は 1〜{limit} 文字の文字列にしてください")
    return value.strip()


def _normalize(raw_type: Any, raw_subtype: Any, warnings: list[dict[str, Any]], where: str) -> tuple[str, str | None]:
    edge_type = str(raw_type or DIRECT).upper()
    if edge_type not in TYPES:
        warnings.append({"code": "unknown_type", "where": where, "value": edge_type[:30]})
        edge_type = DIRECT  # 値が流れる側に倒す (継承と消去の対象を狭めない)
    subtype = str(raw_subtype).upper() if raw_subtype else None
    if subtype and subtype not in SUBTYPES:
        warnings.append({"code": "unknown_subtype", "where": where, "value": subtype[:30]})
        subtype = OTHER
    return edge_type, subtype


def _transformations(item: Mapping[str, Any], legacy_type: Any, warnings: list[dict[str, Any]], where: str) -> list[tuple[str, str | None, bool]]:
    found = item.get("transformations")
    if found:
        if not isinstance(found, list):
            raise ColumnLineageError(f"{where}.transformations は配列にしてください")
        out = []
        for i, t in enumerate(found):
            if not isinstance(t, Mapping):
                raise ColumnLineageError(f"{where}.transformations[{i}] はオブジェクトにしてください")
            edge_type, subtype = _normalize(t.get("type"), t.get("subtype"), warnings, f"{where}.transformations[{i}]")
            out.append((edge_type, subtype, bool(t.get("masking"))))  # description は読まない (保存しない)
        return out
    if legacy_type:  # 廃止予定の transformationType: transformations が無いときだけ読み替える
        value = str(legacy_type).upper()
        if value in TYPES:
            return [(value, None, False)]
        _, subtype = _normalize(DIRECT, value if value != "MASKED" else "TRANSFORMATION", warnings, where)
        return [(DIRECT, subtype, value == "MASKED")]
    return [(DIRECT, None, False)]  # 種類の記録が無い経路は、値が流れるものとして扱う


def _input(item: Any, where: str) -> tuple[str, str]:
    if not isinstance(item, Mapping):
        raise ColumnLineageError(f"{where} はオブジェクトにしてください")
    return _text(item.get("name"), NAME_MAX, f"{where}.name"), _text(item.get("field"), FIELD_MAX, f"{where}.field")


def parse_facet(output_name: str, facet: Any) -> tuple[list[EdgeSpec], list[dict[str, Any]]]:
    if not isinstance(facet, Mapping):
        raise ColumnLineageError("columnLineage はオブジェクトにしてください")
    match = SCHEMA_URL.search(str(facet.get("_schemaURL") or ""))
    if match is None or match.group(1) != "1":
        raise ColumnLineageError("columnLineage の _schemaURL はメジャー版 1 (…/1-x-y/ColumnLineageDatasetFacet.json) だけを受け付けます")
    warnings: list[dict[str, Any]] = []
    specs: dict[tuple, EdgeSpec] = {}

    def add(source: str, column: str, target_column: str | None, edge_type: str, subtype: str | None, masking: bool) -> None:
        key = (source, column, output_name, target_column, edge_type)
        previous = specs.get(key)
        specs[key] = EdgeSpec(source, column, output_name, target_column, edge_type, (previous.subtype if previous else None) or subtype,
                              masking or bool(previous and previous.masking))

    fields = facet.get("fields", {})
    if not isinstance(fields, Mapping):
        raise ColumnLineageError("columnLineage.fields はオブジェクトにしてください")
    for out_column, spec in fields.items():
        out_column = _text(out_column, FIELD_MAX, "columnLineage.fields のキー")
        where = f"fields.{out_column}"
        if not isinstance(spec, Mapping) or not isinstance(spec.get("inputFields", []), list):
            raise ColumnLineageError(f"{where}.inputFields は配列にしてください")
        for i, item in enumerate(spec.get("inputFields", [])):
            source, column = _input(item, f"{where}.inputFields[{i}]")
            for edge_type, subtype, masking in _transformations(item, spec.get("transformationType"), warnings, f"{where}.inputFields[{i}]"):
                add(source, column, out_column, edge_type, subtype, masking)
    dataset = facet.get("dataset", [])
    if not isinstance(dataset, list):
        raise ColumnLineageError("columnLineage.dataset は配列にしてください")
    for i, item in enumerate(dataset):
        source, column = _input(item, f"dataset[{i}]")
        for _, subtype, masking in _transformations(item, None, warnings, f"dataset[{i}]"):
            add(source, column, None, INDIRECT, subtype, masking)  # データセット全体に効く入力は INDIRECT
    if len(specs) > MAX_EDGES:
        raise ColumnLineageError(f"1 つの facet のカラムの辺は {MAX_EDGES} 件までです")
    return list(specs.values()), warnings


def upsert(db: Session, spec: EdgeSpec, job: str, run_id: str | None = None) -> ColumnLineageEdge:
    """カラムの辺を保存し、対応するデータセット単位の辺も作る。"""
    lineage.upsert_edge(db, spec.source, spec.target, job, run_id=run_id)
    target_column = ColumnLineageEdge.target_column.is_(None) if spec.target_column is None else ColumnLineageEdge.target_column == spec.target_column
    edge = db.scalar(select(ColumnLineageEdge).where(ColumnLineageEdge.source == spec.source, ColumnLineageEdge.source_column == spec.source_column,
                                                     ColumnLineageEdge.target == spec.target, target_column, ColumnLineageEdge.job == job,
                                                     ColumnLineageEdge.type == spec.type))
    if edge is None:
        edge = ColumnLineageEdge(source=spec.source, source_column=spec.source_column, target=spec.target, target_column=spec.target_column,
                                 job=job, type=spec.type, subtype=spec.subtype, masking=spec.masking, last_run_id=run_id)
        db.add(edge)
    else:
        edge.subtype, edge.masking, edge.updated_at = spec.subtype, spec.masking, utcnow()
        edge.last_run_id = run_id or edge.last_run_id
    db.flush()
    return edge


def unregistered_columns(db: Session, specs: Iterable[EdgeSpec]) -> list[dict[str, Any]]:
    """Catalog にある資産で、スキーマに無いカラム (Phase 15 の資産一覧で追いつく前提で拒否はしない)。"""
    wanted: dict[str, set[str]] = {}
    for s in specs:
        wanted.setdefault(s.source, set()).add(s.source_column)
        if s.target_column is not None:
            wanted.setdefault(s.target, set()).add(s.target_column)
    assets = {a.qualified_name: a for a in db.scalars(select(DataAsset).where(DataAsset.qualified_name.in_(list(wanted))))} if wanted else {}
    out = []
    for name, columns in sorted(wanted.items()):
        asset = assets.get(name)
        if asset is None:
            continue
        known = {c.get("name") for c in asset.schema_ or []}
        out.extend({"code": "unregistered_column", "asset": name, "column": c} for c in sorted(columns - known))
    return out


def ingest(db: Session, specs: list[EdgeSpec], job: str, run_id: str | None) -> tuple[int, list[dict[str, Any]]]:
    warnings = []
    stored = 0
    for spec in specs:
        if spec.source == spec.target:
            warnings.append({"code": "same_dataset", "asset": spec.source, "column": spec.source_column})
            continue  # データセット単位の辺は自己ループにできない
        upsert(db, spec, job, run_id)
        stored += 1
    return stored, warnings + unregistered_columns(db, specs)
