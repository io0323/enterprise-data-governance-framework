"""§15 / §18.2 接続仕様はオープン標準に限定: Data Contract と ODCS (Open Data Contract Standard) の変換.

内部のモデルは独自に持ち、ODCS v3 の主要部分だけを対応付ける。対応しないフィールドは保存せず、警告として返す
(警告には項目名だけを含め、値は含めない。examples などにデータの値が含まれうるため)。対応表は README を参照。
ODCS に対応する項目が無い機密度と利用条件などは customProperties の edgf.* で表す。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml
from pydantic import ValidationError

from edgf.auth import SENSITIVITY_LEVELS
from edgf.enums import ContractStatus
from edgf.models import ContractVersion, DataContract, DataProduct
from edgf.schemas import ContractVersionIn
from edgf.services.mesh import MeshValidationError
from edgf.services.mesh_compat import parse_type

API_VERSION = "v3.1.0"
MAX_DOCUMENT_BYTES = 1_000_000
CDM_DEFINITION_TYPE = "cdm"
CDM_URL_PREFIX = "urn:edgf:cdm:"
SENSITIVITY, PURPOSES, MIN_SCORE, MAPPINGS = "edgf.sensitivity", "edgf.purposes", "edgf.quality_min_score", "edgf.cdm_mappings"
COLUMN_EXTRAS = {"edgf.identifier_type": "identifier_type", "edgf.reference_set": "reference_set"}
FRESHNESS_PROPERTIES = ("latency", "ly", "freshness")
HOURS_PER_UNIT = {"h": 1, "hr": 1, "hour": 1, "hours": 1, "d": 24, "day": 24, "days": 24}
STATUS_TO_ODCS = {ContractStatus.DRAFT.value: "draft", ContractStatus.PUBLISHED.value: "active",
                  ContractStatus.DEPRECATED.value: "deprecated", ContractStatus.RETIRED.value: "retired"}
LOGICAL_TYPES = {
    **dict.fromkeys(("tinyint", "smallint", "int", "bigint"), "integer"), **dict.fromkeys(("float", "double", "decimal"), "number"),
    "boolean": "boolean", "date": "date", "timestamp": "timestamp", "time": "time", "array": "array", "object": "object", "map": "map",
}
TOP_KEYS = {"apiVersion", "kind", "id", "name", "version", "status", "domain", "dataProduct", "description", "schema", "slaProperties",
            "customProperties", "authoritativeDefinitions"}
OBJECT_KEYS = {"name", "logicalType", "properties", "quality"}
PROPERTY_KEYS = {"name", "logicalType", "physicalType", "required", "description", "classification", "criticalDataElement",
                 "customProperties"}


def _as_custom(items: Any, where: str) -> dict[str, Any]:
    if items is None:
        return {}
    if not isinstance(items, list):
        raise MeshValidationError(f"{where}.customProperties は配列で指定してください")
    return {i["property"]: i.get("value") for i in items if isinstance(i, dict) and i.get("property")}


def _warn_unknown(data: Mapping[str, Any], known: set[str], where: str, warnings: list[str]) -> None:
    warnings.extend(f"対応していないフィールドです: {where}{key}" for key in data if key not in known)


def _column(prop: Any, index: int, warnings: list[str], classifications: list[str]) -> dict[str, Any]:
    if not isinstance(prop, dict) or not prop.get("name"):
        raise MeshValidationError(f"schema[0].properties[{index}] に name がありません")
    name = str(prop["name"])
    where = f"schema[0].properties[{name}]."
    _warn_unknown(prop, PROPERTY_KEYS, where, warnings)
    type_name = prop.get("physicalType") or prop.get("logicalType")
    if not type_name:
        raise MeshValidationError(f"{where[:-1]} に physicalType も logicalType もありません")
    custom = _as_custom(prop.get("customProperties"), where[:-1])
    known_custom = {"pii", *COLUMN_EXTRAS}
    warnings.extend(f"対応していないフィールドです: {where}customProperties.{k}" for k in custom if k not in known_custom)
    pii = custom.get("pii") is True
    classification = prop.get("classification")
    if classification is not None:
        label = str(classification).lower()
        if label == "pii":
            pii = True
        elif label in SENSITIVITY_LEVELS:
            classifications.append(label)
            warnings.append(f"カラムの classification は Contract の機密度に集約します: {where}classification")
        else:
            warnings.append(f"対応していない classification です: {where}classification")
    column = {"name": name, "type": str(type_name), "nullable": not prop.get("required", False), "pii": pii,
              "description": str(prop.get("description") or "")}
    cde = prop.get("criticalDataElement")
    if cde is True:
        column["cde"] = True
    elif cde not in (None, False):
        warnings.append(f"criticalDataElement は true / false で指定してください: {where}criticalDataElement")
    column.update({field: custom[key] for key, field in COLUMN_EXTRAS.items() if custom.get(key) is not None})
    return column


def _freshness(items: Any, warnings: list[str]) -> int | None:
    hours = None
    for item in items or []:
        name = item.get("property") if isinstance(item, dict) else None
        if name not in FRESHNESS_PROPERTIES:
            warnings.append(f"対応していないフィールドです: slaProperties[{name}]")
            continue
        factor = HOURS_PER_UNIT.get(str(item.get("unit", "h")).lower())
        if factor is None or not isinstance(item.get("value"), int):
            raise MeshValidationError(f"slaProperties[{name}] は整数の value と、時間または日の unit で指定してください")
        hours = item["value"] * factor
    return hours


def _conforms_to(items: Any, warnings: list[str]) -> str | None:
    found = None
    for item in items or []:
        if isinstance(item, dict) and item.get("type") == CDM_DEFINITION_TYPE and item.get("url"):
            found = str(item["url"]).removeprefix(CDM_URL_PREFIX)
        else:
            warnings.append("対応していないフィールドです: authoritativeDefinitions の項目 (type: cdm 以外)")
    return found


def _rule_ids(items: Any, warnings: list[str]) -> list[str]:
    ids = []
    for item in items or []:
        if isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
            _warn_unknown(item, {"id", "type", "name"}, "schema[0].quality[].", warnings)
        else:
            warnings.append("対応していないフィールドです: schema[0].quality の id の無い項目")
    return ids


def parse_document(text: str, *, product: DataProduct) -> tuple[ContractVersionIn, dict[str, str | None], list[str]]:
    """ODCS の YAML を内部の形式に変換する。(版の内容, Contract の属性, 警告) を返す。"""
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise MeshValidationError(f"ODCS の文書が大きすぎます (上限 {MAX_DOCUMENT_BYTES} バイト)")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:  # 例外の文言には入力の一部が含まれるので返さない
        raise MeshValidationError("YAML を解釈できません") from e
    if not isinstance(doc, dict):
        raise MeshValidationError("ODCS の文書はオブジェクトで指定してください")
    if doc.get("kind") != "DataContract":
        raise MeshValidationError("kind は DataContract を指定してください")
    if not str(doc.get("apiVersion", "")).startswith("v3"):
        raise MeshValidationError("対応している apiVersion は v3 系です")
    warnings: list[str] = []
    _warn_unknown(doc, TOP_KEYS, "", warnings)
    if doc.get("status") not in (None, "draft", "proposed"):
        warnings.append("status は取り込みません (取り込んだ版は下書きで、公開には Product の Owner の承認が必要です)")
    if doc.get("domain") not in (None, product.domain):
        warnings.append(f"domain は Output Port の Product のドメイン ({product.domain}) を使います")
    if doc.get("dataProduct") not in (None, product.name):
        warnings.append(f"dataProduct は Output Port の Product ({product.name}) を使います")
    description = doc.get("description")
    if isinstance(description, dict):
        _warn_unknown(description, {"purpose"}, "description.", warnings)
        purpose = description.get("purpose")
    else:
        purpose = description
    objects = doc.get("schema")
    if not isinstance(objects, list) or not objects or not isinstance(objects[0], dict):
        raise MeshValidationError("schema にオブジェクトが必要です")
    if len(objects) > 1:
        warnings.append("schema の 2 つ目以降は取り込みません (Output Port と Contract は 1 対 1 です)")
    obj = objects[0]
    _warn_unknown(obj, OBJECT_KEYS, "schema[0].", warnings)
    props = obj.get("properties")
    if not isinstance(props, list) or not props:
        raise MeshValidationError("schema[0].properties が必要です")
    classifications: list[str] = []
    columns = [_column(p, i, warnings, classifications) for i, p in enumerate(props)]
    custom = _as_custom(doc.get("customProperties"), "")
    warnings.extend(f"対応していないフィールドです: customProperties.{k}" for k in custom if k not in (SENSITIVITY, PURPOSES, MIN_SCORE, MAPPINGS))
    sensitivity = custom.get(SENSITIVITY) or (max(classifications, key=SENSITIVITY_LEVELS.index) if classifications else None)
    if sensitivity is None:
        raise MeshValidationError(f"機密度が必要です (customProperties の {SENSITIVITY}、またはカラムの classification)")
    try:
        version = ContractVersionIn(
            version=str(doc.get("version", "")), description=str(purpose or ""), schema=columns, sensitivity=sensitivity,
            quality_sla={"min_score": custom.get(MIN_SCORE), "rule_ids": _rule_ids(obj.get("quality"), warnings)},
            freshness_sla_hours=_freshness(doc.get("slaProperties"), warnings), purposes=custom.get(PURPOSES) or [],
            conforms_to=_conforms_to(doc.get("authoritativeDefinitions"), warnings), cdm_mappings=custom.get(MAPPINGS) or {})
    except ValidationError as e:
        # 入力の値は返さない (項目の位置と種類だけ)
        fields = sorted({".".join(str(x) for x in err["loc"]) for err in e.errors()})
        raise MeshValidationError(f"ODCS の内容が不正です: {fields}") from e
    return version, {"external_id": str(doc["id"]) if doc.get("id") else None, "name": str(doc["name"]) if doc.get("name") else None,
                     "schema_object": str(obj["name"]) if obj.get("name") else None}, warnings


def _logical_type(type_name: str) -> str:
    return LOGICAL_TYPES.get(parse_type(type_name)[0], "string")


def _export_property(column: Mapping[str, Any]) -> dict[str, Any]:
    prop: dict[str, Any] = {"name": column["name"], "logicalType": _logical_type(column["type"]), "physicalType": column["type"],
                            "required": not column.get("nullable", True)}
    if column.get("description"):
        prop["description"] = column["description"]
    if column.get("cde"):
        prop["criticalDataElement"] = True
    custom = [{"property": "pii", "value": True}] if column.get("pii") else []
    custom += [{"property": key, "value": column[field]} for key, field in COLUMN_EXTRAS.items() if column.get(field)]
    if custom:
        prop["customProperties"] = custom
    return prop


def export_document(contract: DataContract, row: ContractVersion, product: DataProduct) -> dict[str, Any]:
    sla = row.quality_sla or {}
    obj: dict[str, Any] = {"name": contract.schema_object, "logicalType": "object",
                           "properties": [_export_property(c) for c in row.schema_]}
    if sla.get("rule_ids"):
        obj["quality"] = [{"id": rule_id, "type": "library"} for rule_id in sla["rule_ids"]]
    custom: list[dict[str, Any]] = [{"property": SENSITIVITY, "value": row.sensitivity}]
    if row.purposes:
        custom.append({"property": PURPOSES, "value": list(row.purposes)})
    if sla.get("min_score") is not None:
        custom.append({"property": MIN_SCORE, "value": sla["min_score"]})
    if row.cdm_mappings:
        custom.append({"property": MAPPINGS, "value": dict(row.cdm_mappings)})
    doc: dict[str, Any] = {"apiVersion": API_VERSION, "kind": "DataContract", "id": contract.external_id or contract.id, "name": contract.name,
                           "version": row.version, "status": STATUS_TO_ODCS[row.status], "domain": product.domain,
                           "dataProduct": product.name, "description": {"purpose": row.description}, "schema": [obj]}
    if row.freshness_sla_hours is not None:
        doc["slaProperties"] = [{"property": "latency", "value": row.freshness_sla_hours, "unit": "h"}]
    if row.conforms_to:
        doc["authoritativeDefinitions"] = [{"type": CDM_DEFINITION_TYPE, "url": f"{CDM_URL_PREFIX}{row.conforms_to}"}]
    doc["customProperties"] = custom
    return doc


def to_yaml(doc: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(doc), allow_unicode=True, sort_keys=False)
