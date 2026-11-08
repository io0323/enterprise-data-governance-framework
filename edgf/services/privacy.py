"""§12 Data Privacy: PII Inventory と contains_pii の判定.

Control Plane が持つのはカラム単位のメタデータだけで、PII の値そのものは扱わない (§18.2)。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from edgf.models import DataAsset


def pii_columns(schema: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [c for c in (schema or []) if c.get("pii")]


def effective_contains_pii(declared: bool, schema: Iterable[dict[str, Any]] | None) -> bool:
    """PII カラムがあれば contains_pii は常に True (カラムの pii フラグから自動推定)。"""
    return bool(declared or pii_columns(schema))


def _matches(col: dict[str, Any], category: str | None, purpose: str | None, legal_basis: str | None) -> bool:
    return ((category is None or col.get("pii_category") == category)
            and (purpose is None or purpose in (col.get("processing_purposes") or []))
            and (legal_basis is None or col.get("legal_basis") == legal_basis))


def _column_view(col: dict[str, Any]) -> dict[str, Any]:
    return {"name": col.get("name"), "type": col.get("type", "string"), "pii_category": col.get("pii_category"),
            "identifier_type": col.get("identifier_type"),
            "processing_purposes": col.get("processing_purposes") or [], "legal_basis": col.get("legal_basis")}


def inventory_entries(assets: Sequence[DataAsset], *, category: str | None = None, purpose: str | None = None,
                      legal_basis: str | None = None) -> list[dict[str, Any]]:
    """PII を含む資産とカラムの一覧。カラムの条件を指定した場合は、一致するカラムを持つ資産だけを返す。"""
    filtered = category is not None or purpose is not None or legal_basis is not None
    out = []
    for a in assets:
        cols = [_column_view(c) for c in pii_columns(a.schema_) if _matches(c, category, purpose, legal_basis)]
        if filtered and not cols:
            continue
        out.append({"asset_id": a.id, "qualified_name": a.qualified_name, "domain": a.domain, "owner": a.owner,
                    "steward": a.steward, "custodian": a.custodian, "sensitivity": a.sensitivity,
                    "retention_days": a.retention_days, "columns": cols})
    return out


def summarize(entries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    by_category: Counter[str] = Counter()
    by_purpose: Counter[str] = Counter()
    by_basis: Counter[str] = Counter()
    undocumented = 0
    columns = 0
    for e in entries:
        for c in e["columns"]:
            columns += 1
            by_category[c["pii_category"] or "unclassified"] += 1
            by_basis[c["legal_basis"] or "unspecified"] += 1
            by_purpose.update(c["processing_purposes"] or ["unspecified"])
            if not c["processing_purposes"] or not c["legal_basis"]:
                undocumented += 1
    return {"asset_count": len(entries), "column_count": columns, "undocumented_column_count": undocumented,
            "by_category": dict(sorted(by_category.items())), "by_purpose": dict(sorted(by_purpose.items())),
            "by_legal_basis": dict(sorted(by_basis.items()))}
