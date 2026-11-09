"""Phase 11a (§15 Data Product / Domain Ownership)."""
from __future__ import annotations

import pytest

from tests.mesh_helpers import (ADMIN, API, COMMITTEE, MESH, PO, STEWARD, U, add_port, domain, port_asset, product, term)


@pytest.fixture()
def sales(client):
    domain(client)
    return client


def test_product_requires_domain_data_owner(sales):
    url = f"{MESH}/products"
    assert sales.post(url, headers=STEWARD, json={"name": "x", "domain": "sales", "owner": "stranger"}).status_code == 422
    assert sales.post(url, headers=STEWARD, json={"name": "x", "domain": "none", "owner": "po"}).status_code == 403
    assert sales.post(url, headers=U("a", "owner", "*"), json={"name": "x", "domain": "none", "owner": "po"}).status_code == 422
    # ドメインの資産の Owner も Data Owner
    port_asset(sales, owner="asset-owner")
    assert product(sales, "by-asset-owner", owner="asset-owner")["owner"] == "asset-owner"
    assert product(sales)["status"] == "draft"
    assert sales.post(url, headers=STEWARD, json={"name": "orders", "domain": "sales", "owner": "po"}).status_code == 409


def test_product_permissions(sales):
    body = {"name": "x", "domain": "sales", "owner": "po"}
    for headers in (U("n"), U("o", "owner", "other"), U("c", "custodian", "sales"), ADMIN, COMMITTEE):
        assert sales.post(f"{MESH}/products", headers=headers, json=body).status_code == 403
    assert sales.post(f"{MESH}/products", headers=PO, json=body).status_code == 201
    assert sales.post(f"{MESH}/products", headers=PO, json={**body, "name": "y", "secret": "v"}).status_code == 422


def test_update_and_terms(sales):
    t = term(sales)
    p = product(sales, term_ids=[t])
    assert p["term_ids"] == [t]
    url = f"{MESH}/products/{p['id']}"
    assert sales.patch(url, headers=U("x", "steward", "other"), json={"description": "z"}).status_code == 403
    r = sales.patch(url, headers=STEWARD, json={"description": "新", "term_ids": []})
    assert r.json()["description"] == "新" and r.json()["term_ids"] == []
    assert sales.patch(url, headers=STEWARD, json={"owner": "stranger"}).status_code == 422
    assert sales.patch(url, headers=STEWARD, json={"term_ids": ["none"]}).status_code == 422
    assert sales.get(url, headers=U("anyone")).status_code == 200
    assert sales.get(f"{MESH}/products/none", headers=U("anyone")).status_code == 404
    r = sales.get(f"{MESH}/products", headers=U("anyone"), params={"domain": "sales", "status": "draft", "owner": "po"})
    assert r.headers["X-Total-Count"] == "1"
    events = [e["action"] for e in sales.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000}).json()]
    assert {"mesh.product.create", "mesh.product.update"} <= set(events)


def test_port_rules(sales):
    domain(sales, "other", owner="oo")
    a = port_asset(sales)
    foreign = port_asset(sales, "other.x", domain_name="other")
    p = product(sales)
    port = add_port(sales, p["id"], a["id"])
    assert port["port_type"] == "table" and port["address_uri"] == "warehouse://sales/orders" and port["asset_id"] == a["id"]
    url = f"{MESH}/products/{p['id']}/ports"
    base = {"name": "b", "port_type": "api"}
    assert sales.post(url, headers=STEWARD, json={**base, "asset_id": foreign["id"]}).status_code == 422  # 別ドメイン
    assert sales.post(url, headers=STEWARD, json={**base, "asset_id": "none"}).status_code == 422
    assert sales.post(url, headers=STEWARD, json={**base, "asset_id": a["id"]}).status_code == 409  # 1 対 1
    b = port_asset(sales, "sales.b")
    assert sales.post(url, headers=STEWARD, json={**base, "name": "main", "asset_id": b["id"]}).status_code == 409
    assert sales.post(url, headers=STEWARD, json={**base, "port_type": "kafka", "asset_id": b["id"]}).status_code == 422  # 製品名は不可
    assert sales.post(url, headers=STEWARD, json={**base, "asset_id": b["id"], "address_uri": "data:text/plain,x"}).status_code == 422
    assert sales.post(url, headers=U("n"), json={**base, "asset_id": b["id"]}).status_code == 403
    assert sales.post(f"{MESH}/products/none/ports", headers=STEWARD, json={**base, "asset_id": b["id"]}).status_code == 404

    purl = f"{MESH}/ports/{port['id']}"
    assert sales.patch(purl, headers=U("n"), json={"address_uri": None}).status_code == 403
    assert sales.patch(purl, headers=STEWARD, json={"address_uri": None, "port_type": None}).json()["address_uri"] is None
    assert sales.patch(purl, headers=STEWARD, json={"port_type": "stream"}).json()["port_type"] == "stream"
    assert sales.patch(purl, headers=STEWARD, json={"address_uri": "data:,x"}).status_code == 422
    assert sales.get(f"{MESH}/ports/none", headers=STEWARD).status_code == 404


def test_port_redacted_for_unreadable_asset(sales):
    secret = port_asset(sales, "sales.secret", sensitivity="restricted", owner="so")
    p = product(sales)
    port = add_port(sales, p["id"], secret["id"], name="secret-port")
    reader = U("reader", clearance="confidential")
    seen = sales.get(f"{MESH}/products/{p['id']}", headers=reader).json()["ports"][0]
    assert seen == {"id": port["id"], "product_id": p["id"], "name": "secret-port", "redacted": True, "asset_id": None,
                    "port_type": None, "address_uri": None, "contract_id": None}
    assert sales.get(f"{MESH}/ports/{port['id']}", headers=reader).json()["redacted"] is True
    assert sales.get(f"{MESH}/ports/{port['id']}", headers=U("so")).json()["asset_id"] == secret["id"]
