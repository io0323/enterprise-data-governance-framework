"""Phase 11f: ODCS の取り込みと書き出し."""
from __future__ import annotations

import yaml

from tests.conftest import dump_database
from tests.mesh_helpers import ADMIN, API, MESH, STEWARD, U, add_port, cdm_asset, import_cdm, port_asset, published, setup_contract

YAML = {"Content-Type": "application/yaml"}
MARKER = "SAMPLE-VALUE-81f2a"


def _doc(**kw) -> dict:
    return {
        "apiVersion": "v3.1.0", "kind": "DataContract", "id": "urn:contract:orders", "name": "受注の契約", "version": "1.2.0", "status": "draft",
        "domain": "sales", "dataProduct": "product-sales-orders_pub", "description": {"purpose": "受注の提供"},
        "schema": [{"name": "orders", "logicalType": "object", "properties": [
            {"name": "order_id", "logicalType": "integer", "physicalType": "bigint", "required": True, "description": "受注 ID",
             "customProperties": [{"property": "edgf.identifier_type", "value": "order_id"}]},
            {"name": "email", "logicalType": "string", "physicalType": "varchar(200)", "required": False, "description": "連絡先",
             "customProperties": [{"property": "pii", "value": True}]},
        ]}],
        "slaProperties": [{"property": "latency", "value": 24, "unit": "h"}],
        "customProperties": [{"property": "edgf.sensitivity", "value": "confidential"},
                             {"property": "edgf.purposes", "value": ["analytics"]},
                             {"property": "edgf.quality_min_score", "value": 0.95}],
        **kw}


def _import(client, port_id, doc, code=201, headers=STEWARD):
    text = doc if isinstance(doc, str) else yaml.safe_dump(doc, allow_unicode=True)
    r = client.post(f"{MESH}/contracts/import", params={"output_port_id": port_id}, headers={**headers, **YAML}, content=text.encode())
    assert r.status_code == code, r.text
    return r.json()


def _purpose(client):
    assert client.post(f"{API}/privacy/purposes", headers=U("chair", "committee"), json={
        "id": "analytics", "description": "d", "default_legal_basis": "contract"}).status_code == 201


def _ctx(client):
    _purpose(client)
    return setup_contract(client, sensitivity="confidential")


def test_round_trip_keeps_main_fields(client):
    ctx = _ctx(client)
    source = _doc()
    body = _import(client, ctx["port"]["id"], source)
    assert body["warnings"] == []
    v = body["version"]
    assert v["status"] == "draft" and v["sensitivity"] == "confidential" and v["freshness_sla_hours"] == 24
    assert v["schema"][1]["pii"] is True and v["schema"][0]["nullable"] is False
    exported = yaml.safe_load(client.get(f"{MESH}/contracts/{body['contract_id']}/export", headers=ADMIN, params={"version": "1.2.0"}).text)
    for key in ("apiVersion", "kind", "id", "name", "version", "status", "domain", "dataProduct", "description", "schema", "slaProperties"):
        assert exported[key] == source[key], key
    assert {i["property"]: i["value"] for i in exported["customProperties"]} == {i["property"]: i["value"] for i in source["customProperties"]}
    # 書き出したものをもう一度取り込んでも同じ内容になる
    other = setup_contract(client, sensitivity="confidential", qn="sales.second")
    again = _import(client, other["port"]["id"], exported)
    assert again["version"]["schema"] == v["schema"] and again["version"]["quality_sla"] == v["quality_sla"]


def test_cdm_and_quality_rules_round_trip(client):
    _purpose(client)
    ctx = setup_contract(client)
    cdm = cdm_asset(client)
    import_cdm(client, cdm["id"])
    qa = U("qa", "admin,owner,custodian", "*")
    rule = client.post(f"{API}/quality/assets/{ctx['asset']['id']}/rules", headers=qa, json={
        "name": "nn", "dimension": "completeness", "rule_type": "not_null", "column": "order_id"}).json()
    v = published(client, ctx["contract"]["id"], conforms_to="party@1", cdm_mappings={"party_id": "order_id"},
                  quality_sla={"rule_ids": [rule["id"]]}, freshness_sla_hours=48, purposes=["analytics"])
    assert v["status"] == "published"
    doc = yaml.safe_load(client.get(f"{MESH}/contracts/{ctx['contract']['id']}/export", headers=ADMIN).text)
    assert doc["status"] == "active" and doc["version"] == "1.0.0" and doc["id"] == ctx["contract"]["id"]
    assert doc["authoritativeDefinitions"] == [{"type": "cdm", "url": "urn:edgf:cdm:party@1"}]
    assert doc["schema"][0]["quality"] == [{"id": rule["id"], "type": "library"}]
    assert doc["slaProperties"] == [{"property": "latency", "value": 48, "unit": "h"}]
    assert [c["logicalType"] for c in doc["schema"][0]["properties"]] == ["integer", "integer"]
    # 同じ Contract に取り込むと次の版の下書きになる
    doc["version"] = "1.1.0"
    doc["status"] = "active"
    body = _import(client, ctx["port"]["id"], doc)
    assert body["contract_id"] == ctx["contract"]["id"] and body["version"]["conforms_to"] == "party@1"
    assert body["version"]["quality_sla"]["rule_ids"] == [rule["id"]] and body["version"]["cdm_mappings"] == {"party_id": "order_id"}
    assert any("status は取り込みません" in w for w in body["warnings"])


def test_unsupported_fields_are_warnings_and_never_stored(client, edgf_logs):
    ctx = _ctx(client)
    doc = _doc(servers=[{"server": MARKER}], team=[{"username": MARKER}], price={"priceAmount": 1}, roles=[{"role": "x"}], support=[{"channel": "c"}],
               tenant="t", tags=["a"], description={"purpose": "p", "usage": MARKER, "limitations": MARKER})
    doc["schema"][0]["description"] = MARKER
    doc["schema"][0]["properties"][0].update(examples=[MARKER], primaryKey=True, unique=True)
    doc["schema"][0]["properties"].append({"name": "other", "logicalType": "string", "classification": "secret",
                                           "customProperties": [{"property": "x", "value": MARKER}]})
    doc["slaProperties"].append({"property": "availability", "value": MARKER})
    doc["authoritativeDefinitions"] = [{"type": "glossary", "url": MARKER}]
    doc["customProperties"].append({"property": "vendor.x", "value": MARKER})
    doc["schema"][0]["quality"] = [{"metric": MARKER}]
    body = _import(client, ctx["port"]["id"], doc)
    text = "\n".join(body["warnings"])
    for name in ("servers", "team", "price", "roles", "support", "tenant", "tags", "description.usage", "description.limitations",
                 "schema[0].description", "schema[0].properties[order_id].examples", "schema[0].properties[order_id].primaryKey",
                 "classification", "customProperties.x", "slaProperties[availability]", "authoritativeDefinitions", "customProperties.vendor.x", "quality"):
        assert name in text, name
    assert MARKER not in text
    # 値は DB にもログにも監査にも残らない
    assert MARKER.lower() not in dump_database() and MARKER.lower() not in edgf_logs.text()
    assert MARKER not in client.get(f"{MESH}/contracts/{body['contract_id']}/export", headers=ADMIN, params={"version": "1.2.0"}).text


def test_mismatched_product_and_classification_handling(client):
    ctx = _ctx(client)
    doc = _doc(domain="other", dataProduct="other", description="文字列の説明", version="1.0.0")
    doc["customProperties"] = []
    doc["schema"][0]["properties"][0]["classification"] = "confidential"
    doc["schema"][0]["properties"][1]["classification"] = "pii"
    doc["slaProperties"] = [{"property": "ly", "value": 2, "unit": "d"}]
    body = _import(client, ctx["port"]["id"], doc)
    text = " ".join(body["warnings"])
    assert "domain" in text and "dataProduct" in text and "classification" in text
    v = body["version"]
    assert v["sensitivity"] == "confidential" and v["description"] == "文字列の説明" and v["freshness_sla_hours"] == 48
    assert v["schema"][1]["pii"] is True


def test_multiple_schema_objects_warn(client):
    ctx = _ctx(client)
    doc = _doc()
    doc["schema"].append({"name": "other", "properties": [{"name": "x", "logicalType": "string"}]})
    assert any("2 つ目" in w for w in _import(client, ctx["port"]["id"], doc)["warnings"])


def test_invalid_documents_are_rejected_without_echo(client):
    ctx = _ctx(client)
    pid = ctx["port"]["id"]
    bad = [
        f"key: [{MARKER}", "- not\n- a\n- mapping\n", _doc(kind="Other"), _doc(apiVersion="v2.2.0"), _doc(schema=[]), _doc(schema=[{"name": "x"}]),
        _doc(schema=[{"properties": [{"logicalType": "string"}]}]), _doc(schema=[{"properties": [{"name": "x"}]}]),
        _doc(version="latest"), _doc(customProperties=[]), _doc(customProperties="x"),
        _doc(slaProperties=[{"property": "latency", "value": 1, "unit": "y"}]), _doc(slaProperties=[{"property": "latency", "value": "x"}]),
        _doc(customProperties=[{"property": "edgf.sensitivity", "value": MARKER}]),
    ]
    for doc in bad:
        body = _import(client, pid, doc, code=422)
        assert MARKER not in str(body)
    _import(client, pid, "x: " + "a" * 1_000_001, code=422)
    # 検証は通常の登録と同じ
    _import(client, pid, _doc(customProperties=[{"property": "edgf.sensitivity", "value": "internal"}, {"property": "edgf.purposes", "value": ["none"]}]), code=422)


def test_permissions_and_lookups(client):
    ctx = _ctx(client)
    pid, cid = ctx["port"]["id"], ctx["contract"]["id"]
    _import(client, pid, _doc(), code=403, headers=U("stranger"))
    _import(client, "none", _doc(), code=404)
    _import(client, pid, _doc())
    _import(client, pid, _doc(), code=409)  # 同じ版
    assert client.get(f"{MESH}/contracts/{cid}/export", headers=ADMIN).status_code == 404  # 公開済みの版が無い
    assert client.get(f"{MESH}/contracts/{cid}/export", headers=ADMIN, params={"version": "9.9.9"}).status_code == 404
    assert client.get(f"{MESH}/contracts/{cid}/export", headers=ADMIN, params={"format": "csv"}).status_code == 422
    assert client.get(f"{MESH}/contracts/none/export", headers=ADMIN).status_code == 404
    r = client.get(f"{MESH}/contracts/{cid}/export", headers=U("reader", clearance="internal"), params={"version": "1.2.0"})
    assert r.status_code == 403  # confidential の資産は ABAC で閉じる
    events = client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000}).json()
    assert any(e["action"] == "mesh.contract.import" for e in events)


def test_import_creates_contract_and_never_publishes(client):
    _purpose(client)
    ctx = setup_contract(client)
    a = port_asset(client, "sales.fresh", sensitivity="confidential")
    port = add_port(client, ctx["product"]["id"], a["id"], name="fresh")
    body = _import(client, port["id"], _doc())
    assert client.get(f"{MESH}/contracts/{body['contract_id']}", headers=ADMIN).json()["external_id"] == "urn:contract:orders"
    assert body["version"]["status"] == "draft"
    assert client.get(f"{MESH}/ports/{port['id']}", headers=ADMIN).json()["contract_id"] == body["contract_id"]
