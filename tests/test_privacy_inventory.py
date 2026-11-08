"""Phase 8a (§12 PII): カラム単位の PII Inventory."""
from __future__ import annotations

import pytest

from tests.conftest import H

API = "/api/v1"
COMMITTEE = {"X-Actor": "chair", "X-Roles": "committee"}
ADMIN = {"X-Actor": "root", "X-Roles": "admin"}


def _asset(client, qn, domain="sales", sensitivity="confidential", **kw):
    r = client.post(f"{API}/assets", headers=ADMIN, json={
        "qualified_name": qn, "name": qn, "domain": domain, "data_class": "master", "sensitivity": sensitivity, **kw})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def inv(client):
    for d in ("sales", "hr"):
        client.post(f"{API}/domains", headers=COMMITTEE, json={"name": d})
    _asset(client, "sales.customers", schema=[
        {"name": "customer_id", "pii_category": "identifier", "identifier_type": "customer_id",
         "processing_purposes": ["crm"], "legal_basis": "contract"},
        {"name": "email", "pii_category": "contact", "identifier_type": "email",
         "processing_purposes": ["crm", "marketing"], "legal_basis": "consent"},
        {"name": "segment"}])
    _asset(client, "hr.staff", domain="hr", schema=[{"name": "legacy_flag", "pii": True}])
    _asset(client, "hr.secret", domain="hr", sensitivity="restricted", owner="o1",
           schema=[{"name": "health", "pii_category": "health"}])
    _asset(client, "sales.kpi", sensitivity="internal", schema=[{"name": "value"}])
    return client


def _names(r):
    return [e["qualified_name"] for e in r.json()]


def test_column_metadata_is_backward_compatible_and_infers_pii(client):
    client.post(f"{API}/domains", headers=COMMITTEE, json={"name": "sales"})
    a = _asset(client, "sales.a", schema=[{"name": "x", "pii": True}, {"name": "y", "identifier_type": "email"},
                                          {"name": "z"}])
    assert a["contains_pii"] is True
    cols = {c["name"]: c for c in a["schema"]}
    assert cols["x"]["pii"] and cols["x"]["pii_category"] is None and cols["x"]["processing_purposes"] == []
    assert cols["y"]["pii"] is True  # identifier_type を持つカラムは PII
    assert cols["z"]["pii"] is False
    # 不正な値は 422
    assert client.post(f"{API}/assets", headers=ADMIN, json={
        "qualified_name": "sales.b", "name": "b", "data_class": "master", "sensitivity": "internal",
        "schema": [{"name": "c", "pii_category": "secret"}]}).status_code == 422
    assert client.post(f"{API}/assets", headers=ADMIN, json={
        "qualified_name": "sales.c", "name": "c", "data_class": "master", "sensitivity": "internal",
        "schema": [{"name": "c", "identifier_type": "Bad Type"}]}).status_code == 422


def test_patch_infers_contains_pii_and_cannot_clear_it_while_pii_columns_exist(client):
    client.post(f"{API}/domains", headers=COMMITTEE, json={"name": "sales"})
    a = _asset(client, "sales.a", schema=[{"name": "v"}])
    assert a["contains_pii"] is False
    r = client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"schema": [{"name": "email", "pii": True}]})
    assert r.json()["contains_pii"] is True
    r = client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"contains_pii": False})
    assert r.json()["contains_pii"] is True
    r = client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"schema": [{"name": "v"}], "contains_pii": False})
    assert r.json()["contains_pii"] is False


def test_inventory_lists_only_pii_columns_and_filters(inv):
    r = inv.get(f"{API}/privacy/inventory", headers=COMMITTEE)
    assert _names(r) == ["hr.secret", "hr.staff", "sales.customers"] and r.headers["x-total-count"] == "3"
    cust = r.json()[2]
    assert [c["name"] for c in cust["columns"]] == ["customer_id", "email"]
    assert cust["columns"][1]["processing_purposes"] == ["crm", "marketing"]
    r = inv.get(f"{API}/privacy/inventory", headers=COMMITTEE, params={"purpose": "marketing"})
    assert _names(r) == ["sales.customers"] and [c["name"] for c in r.json()[0]["columns"]] == ["email"]
    assert _names(inv.get(f"{API}/privacy/inventory", headers=COMMITTEE, params={"category": "health"})) == ["hr.secret"]
    assert _names(inv.get(f"{API}/privacy/inventory", headers=COMMITTEE, params={"legal_basis": "contract"})) == ["sales.customers"]
    assert _names(inv.get(f"{API}/privacy/inventory", headers=COMMITTEE, params={"domain": "hr"})) == ["hr.secret", "hr.staff"]
    r = inv.get(f"{API}/privacy/inventory", headers=COMMITTEE, params={"limit": 1})
    assert len(r.json()) == 1 and r.headers["x-total-count"] == "3"
    assert inv.get(f"{API}/privacy/inventory", headers=COMMITTEE, params={"category": "bad"}).status_code == 422


def test_inventory_summary(inv):
    s = inv.get(f"{API}/privacy/inventory/summary", headers=COMMITTEE).json()
    assert s == {"asset_count": 3, "column_count": 4, "undocumented_column_count": 2,
                 "by_category": {"contact": 1, "health": 1, "identifier": 1, "unclassified": 1},
                 "by_purpose": {"crm": 2, "marketing": 1, "unspecified": 2},
                 "by_legal_basis": {"consent": 1, "contract": 1, "unspecified": 2}}


def test_inventory_authorization_and_abac(inv):
    st = {"X-Actor": "st", "X-Roles": "steward", "X-Domains": "sales"}
    assert _names(inv.get(f"{API}/privacy/inventory", headers=st)) == ["sales.customers"]
    hr = {"X-Actor": "o2", "X-Roles": "owner", "X-Domains": "hr"}
    # restricted は owner ロールでも当事者でなければ見えない (ABAC)
    assert _names(inv.get(f"{API}/privacy/inventory", headers=hr)) == ["hr.staff"]
    allst = H("all", "steward", "*")
    assert "hr.secret" not in _names(inv.get(f"{API}/privacy/inventory", headers=allst))
    # client の既定ヘッダ (全ロール) を上書きするため、X-Roles / X-Domains を明示する
    for who in ({"X-Actor": "analyst", "X-Roles": "", "X-Domains": ""},
                {"X-Actor": "c", "X-Roles": "custodian", "X-Domains": "*"},
                {"X-Actor": "st0", "X-Roles": "steward", "X-Domains": ""}):
        r = inv.get(f"{API}/privacy/inventory", headers=who)
        assert r.status_code == 403 and r.json()["action"] == "privacy.inventory.read"
    ev = inv.get(f"{API}/audit/events", headers=COMMITTEE, params={"action": "authz.denied"}).json()[0]
    assert ev["payload"]["action"] == "privacy.inventory.read" and ev["actor"] == "dev:st0"
