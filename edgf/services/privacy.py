"""§12 Data Privacy: PII Inventory と contains_pii の判定.

Control Plane が持つのはカラム単位のメタデータだけで、PII の値そのものは扱わない (§18.2)。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from edgf.config import settings
from edgf.enums import PrivacyTechnique
from edgf.models import DataAsset

ANONYMIZED = PrivacyTechnique.ANONYMIZED.value
PSEUDONYMIZED = PrivacyTechnique.PSEUDONYMIZED.value


def pii_columns(schema: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [c for c in (schema or []) if c.get("pii")]


def k_satisfied(params: dict[str, Any] | None) -> bool:
    """PRV-003 の基準: k >= EDGF_MIN_K_ANONYMITY。k は申告値であり、データ自体は検証しない (Data Plane の責務)。"""
    k = (params or {}).get("k")
    return isinstance(k, int) and not isinstance(k, bool) and k >= settings.min_k_anonymity


def is_anonymized(technique: str | None, params: dict[str, Any] | None) -> bool:
    return technique == ANONYMIZED and k_satisfied(params)


def effective_pii_columns(schema: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """基準を満たして匿名化されたカラムを除く PII カラム。"""
    return [c for c in pii_columns(schema) if not is_anonymized(c.get("privacy_technique"), c.get("privacy_params"))]


def effective_contains_pii(declared: bool, schema: Iterable[dict[str, Any]] | None,
                           technique: str | None = None, params: dict[str, Any] | None = None) -> bool:
    """contains_pii を判定する (§12)。

    - 仮名化されたデータは個人データのまま (常に True)
    - 自動で False にできるのは PRV-003 を満たす anonymized だけ
    - それ以外は、PII カラムがあれば True
    """
    columns = pii_columns(schema)
    if technique == PSEUDONYMIZED or any(c.get("privacy_technique") == PSEUDONYMIZED for c in schema or []):
        return True
    if is_anonymized(technique, params):
        return False
    if effective_pii_columns(columns):
        return True
    return False if columns else bool(declared)


def _declared(asset: DataAsset) -> list[tuple[str, str | None, dict[str, Any]]]:
    """資産自身とカラムの保護手法の宣言を (表示名, 手法, パラメータ) で列挙する。"""
    items = [("(asset)", asset.privacy_technique, asset.privacy_params or {})]
    items += [(c.get("name", "?"), c.get("privacy_technique"), c.get("privacy_params") or {}) for c in asset.schema_ or []]
    return items


def check_anonymization_k(asset: DataAsset) -> tuple[bool | None, str]:
    """PRV-003。anonymized の宣言が無ければ対象外 (None)。"""
    targets = [(n, p) for n, t, p in _declared(asset) if t == ANONYMIZED]
    if not targets:
        return None, "対象外"
    bad = [n for n, p in targets if not k_satisfied(p)]
    if bad:
        return False, f"anonymized には k >= {settings.min_k_anonymity} が必要です: {bad}"
    return True, "ok"


def check_pseudonymization(asset: DataAsset) -> tuple[bool | None, str]:
    """PRV-004。pseudonymized の宣言が無ければ対象外 (None)。"""
    targets = [(n, p) for n, t, p in _declared(asset) if t == PSEUDONYMIZED]
    if not targets:
        return None, "対象外"
    no_key = [n for n, p in targets if not p.get("key_vault_ref")]
    if no_key:
        return False, f"pseudonymized には鍵の分離保管先 (key_vault_ref) が必要です: {no_key}"
    if not asset.contains_pii:
        return False, "仮名化されたデータは個人データのままです (contains_pii=true が必要)"
    return True, "ok"


def check_pii_documented(asset: DataAsset) -> tuple[bool | None, str]:
    """PRV-005。PII カラムが無ければ対象外 (None)。"""
    columns = effective_pii_columns(asset.schema_)
    if not columns:
        return None, "対象外"
    missing = [c.get("name") for c in columns if not c.get("processing_purposes") or not c.get("legal_basis")]
    if missing:
        return False, f"processing_purposes / legal_basis が未設定の PII カラム: {missing}"
    return True, "ok"


def _matches(col: dict[str, Any], category: str | None, purpose: str | None, legal_basis: str | None) -> bool:
    return ((category is None or col.get("pii_category") == category)
            and (purpose is None or purpose in (col.get("processing_purposes") or []))
            and (legal_basis is None or col.get("legal_basis") == legal_basis))


def _column_view(col: dict[str, Any]) -> dict[str, Any]:
    return {"name": col.get("name"), "type": col.get("type", "string"), "pii_category": col.get("pii_category"),
            "identifier_type": col.get("identifier_type"),
            "processing_purposes": col.get("processing_purposes") or [], "legal_basis": col.get("legal_basis"),
            "privacy_technique": col.get("privacy_technique")}


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
                    "retention_days": a.retention_days, "privacy_technique": a.privacy_technique, "columns": cols})
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
