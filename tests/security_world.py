"""Phase 14c: 目印を埋めた restricted 資産と、その周辺のデータを用意する.

目印 `zzsecret` は、閲覧権限の無い利用者への応答のどこにも現れてはならない。
Product の名前と説明、ポートの名前は公開する設計 (Phase 11) なので目印を入れない。
ポートの裏の資産とアドレス、Contract のスキーマには入れる。
"""
from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from tests.ai_helpers import API, CONTROLS, HASH, U

MARK = "zzsecret"
FUTURE = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
ADMIN, COMMITTEE = U("root", "admin"), U("chair", "committee")
OWNER_H = U("zzowner", "owner", "sales")
STEWARD_H = U("st", "steward", "sales")
CUSTODIAN_H = U("cust", "custodian", "sales")
ANALYST = U("analyst")                                # ロールなし = consumer (clearance internal)
OUTSIDER = U("other-owner", "owner,steward,custodian", "hr")  # 他ドメインの当事者 (clearance confidential)


def _ok(r):
    assert r.status_code in (200, 201, 202), f"{r.request.method} {r.request.url}: {r.text}"
    return r.json()


def _asset(client, qn, sensitivity="restricted", **kw) -> dict:
    return _ok(client.post(f"{API}/assets", headers=ADMIN, json={
        "qualified_name": qn, "name": qn, "description": f"{MARK} の説明" if MARK in qn else "公開の説明", "domain": "sales",
        "data_class": "analytical", "sensitivity": sensitivity, "owner": "zzowner", "steward": "st", "custodian": "cust",
        "controls": CONTROLS, **kw}))


def build_world(client) -> dict[str, Any]:
    """全パラメータの値を返す。値が無い種類は存在しない ID (404 になるだけ) で埋める。"""
    for name in ("sales", "hr"):
        _ok(client.post(f"{API}/domains", headers=ADMIN, json={"name": name, "owner": "zzowner"}))
    columns = [{"name": f"{MARK}_column", "type": "string", "identifier_type": "email"}]
    hidden = _asset(client, f"sales.{MARK}_orders", schema=columns, is_cde=True, retention_days=1)
    neighbor = _asset(client, f"sales.{MARK}_neighbor")
    visible = _asset(client, "sales.visible", sensitivity="internal")
    downstream = _asset(client, "sales.visible_down", sensitivity="internal")
    # Lineage: 見える資産の間に、見えない資産が挟まる
    for source, target in ((visible["qualified_name"], neighbor["qualified_name"]), (neighbor["qualified_name"], hidden["qualified_name"]),
                           (hidden["qualified_name"], downstream["qualified_name"])):
        _ok(client.post(f"{API}/lineage/edges", headers=ADMIN, json={"source": source, "target": target, "job": f"{MARK}_job", "transformation": f"SELECT {MARK}_expr"}))
    # Glossary、品質、Issue、Policy の評価
    term = _ok(client.post(f"{API}/glossary/terms", headers=ADMIN, json={"name": "用語", "definition": "定義"}))
    rule = _ok(client.post(f"{API}/quality/assets/{hidden['id']}/rules", headers=OWNER_H, json={
        "name": f"{MARK}_rule", "dimension": "completeness", "rule_type": "not_null", "column": f"{MARK}_column", "threshold": 0.99}))
    _ok(client.post(f"{API}/quality/assets/{hidden['id']}/runs", headers=OWNER_H, json={"records": [{f"{MARK}_column": None}]}))
    issue = _ok(client.post(f"{API}/issues", headers=OWNER_H, json={"title": f"{MARK} の障害", "asset_id": hidden["id"], "description": f"{MARK} の詳細"}))
    _ok(client.post(f"{API}/policies/evaluate", headers=ADMIN))
    # 他の利用者の承認の申請
    approval = _ok(client.post(f"{API}/approvals", headers=U("requester"), json={
        "type": "access_request", "asset_id": hidden["id"], "reason": f"{MARK} を調査", "purpose": "分析", "expires_at": FUTURE}))
    # Data Mesh
    product = _ok(client.post(f"{API}/mesh/products", headers=STEWARD_H, json={"name": "orders", "domain": "sales", "owner": "zzowner", "description": "受注の Product"}))
    port = _ok(client.post(f"{API}/mesh/products/{product['id']}/ports", headers=STEWARD_H, json={
        "name": "main", "asset_id": hidden["id"], "port_type": "table", "address_uri": f"warehouse://sales/{MARK}"}))
    contract = _ok(client.post(f"{API}/mesh/contracts", headers=STEWARD_H, json={"output_port_id": port["id"]}))
    contract_version = _ok(client.post(f"{API}/mesh/contracts/{contract['id']}/versions", headers=STEWARD_H, json={
        "version": "1.0.0", "description": f"{MARK} の契約", "sensitivity": "restricted",
        "schema": [{"name": f"{MARK}_col", "type": "string", "nullable": False, "description": MARK}]}))
    # AI: Dataset → Model
    dataset = _asset(client, f"ai.{MARK}_dataset", data_class="ai", ai_kind="training_dataset")
    model = _asset(client, f"ai.{MARK}_model", data_class="ai", ai_kind="model")
    _ok(client.put(f"{API}/ai/assets/{dataset['id']}/dataset-card", headers=OWNER_H, json={"purpose": f"{MARK} の目的"}))
    dv = _ok(client.post(f"{API}/ai/assets/{dataset['id']}/dataset-versions", headers=CUSTODIAN_H, json={
        "version": "v1", "manifest_hash": HASH, "record_count": 10, "sources": [{"asset_id": hidden["id"], "version": "2026-01"}]}))
    mv = _ok(client.post(f"{API}/ai/assets/{model['id']}/model-versions", headers=CUSTODIAN_H, json={"version": "m1", "dataset_version_ids": [dv["id"]]}))
    # KPI の履歴と定期レビュー
    for job in ("kpi_snapshot", "review_campaigns"):
        _ok(client.post(f"{API}/ops/jobs/{job}/run", headers=ADMIN))
    none = str(uuid.uuid4())
    return {
        "hidden": hidden, "neighbor": neighbor, "visible": visible,
        "values": {
            "asset_id": [hidden["id"], dataset["id"], model["id"]],
            "qualified_name": [hidden["qualified_name"], visible["qualified_name"]],
            "term_id": [term["id"]], "rule_id": [rule["id"]], "issue_id": [issue["id"]],
            "approval_id": [approval["id"]],
            "product_id": [product["id"]], "port_id": [port["id"]], "contract_id": [contract["id"]],
            "version_id": [contract_version["id"], dv["id"], mv["id"]],
            "model_version_id": [mv["id"]], "dataset_version_id": [dv["id"]],
            "version": ["1.0.0", "1"], "number": ["1"],
            "entity_type": ["party"], "name": ["party", "kpi_snapshot"],
        },
        "none": none,
    }


def candidates(route, world) -> list[tuple[str, str]]:
    """ルートの全メソッドと、パスパラメータの候補の組み合わせ。"""
    params = route.params
    values = [world["values"].get(p, [world["none"]]) for p in params]
    out = []
    for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
        for combo in itertools.product(*values):
            path = route.path
            for p, v in zip(params, combo, strict=True):
                path = path.replace(f"{{{p}}}", str(v))
            out.append((method, path))
    return out


def _sample(schema: dict, components: dict, world: dict, name: str = "", depth: int = 0) -> Any:
    if "$ref" in schema:
        return _sample(components[schema["$ref"].rsplit("/", 1)[1]], components, world, name, depth)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            options = [s for s in schema[key] if s.get("type") != "null"]
            return _sample(options[0], components, world, name, depth) if options else None
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        if depth > 6:
            return {}
        return {k: _sample(v, components, world, k, depth + 1) for k, v in schema.get("properties", {}).items() if k in schema.get("required", [])}
    if kind == "array":
        return [_sample(schema.get("items", {}), components, world, name, depth + 1) for _ in range(max(1, schema.get("minItems", 1)))]
    if kind == "integer":
        return max(1, schema.get("minimum", 1))
    if kind == "number":
        return max(0.5, schema.get("minimum", 0.5))
    if kind == "boolean":
        return True
    if kind == "string":
        if schema.get("format") == "date-time":
            return FUTURE
        if schema.get("format") == "date":
            return FUTURE[:10]
        values = world["values"].get(name)
        if values:
            return values[0]
        if "pattern" in schema or "url" in name:
            return {"url": "https://example.test/x"}.get(name, "a")
        return "a" * max(1, schema.get("minLength", 1))
    return "a"


def fill_body(openapi: dict, path: str, method: str, world: dict) -> Any:
    """OpenAPI のスキーマから、必須項目だけを埋めた本文を作る。"""
    operation = openapi["paths"].get(path, {}).get(method.lower(), {})
    content = operation.get("requestBody", {}).get("content", {})
    schema = content.get("application/json", {}).get("schema")
    return _sample(schema, openapi["components"]["schemas"], world) if schema else None


__all__ = ["ADMIN", "ANALYST", "COMMITTEE", "MARK", "OUTSIDER", "build_world", "candidates", "fill_body"]
