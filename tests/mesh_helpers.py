"""Phase 11 のテストの共通部品。"""
from __future__ import annotations

from tests.ai_helpers import ADMIN, API, COMMITTEE, CONTROLS, U  # noqa: F401

MESH = f"{API}/mesh"
PO = U("po", "owner", "sales")          # Product の owner
STEWARD = U("st", "steward", "sales")   # ドメインの Steward
CUSTODIAN = U("cust", "custodian", "sales")


def domain(client, name="sales", owner="po") -> None:
    assert client.post(f"{API}/domains", headers=ADMIN, json={"name": name, "owner": owner}).status_code == 201


def term(client, name="売上") -> str:
    r = client.post(f"{API}/glossary/terms", headers=ADMIN, json={"name": name, "definition": "d"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def port_asset(client, qn="sales.orders_pub", *, sensitivity="internal", owner="asset-owner", domain_name="sales", **kw) -> dict:
    r = client.post(f"{API}/assets", headers=ADMIN, json={
        "qualified_name": qn, "name": qn, "description": "d", "domain": domain_name, "data_class": "transactional",
        "sensitivity": sensitivity, "owner": owner, "steward": "asset-steward", "custodian": "cust", "controls": CONTROLS, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def product(client, name="orders", *, domain_name="sales", owner="po", **kw) -> dict:
    r = client.post(f"{MESH}/products", headers=U("st", "steward", domain_name),
                    json={"name": name, "domain": domain_name, "owner": owner, "description": "受注の Data Product", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def add_port(client, product_id, asset_id, name="main", reachable=True, **kw) -> dict:
    r = client.post(f"{MESH}/products/{product_id}/ports", headers=STEWARD,
                    json={"name": name, "asset_id": asset_id, "port_type": "table", "address_uri": "warehouse://sales/orders", **kw})
    assert r.status_code == 201, r.text
    port = r.json()
    if reachable:  # MESH-007 (Phase 15): Data Plane がポートの到達を報告する。資産の Custodian として送る
        custodian = client.get(f"{API}/assets/{asset_id}", headers=ADMIN).json()["custodian"]
        reported = client.post(f"{MESH}/ports/{port['id']}/reachability", headers=U(custodian), json={"reachable": True})
        assert reported.status_code == 201, reported.text
    return port


COLUMNS = [{"name": "order_id", "type": "bigint", "nullable": False, "description": "受注 ID"},
           {"name": "amount", "type": "int", "nullable": True, "description": "金額"}]


def contract_body(version="1.0.0", **kw) -> dict:
    return {"version": version, "description": "受注の契約", "schema": COLUMNS, "sensitivity": "internal", **kw}


def contract(client, port_id) -> dict:
    r = client.post(f"{MESH}/contracts", headers=STEWARD, json={"output_port_id": port_id})
    assert r.status_code == 201, r.text
    return r.json()


def draft(client, contract_id, version="1.0.0", **kw) -> dict:
    r = client.post(f"{MESH}/contracts/{contract_id}/versions", headers=STEWARD, json=contract_body(version, **kw))
    assert r.status_code == 201, r.text
    return r.json()


def publish(client, version_id, code=200, headers=PO):
    r = client.post(f"{MESH}/contract-versions/{version_id}/publish", headers=headers)
    assert r.status_code == code, r.text
    return r.json()


def published(client, contract_id, version="1.0.0", **kw) -> dict:
    return publish(client, draft(client, contract_id, version, **kw)["id"])


def setup_contract(client, *, sensitivity="internal", qn="sales.orders_pub", **asset_kw) -> dict:
    """ドメイン、Output Port の資産 (Contract と同じスキーマ)、Product、Port、Contract を用意する。"""
    if client.get(f"{API}/domains", headers=ADMIN).headers["X-Total-Count"] == "0":
        domain(client)
    schema = [{"name": c["name"], "type": c["type"], "nullable": c["nullable"], "description": c["description"]} for c in COLUMNS]
    a = port_asset(client, qn, sensitivity=sensitivity, schema=schema, **asset_kw)
    p = product(client, "product-" + qn.replace(".", "-"))
    port = add_port(client, p["id"], a["id"])
    return {"asset": a, "product": p, "port": port, "contract": contract(client, port["id"])}


def mark_published(product_id) -> None:
    """DATSIS の判定は tests/test_mesh_datsis.py で確認する。ここでは状態だけを公開済みにする。"""
    from sqlalchemy import text

    from edgf.db import engine
    with engine.begin() as conn:
        conn.execute(text("UPDATE data_products SET status = 'published' WHERE id = :id"), {"id": product_id})


def purpose(client, purpose_id="analytics") -> str:
    r = client.post(f"{API}/privacy/purposes", headers=COMMITTEE, json={"id": purpose_id, "description": "d", "default_legal_basis": "contract"})
    assert r.status_code == 201, r.text
    return purpose_id


def subscribe(client, subscriber, port_id, purpose_id="analytics", code=201, **kw):
    r = client.post(f"{MESH}/subscriptions", headers=U(subscriber), json={"output_port_id": port_id, "purpose_id": purpose_id, **kw})
    assert r.status_code == code, r.text
    return r.json()


PARTY_SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                "properties": {"party_id": {"type": "integer"}, "total": {"type": ["number", "null"]}},
                "required": ["party_id"]}


def cdm_asset(client, qn="cdm.party") -> dict:
    r = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": qn, "name": qn, "data_class": "metadata",
                                                          "sensitivity": "public", "owner": "cdm-owner"})
    assert r.status_code == 201, r.text
    return r.json()


def import_cdm(client, asset_id, entity="party", version="1", schema=None, code=201, headers=COMMITTEE):
    r = client.post(f"{MESH}/cdm/imports", headers=headers, json={
        "entity": entity, "asset_id": asset_id, "version": version, "json_schema": PARTY_SCHEMA if schema is None else schema})
    assert r.status_code == code, r.text
    return r.json()


def shippable(client, *, qn="sales.orders_pub", **version_kw) -> dict:
    """DATSIS の全基準に合格する Data Product (公開前) を用意する。各テストはここから 1 基準だけ崩す。"""
    t = term(client, f"用語-{qn}")
    ctx = setup_contract(client, qn=qn)
    pid = ctx["product"]["id"]
    assert client.patch(f"{MESH}/products/{pid}", headers=STEWARD, json={"term_ids": [t]}).status_code == 200
    qa = U("qa", "admin,owner,custodian", "*")
    aid = ctx["asset"]["id"]
    assert client.post(f"{API}/quality/assets/{aid}/rules", headers=qa, json={
        "name": "nn", "dimension": "completeness", "rule_type": "not_null", "column": "order_id"}).status_code == 201
    assert client.post(f"{API}/quality/assets/{aid}/runs", headers=qa, json={"records": [{"order_id": 1}]}).status_code == 201
    assert client.post(f"{API}/assets/{aid}/lifecycle", headers=ADMIN, json={"target_state": "active"}).status_code == 200
    r = client.post(f"{API}/assets/{aid}/certification", headers=U("asset-owner"), json={"certification": "certified"})
    assert r.status_code == 200, r.text
    version = published(client, ctx["contract"]["id"], **{"quality_sla": {"min_score": 0.9}, **version_kw})
    return {**ctx, "version": version, "pid": pid, "aid": aid, "term": t}
