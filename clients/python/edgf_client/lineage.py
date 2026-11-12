"""OpenLineage の columnLineage facet (1-2-0) を組み立てる.

    facet = column_lineage_facet({"total": [input_field("warehouse", "sales.orders", "amount", subtype="AGGREGATION")]})
    event["outputs"][0]["facets"] = {"columnLineage": facet}

transformations の description (自由記述) は Control Plane が保存しないので、ここでも受け取らない。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

SCHEMA_URL = "https://openlineage.io/spec/facets/1-2-0/ColumnLineageDatasetFacet.json#/$defs/ColumnLineageDatasetFacet"
PRODUCER = "https://github.com/io0323/enterprise-data-governance-framework/clients/python"


def input_field(namespace: str, name: str, field: str, *, type: str = "DIRECT", subtype: str | None = None, masking: bool = False) -> dict[str, Any]:  # noqa: A002 - OpenLineage の項目名
    """入力のカラム。type は DIRECT (値が流れる) か INDIRECT (結合キー、絞り込みなど)。"""
    transformation: dict[str, Any] = {"type": type, "masking": masking}
    if subtype:
        transformation["subtype"] = subtype
    return {"namespace": namespace, "name": name, "field": field, "transformations": [transformation]}


def column_lineage_facet(fields: Mapping[str, Iterable[Mapping[str, Any]]], dataset: Iterable[Mapping[str, Any]] = (), *,
                         producer: str = PRODUCER) -> dict[str, Any]:
    """出力のカラム → 入力のカラムの一覧。dataset はデータセット全体に効く入力 (INDIRECT として扱われる)。"""
    return {"_producer": producer, "_schemaURL": SCHEMA_URL,
            "fields": {column: {"inputFields": [dict(i) for i in inputs]} for column, inputs in fields.items()},
            **({"dataset": [dict(i) for i in dataset]} if dataset else {})}


def openlineage_event(job_namespace: str, job_name: str, run_id: str, inputs: Iterable[tuple[str, str]], outputs: Iterable[tuple[str, str]], *,
                      column_lineage: Mapping[str, Mapping[str, Any]] | None = None, event_type: str = "COMPLETE") -> dict[str, Any]:
    """RunEvent の最小の形。column_lineage は出力の名前 → facet。"""
    def dataset(namespace: str, name: str) -> dict[str, Any]:
        facet = (column_lineage or {}).get(name)
        return {"namespace": namespace, "name": name, **({"facets": {"columnLineage": facet}} if facet else {})}
    return {"eventType": event_type, "run": {"runId": run_id}, "job": {"namespace": job_namespace, "name": job_name},
            "inputs": [dataset(n, d) for n, d in inputs], "outputs": [dataset(n, d) for n, d in outputs]}
