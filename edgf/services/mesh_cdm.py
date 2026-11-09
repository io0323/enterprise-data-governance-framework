"""冒頭の役割分離 / §15 Standardization: CDM の取り込みと準拠の検証.

CDM は「データの形」を定義し、Governance は準拠を検証する。ここにあるのは外部の定義の写しと検証だけで、
作成・編集・設計の機能は持たない。取り込んだ版は変更できない (Reference Data と同じ方式)。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.enums import DataClass, LifecycleState
from edgf.models import CdmEntity, CdmVersion, DataAsset
from edgf.services.mesh import MeshStateError, MeshValidationError
from edgf.services.mesh_compat import parse_type

JSON_TYPES = {"string", "integer", "number", "boolean", "array", "object"}
# 検証の意味を変えるキーワード。黙って無視すると不合格を見逃すので拒否する
UNSUPPORTED = {"$ref", "allOf", "anyOf", "oneOf", "not", "if", "then", "else", "patternProperties", "dependentSchemas",
               "dependentRequired", "propertyNames", "$defs", "definitions"}
ROOT_KEYS = {"type", "properties", "required", "title", "description", "$schema", "$id", "additionalProperties"}
PROPERTY_KEYS = {"type", "format", "title", "description"}
# Contract の型 (正規化後) → 受け入れる JSON Schema の型
TYPE_TABLE: Mapping[str, frozenset[str]] = {
    **{t: frozenset({"integer", "number"}) for t in ("tinyint", "smallint", "int", "bigint")},
    **{t: frozenset({"number"}) for t in ("float", "double", "decimal")},
    **{t: frozenset({"string"}) for t in ("string", "varchar", "char", "date", "timestamp", "time", "uuid")},
    "boolean": frozenset({"boolean"}), "array": frozenset({"array"}), "object": frozenset({"object"}),
    "map": frozenset({"object"}), "struct": frozenset({"object"}),
}
# format が日付や時刻を要求するときに認める Contract の型
FORMAT_TABLE: Mapping[str, frozenset[str]] = {
    "date": frozenset({"date"}), "date-time": frozenset({"timestamp"}), "time": frozenset({"time"}), "uuid": frozenset({"uuid", "string", "varchar", "char"}),
}


@dataclass(frozen=True)
class Attribute:
    name: str
    json_type: str
    nullable: bool
    required: bool
    format: str | None


def parse(schema: Mapping[str, Any]) -> tuple[list[Attribute], list[str]]:
    """JSON Schema の一部を解釈する。意味を変えるキーワードは拒否し、注釈や未対応の制約は警告にする。"""
    found = sorted(UNSUPPORTED & set(schema))
    if found:
        raise MeshValidationError(f"対応していない JSON Schema のキーワードです: {found}")
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict) or not schema["properties"]:
        raise MeshValidationError("JSON Schema は type: object と properties が必要です")
    required = schema.get("required") or []
    if not isinstance(required, list) or set(required) - set(schema["properties"]):
        raise MeshValidationError("required は properties にある属性の配列で指定してください")
    warnings = [f"無視しました: {k}" for k in sorted(set(schema) - ROOT_KEYS)]
    attrs = []
    for name, spec in schema["properties"].items():
        if not isinstance(spec, dict):
            raise MeshValidationError(f"{name}: 属性の定義がオブジェクトではありません")
        bad = sorted(UNSUPPORTED & set(spec))
        if bad:
            raise MeshValidationError(f"{name}: 対応していない JSON Schema のキーワードです: {bad}")
        declared = spec.get("type")
        types = declared if isinstance(declared, list) else [declared]
        concrete = [t for t in types if t != "null"]
        if len(concrete) != 1 or concrete[0] not in JSON_TYPES:
            raise MeshValidationError(f"{name}: type は 1 つの型か、それと null の組で指定してください: {declared}")
        warnings.extend(f"無視しました: {name}.{k}" for k in sorted(set(spec) - PROPERTY_KEYS))
        attrs.append(Attribute(name=name, json_type=concrete[0], nullable="null" in types, required=name in required,
                               format=spec.get("format")))
    return attrs, warnings


def import_version(db: Session, *, entity: str, asset_id: str, version: str, json_schema: Mapping[str, Any], source_uri: str | None,
                   actor: str) -> tuple[CdmVersion, list[str]]:
    _, warnings = parse(json_schema)
    asset = db.get(DataAsset, asset_id)
    if asset is None or asset.lifecycle_state == LifecycleState.DELETED.value:
        raise MeshValidationError(f"CDM の資産が Catalog にありません: {asset_id}")
    if asset.data_class != DataClass.METADATA.value:
        raise MeshValidationError(f"CDM は data_class=metadata の資産に対応付けます (現在: {asset.data_class})")
    row = db.get(CdmEntity, entity)
    if row is None:
        if db.scalar(select(CdmEntity.name).where(CdmEntity.asset_id == asset.id)):
            raise MeshStateError(f"この資産は別の CDM エンティティに対応付けられています: {asset.qualified_name}")
        db.add(CdmEntity(name=entity, asset_id=asset.id, created_by=actor))
        db.flush()
    elif row.asset_id != asset.id:
        raise MeshStateError(f"エンティティ {entity} は別の資産に対応付けられています")
    if db.scalar(select(CdmVersion.id).where(CdmVersion.entity == entity, CdmVersion.version == version)):
        raise MeshStateError(f"{entity}@{version} は取り込み済みです (取り込んだ版は変更できません)")
    digest = hashlib.sha256(json.dumps(json_schema, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    version_row = CdmVersion(entity=entity, version=version, json_schema=dict(json_schema), content_hash=digest,
                             source_uri=source_uri, imported_by=actor)
    db.add(version_row)
    db.flush()
    return version_row, warnings


def _type_violation(attr: Attribute, column: Mapping[str, Any]) -> str | None:
    base, _ = parse_type(column["type"])
    accepted = TYPE_TABLE.get(base, frozenset())
    if attr.json_type not in accepted:
        return f"{attr.name}: 型に互換性がありません (CDM: {attr.json_type}, Contract: {column['type']})"
    allowed_formats = FORMAT_TABLE.get(attr.format or "")
    if allowed_formats is not None and base not in allowed_formats:
        return f"{attr.name}: format {attr.format} と型 {column['type']} に互換性がありません"
    return None


def validate(db: Session, target: str, schema: Sequence[Mapping[str, Any]], mappings: Mapping[str, str]) -> list[str]:
    """準拠の検証: 名前の対応付けが正しいか、必須の属性があるか、型に互換性があるか。空なら合格。"""
    entity, _, version = target.partition("@")
    row = db.scalar(select(CdmVersion).where(CdmVersion.entity == entity, CdmVersion.version == version))
    if row is None:
        return [f"CDM の定義が取り込まれていません: {target}"]
    attrs, _ = parse(row.json_schema)
    known = {a.name for a in attrs}
    columns = {c["name"]: c for c in schema}
    violations = [f"対応付けの属性が CDM にありません: {a}" for a in sorted(set(mappings) - known)]
    violations += [f"対応付けのカラムが Contract にありません: {c}" for c in sorted(set(mappings.values()) - set(columns))]
    duplicated = sorted({c for c in mappings.values() if list(mappings.values()).count(c) > 1})
    violations += [f"1 つのカラムに複数の属性を対応付けています: {c}" for c in duplicated]
    for attr in attrs:
        column = columns.get(mappings.get(attr.name, attr.name))
        if column is None:
            if attr.required:
                violations.append(f"必須の属性がありません: {attr.name}")
            continue
        problem = _type_violation(attr, column)
        if problem:
            violations.append(problem)
        if attr.required and not attr.nullable and column.get("nullable", True):
            violations.append(f"{attr.name}: CDM では必須で null を許しませんが、カラムが nullable です")
    return violations
