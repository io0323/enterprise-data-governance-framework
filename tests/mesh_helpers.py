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


def add_port(client, product_id, asset_id, name="main", **kw) -> dict:
    r = client.post(f"{MESH}/products/{product_id}/ports", headers=STEWARD,
                    json={"name": name, "asset_id": asset_id, "port_type": "table", "address_uri": "warehouse://sales/orders", **kw})
    assert r.status_code == 201, r.text
    return r.json()


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
