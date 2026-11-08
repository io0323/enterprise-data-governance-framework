"""§15 Standardization: Data Contract の互換性の判定 (利用者から見た判定).

判定は宣言的な表で定義する。COMPAT_RULES が「どの変更がどの版の上げ方を必要とするか」、
TYPE_WIDENING が「どの型の変更が拡大か」を決める。ここを読めば判定の全体が分かる。
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import IntEnum
from typing import Any

from edgf.auth import SENSITIVITY_LEVELS


class Bump(IntEnum):
    PATCH = 1
    MINOR = 2
    MAJOR = 3


# 変更の種類 → 必要な版の上げ方と説明。MAJOR が破壊的な変更
COMPAT_RULES: Mapping[str, tuple[Bump, str]] = {
    "column_removed": (Bump.MAJOR, "カラムの削除"),
    "column_renamed": (Bump.MAJOR, "カラムの名前の変更"),
    "column_added_required": (Bump.MAJOR, "非 null のカラムの追加"),
    "type_incompatible": (Bump.MAJOR, "互換性の無い型の変更"),
    "nullable_relaxed": (Bump.MAJOR, "非 null の保証の撤回 (非 null → nullable)"),
    "sensitivity_raised": (Bump.MAJOR, "機密度の引き上げ"),
    "pii_added": (Bump.MAJOR, "PII の追加"),
    "quality_sla_lowered": (Bump.MAJOR, "品質 SLA の引き下げ"),
    "freshness_sla_lowered": (Bump.MAJOR, "鮮度 SLA の引き下げ"),
    "usage_narrowed": (Bump.MAJOR, "利用条件の縮小"),
    "conforms_to_changed": (Bump.MAJOR, "準拠する CDM の変更または削除"),
    "column_added_nullable": (Bump.MINOR, "nullable なカラムの追加"),
    "type_widened": (Bump.MINOR, "型の拡大"),
    "nullable_tightened": (Bump.MINOR, "非 null の保証の追加 (nullable → 非 null)"),
    "sensitivity_lowered": (Bump.MINOR, "機密度の引き下げ"),
    "pii_removed": (Bump.MINOR, "PII の削除"),
    "quality_sla_raised": (Bump.MINOR, "品質 SLA の引き上げ"),
    "freshness_sla_raised": (Bump.MINOR, "鮮度 SLA の引き上げ"),
    "usage_widened": (Bump.MINOR, "利用条件の拡大"),
    "conforms_to_added": (Bump.MINOR, "準拠する CDM の宣言の追加"),
    "column_reference_changed": (Bump.MINOR, "ID や Reference Data の参照の変更"),
    "description_changed": (Bump.PATCH, "説明の変更"),
}

# 表記の揺れをまとめる (型の互換性は正規化した名前で判定する)
TYPE_ALIASES: Mapping[str, str] = {
    "integer": "int", "int4": "int", "int2": "smallint", "short": "smallint", "int8": "bigint", "long": "bigint",
    "real": "float", "float4": "float", "float8": "double", "double precision": "double", "numeric": "decimal", "number": "decimal",
    "text": "string", "str": "string", "character varying": "varchar", "bool": "boolean", "datetime": "timestamp",
}
# 型の拡大 (利用者の既存の読み取りが壊れない方向)。推移的に適用する。bigint → double は桁落ちするので含めない
TYPE_WIDENING: Mapping[str, tuple[str, ...]] = {
    "tinyint": ("smallint",), "smallint": ("int",), "int": ("bigint", "double"), "bigint": ("decimal",),
    "float": ("double",), "date": ("timestamp",), "char": ("varchar",), "varchar": ("string",),
}
_TYPE = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9_ ]*?)\s*(?:\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\))?\s*$")


def parse_type(type_name: str) -> tuple[str, tuple[int, ...]]:
    """(正規化した型名, 長さや精度)。形式が分からない型は全体を名前として扱う (同じ表記だけが互換になる)。"""
    match = _TYPE.match(type_name)
    if not match:
        return type_name.strip().lower(), ()
    base = match.group(1).lower()
    return TYPE_ALIASES.get(base, base), tuple(int(g) for g in match.groups()[1:] if g is not None)


def _widens_to(old: str, new: str) -> bool:
    pending, seen = [old], set()
    while pending:
        current = pending.pop()
        if current == new:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(TYPE_WIDENING.get(current, ()))
    return False


def type_change(old: str, new: str) -> str | None:
    """None (同じ) / "type_widened" / "type_incompatible"。"""
    old_base, old_args = parse_type(old)
    new_base, new_args = parse_type(new)
    if old_base != new_base:
        return "type_widened" if _widens_to(old_base, new_base) else "type_incompatible"
    if old_args == new_args:
        return None
    if not new_args:
        return "type_widened"  # 長さの制限が無くなった
    if not old_args or len(old_args) != len(new_args):
        return "type_incompatible"
    return "type_widened" if all(n >= o for o, n in zip(old_args, new_args)) else "type_incompatible"


def make_change(code: str, target: str | None = None, detail: str = "") -> dict[str, Any]:
    bump, label = COMPAT_RULES[code]
    return {"code": code, "bump": bump.name.lower(), "breaking": bump == Bump.MAJOR, "label": label,
            "target": target, "detail": detail}


def _pair_renames(old: Mapping[str, dict], new: Mapping[str, dict]) -> dict[str, str]:
    """new → old の対応。renamed_from の明示を優先し、削除と追加が 1 件ずつで型が同じなら名前の変更とみなす。"""
    pairs = {n: c["renamed_from"] for n, c in new.items() if n not in old and c.get("renamed_from") in old}
    removed = [n for n in old if n not in new and n not in pairs.values()]
    added = [n for n in new if n not in old and n not in pairs]
    if len(removed) == 1 and len(added) == 1 and parse_type(old[removed[0]]["type"]) == parse_type(new[added[0]]["type"]):
        pairs[added[0]] = removed[0]
    return pairs


def _column_changes(name: str, old: Mapping[str, Any], new: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    kind = type_change(old["type"], new["type"])
    if kind:
        out.append(make_change(kind, name, f"{old['type']} → {new['type']}"))
    # None は「宣言なし」(Catalog 側) なので比較しない
    old_null, new_null = old.get("nullable"), new.get("nullable")
    if old_null is False and new_null is True:
        out.append(make_change("nullable_relaxed", name))
    elif old_null is True and new_null is False:
        out.append(make_change("nullable_tightened", name))
    if bool(new.get("pii")) and not old.get("pii"):
        out.append(make_change("pii_added", name))
    elif bool(old.get("pii")) and not new.get("pii"):
        out.append(make_change("pii_removed", name))
    if (old.get("identifier_type"), old.get("reference_set")) != (new.get("identifier_type"), new.get("reference_set")):
        out.append(make_change("column_reference_changed", name))
    if (old.get("description") or "") != (new.get("description") or ""):
        out.append(make_change("description_changed", name))
    return out


def compare_schema(old_schema: Sequence[Mapping[str, Any]], new_schema: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    old = {c["name"]: dict(c) for c in old_schema}
    new = {c["name"]: dict(c) for c in new_schema}
    renames = _pair_renames(old, new)
    renamed_old = set(renames.values())
    out = [make_change("column_removed", n) for n in old if n not in new and n not in renamed_old]
    for name, col in new.items():
        if name in renames:
            out.append(make_change("column_renamed", name, f"{renames[name]} → {name}"))
            out.extend(_column_changes(name, old[renames[name]], col))
        elif name not in old:
            required = col.get("nullable") is False
            out.append(make_change("column_added_required" if required else "column_added_nullable", name))
            if col.get("pii"):
                out.append(make_change("pii_added", name))
        else:
            out.extend(_column_changes(name, old[name], col))
    return out


def _quality_changes(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    old_min, new_min = old.get("min_score"), new.get("min_score")
    if old_min is not None and (new_min is None or new_min < old_min):
        out.append(make_change("quality_sla_lowered", "min_score", f"{old_min} → {new_min}"))
    elif new_min is not None and (old_min is None or new_min > old_min):
        out.append(make_change("quality_sla_raised", "min_score", f"{old_min} → {new_min}"))
    old_rules, new_rules = set(old.get("rule_ids") or []), set(new.get("rule_ids") or [])
    if old_rules - new_rules:
        out.append(make_change("quality_sla_lowered", "rule_ids", f"削除: {sorted(old_rules - new_rules)}"))
    if new_rules - old_rules:
        out.append(make_change("quality_sla_raised", "rule_ids", f"追加: {sorted(new_rules - old_rules)}"))
    return out


def compare(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[dict[str, Any]]:
    """前の版 (old) から案 (new) への変更の一覧。キーは schema_, quality_sla, freshness_sla_hours, sensitivity など。"""
    out = compare_schema(old["schema_"], new["schema_"])
    out.extend(_quality_changes(old.get("quality_sla") or {}, new.get("quality_sla") or {}))
    old_fresh, new_fresh = old.get("freshness_sla_hours"), new.get("freshness_sla_hours")
    if old_fresh is not None and (new_fresh is None or new_fresh > old_fresh):
        out.append(make_change("freshness_sla_lowered", "freshness_sla_hours", f"{old_fresh} → {new_fresh}"))
    elif new_fresh is not None and (old_fresh is None or new_fresh < old_fresh):
        out.append(make_change("freshness_sla_raised", "freshness_sla_hours", f"{old_fresh} → {new_fresh}"))
    old_level, new_level = SENSITIVITY_LEVELS.index(old["sensitivity"]), SENSITIVITY_LEVELS.index(new["sensitivity"])
    if new_level > old_level:
        out.append(make_change("sensitivity_raised", "sensitivity", f"{old['sensitivity']} → {new['sensitivity']}"))
    elif new_level < old_level:
        out.append(make_change("sensitivity_lowered", "sensitivity", f"{old['sensitivity']} → {new['sensitivity']}"))
    old_use, new_use = set(old.get("purposes") or []), set(new.get("purposes") or [])
    if old_use - new_use:
        out.append(make_change("usage_narrowed", "purposes", f"削除: {sorted(old_use - new_use)}"))
    if new_use - old_use:
        out.append(make_change("usage_widened", "purposes", f"追加: {sorted(new_use - old_use)}"))
    old_cdm, new_cdm = old.get("conforms_to"), new.get("conforms_to")
    if old_cdm and old_cdm != new_cdm:
        out.append(make_change("conforms_to_changed", "conforms_to", f"{old_cdm} → {new_cdm}"))
    elif new_cdm and not old_cdm:
        out.append(make_change("conforms_to_added", "conforms_to", str(new_cdm)))
    if (old.get("description") or "") != (new.get("description") or ""):
        out.append(make_change("description_changed", "description"))
    return out


def required_bump(changes: Sequence[Mapping[str, Any]]) -> Bump:
    """変更が無くても、版を上げるなら patch。"""
    return max((Bump[c["bump"].upper()] for c in changes), default=Bump.PATCH)


def parse_version(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def actual_bump(old: tuple[int, int, int], new: tuple[int, int, int]) -> Bump | None:
    """版の上げ方。前の版以下なら None。"""
    if new <= old:
        return None
    if new[0] > old[0]:
        return Bump.MAJOR
    return Bump.MINOR if new[1] > old[1] else Bump.PATCH


def next_version(old: tuple[int, int, int], bump: Bump) -> str:
    if bump == Bump.MAJOR:
        return f"{old[0] + 1}.0.0"
    if bump == Bump.MINOR:
        return f"{old[0]}.{old[1] + 1}.0"
    return f"{old[0]}.{old[1]}.{old[2] + 1}"
