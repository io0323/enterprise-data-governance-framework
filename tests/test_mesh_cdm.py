"""Phase 11d (冒頭の役割分離 / §15 Standardization): CDM の取り込みと準拠の検証."""
from __future__ import annotations

import pytest

from tests.mesh_helpers import (ADMIN, API, COLUMNS, COMMITTEE, MESH, PARTY_SCHEMA, STEWARD, U, cdm_asset, draft, import_cdm, publish, published,
                                setup_contract)

MAPPINGS = {"party_id": "order_id", "total": "amount"}


@pytest.fixture()
def cdm(client):
    return {"client": client, "asset": cdm_asset(client)}


# ---------------------------------------------------------------- 取り込み
def test_import_permissions_and_immutability(cdm):
    client, a = cdm["client"], cdm["asset"]
    for who in (U("x", "owner", "*"), STEWARD, U("cdm-owner")):
        import_cdm(client, a["id"], code=403, headers=who)
    body = import_cdm(client, a["id"], headers=ADMIN)
    assert body["version"]["version"] == "1" and len(body["version"]["content_hash"]) == 64 and body["warnings"] == []
    # 取り込んだ版は変更できない
    import_cdm(client, a["id"], schema={**PARTY_SCHEMA, "required": []}, code=409)
    url = f"{MESH}/cdm/entities/party/versions/1"
    assert client.get(url, headers=ADMIN).json()["json_schema"] == PARTY_SCHEMA
    for method in ("put", "patch", "delete"):
        assert getattr(client, method)(url, headers=COMMITTEE).status_code == 405
    # 作成・編集の API は無い
    assert client.post(f"{MESH}/cdm/entities", headers=COMMITTEE, json={"name": "x"}).status_code == 405
    events = client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000}).json()
    assert any(e["action"] == "cdm.import" and e["entity_id"] == "party@1" for e in events)


def test_import_requires_metadata_asset(cdm):
    client, a = cdm["client"], cdm["asset"]
    r = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "x.y", "name": "y", "data_class": "master", "sensitivity": "internal"})
    import_cdm(client, r.json()["id"], code=422)
    import_cdm(client, "none", code=422)
    import_cdm(client, a["id"])
    other = cdm_asset(client, "cdm.other")
    import_cdm(client, other["id"], version="2", code=409)  # エンティティと資産の対応は変えられない
    import_cdm(client, a["id"], entity="another", code=409)  # 資産は 1 対 1


@pytest.mark.parametrize("schema", [
    {}, {"type": "array"}, {"type": "object", "properties": {}}, {"type": "object", "properties": {"a": "string"}},
    {"type": "object", "properties": {"a": {"type": "unknown"}}}, {"type": "object", "properties": {"a": {}}},
    {"type": "object", "properties": {"a": {"type": ["string", "integer"]}}},
    {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["b"]},
    {"type": "object", "properties": {"a": {"type": "string"}}, "required": "a"},
    {"type": "object", "properties": {"a": {"type": "string"}}, "allOf": [{}]},
    {"type": "object", "properties": {"a": {"$ref": "#/x"}}},
    {"type": "object", "properties": {"a": {"type": "string", "oneOf": []}}},
])
def test_unsupported_schema_is_rejected(cdm, schema):
    import_cdm(cdm["client"], cdm["asset"]["id"], schema=schema, code=422)


def test_annotations_are_warnings(cdm):
    schema = {"type": "object", "x-owner": "a", "properties": {"a": {"type": "string", "maxLength": 3, "description": "d"}}}
    body = import_cdm(cdm["client"], cdm["asset"]["id"], schema=schema)
    assert body["warnings"] == ["無視しました: x-owner", "無視しました: a.maxLength"]


def test_reads_follow_abac(client):
    r = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "cdm.secret", "name": "s", "data_class": "metadata",
                                                          "sensitivity": "restricted", "owner": "o"})
    import_cdm(client, r.json()["id"], entity="secret")
    public = cdm_asset(client)
    import_cdm(client, public["id"])
    reader = U("reader", clearance="internal")
    assert [e["name"] for e in client.get(f"{MESH}/cdm/entities", headers=reader).json()] == ["party"]
    assert client.get(f"{MESH}/cdm/entities/secret", headers=reader).status_code == 403
    assert client.get(f"{MESH}/cdm/entities/party", headers=reader).json()["asset_id"] == public["id"]
    assert client.get(f"{MESH}/cdm/entities/none", headers=reader).status_code == 404
    assert client.get(f"{MESH}/cdm/entities/party/versions", headers=reader).headers["X-Total-Count"] == "1"
    assert client.get(f"{MESH}/cdm/entities/party/versions/9", headers=reader).status_code == 404


# ---------------------------------------------------------------- 準拠の検証
@pytest.fixture()
def conform(cdm):
    client = cdm["client"]
    import_cdm(client, cdm["asset"]["id"])
    ctx = setup_contract(client)
    return {**cdm, **ctx, "cid": ctx["contract"]["id"], "cdm_asset": cdm["asset"]}


def _publish_expect_violation(client, cid, text, **kw):
    v = draft(client, cid, conforms_to="party@1", **kw)
    r = client.post(f"{MESH}/contract-versions/{v['id']}/publish", headers=U("po", "owner", "sales"))
    assert r.status_code == 409 and r.json()["type"].endswith("contract-not-conforming"), r.text
    assert any(text in x for x in r.json()["violations"]), r.json()["violations"]


def test_conforming_contract_is_published(conform):
    v = published(conform["client"], conform["cid"], conforms_to="party@1", cdm_mappings=MAPPINGS)
    assert v["status"] == "published" and v["conforms_to"] == "party@1"


def test_same_names_need_no_mapping(conform):
    client = conform["client"]
    cols = [{"name": "party_id", "type": "bigint", "nullable": False, "description": "d"}]
    assert published(client, conform["cid"], conforms_to="party@1", schema=cols)["status"] == "published"


@pytest.mark.parametrize("kw,text", [
    ({}, "必須の属性"),  # 対応付けなし → party_id が無い
    ({"cdm_mappings": {**MAPPINGS, "unknown": "amount"}}, "CDM にありません"),
    ({"cdm_mappings": {"party_id": "nope"}}, "Contract にありません"),
    ({"cdm_mappings": {"party_id": "order_id", "total": "order_id"}}, "複数の属性"),
    ({"cdm_mappings": MAPPINGS, "schema": [{"name": "order_id", "type": "string", "nullable": False, "description": "d"},
                                           COLUMNS[1]]}, "型に互換性"),
    ({"cdm_mappings": MAPPINGS, "schema": [{"name": "order_id", "type": "bigint", "nullable": True, "description": "d"},
                                           COLUMNS[1]]}, "nullable"),
    ({"cdm_mappings": MAPPINGS, "schema": [COLUMNS[0], {"name": "amount", "type": "string", "description": "d"}]}, "型に互換性"),
])
def test_non_conforming_contract_is_rejected(conform, kw, text):
    _publish_expect_violation(conform["client"], conform["cid"], text, **kw)


def test_unknown_cdm_version_is_rejected(conform):
    client = conform["client"]
    v = draft(client, conform["cid"], conforms_to="party@9", cdm_mappings=MAPPINGS)
    r = client.post(f"{MESH}/contract-versions/{v['id']}/publish", headers=U("po", "owner", "sales"))
    assert r.status_code == 409 and "取り込まれていません" in r.json()["violations"][0]


@pytest.mark.parametrize("prop,col_type,ok", [
    ({"type": "string", "format": "date"}, "date", True),
    ({"type": "string", "format": "date"}, "string", False),
    ({"type": "string", "format": "date-time"}, "timestamp", True),
    ({"type": "string", "format": "date-time"}, "date", False),
    ({"type": "string", "format": "uuid"}, "string", True),
    ({"type": "string", "format": "email"}, "varchar(100)", True),
    ({"type": "integer"}, "decimal(10,2)", False),
    ({"type": "number"}, "int", True),
    ({"type": "number"}, "decimal(10,2)", True),
    ({"type": "boolean"}, "bool", True),
    ({"type": "array"}, "array", True),
    ({"type": "object"}, "map", True),
    ({"type": "string"}, "geometry", False),
])
def test_type_compatibility(cdm, prop, col_type, ok):
    client = cdm["client"]
    import_cdm(client, cdm["asset"]["id"], entity="thing", schema={"type": "object", "properties": {"v": prop}})
    ctx = setup_contract(client)
    v = draft(client, ctx["contract"]["id"], conforms_to="thing@1", schema=[{"name": "v", "type": col_type, "description": "d"}])
    publish(client, v["id"], code=200 if ok else 409)


def test_check_api_reports_conformance(conform):
    client, cid = conform["client"], conform["cid"]
    url = f"{MESH}/contracts/{cid}/check"
    assert client.post(url, headers=U("cust"), json={"schema": COLUMNS, "sensitivity": "internal"}).json()["conformance"] is None
    bad = client.post(url, headers=U("cust"), json={"schema": COLUMNS, "sensitivity": "internal", "conforms_to": "party@1"}).json()
    assert bad["conformance"]["ok"] is False
    good = client.post(url, headers=U("cust"), json={"schema": COLUMNS, "sensitivity": "internal", "conforms_to": "party@1", "cdm_mappings": MAPPINGS}).json()
    assert good["conformance"] == {"conforms_to": "party@1", "ok": True, "violations": []}


# ---------------------------------------------------------------- 新しい版の取り込み
def _cdm_issues(client):
    rows = client.get(f"{API}/issues", headers=ADMIN, params={"limit": 1000}).json()
    return [i for i in rows if (i["source_ref"] or "").startswith("mesh-cdm:")]


def test_new_version_revalidates_and_opens_issue(conform):
    client, cid, a = conform["client"], conform["cid"], conform["cdm_asset"]
    published(client, cid, conforms_to="party@1", cdm_mappings=MAPPINGS)
    stricter = {"type": "object", "properties": {**PARTY_SCHEMA["properties"], "country": {"type": "string"}},
                "required": ["party_id", "country"]}
    body = import_cdm(client, a["id"], version="2", schema=stricter)
    result = body["revalidation"]
    assert [(r["contract_id"], r["ok"]) for r in result] == [(cid, False)]
    assert "country" in result[0]["violations"][0] and result[0]["issue_id"]
    issues = _cdm_issues(client)
    assert len(issues) == 1 and issues[0]["assignee"] == "po" and "party@2" in issues[0]["title"]
    # 宣言した版には準拠したままなので、Contract の状態は変えない
    latest = client.get(f"{MESH}/contracts/{cid}/versions", headers=ADMIN).json()[-1]
    assert latest["status"] == "published" and latest["conforms_to"] == "party@1"


def test_compatible_new_version_opens_no_issue_and_unrelated_is_ignored(conform):
    client, cid, a = conform["client"], conform["cid"], conform["cdm_asset"]
    published(client, cid, conforms_to="party@1", cdm_mappings=MAPPINGS)
    other = setup_contract(client, qn="sales.other")
    published(client, other["contract"]["id"])  # 宣言なし
    body = import_cdm(client, a["id"], version="2")
    assert [(r["contract_id"], r["ok"], r["issue_id"]) for r in body["revalidation"]] == [(cid, True, None)]
    assert _cdm_issues(client) == []
    second = cdm_asset(client, "cdm.other")
    assert import_cdm(client, second["id"], entity="other", schema={"type": "object", "properties": {"a": {"type": "string"}}})["revalidation"] == []
