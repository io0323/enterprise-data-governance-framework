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
