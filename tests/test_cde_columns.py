"""Phase 16d: CDE のカラム単位化 (cde フラグ、LIN-002、PRV-006、KPI、ODCS)."""
from __future__ import annotations

import uuid

import yaml

from edgf_client import column_lineage_facet, input_field, openlineage_event
from tests.ai_helpers import ADMIN, API, asset, edge, policy_result
from tests.mesh_helpers import MESH, STEWARD, setup_contract
from tests.test_mesh_odcs import _doc, _import, _purpose

ETL = {"X-Actor": "etl", "X-Roles": "custodian", "X-Domains": ""}
SRC = [{"name": "email", "pii": True, "pii_category": "contact", "processing_purposes": ["analytics"], "legal_basis": "contract"},
       {"name": "amount"}]


def status(client, asset_id, policy_id):
    """対象外は結果に出ない (None)。"""
    result = policy_result(client, asset_id, policy_id)
    return None if result is None else result["status"]


def facet(client, output, fields):
    event = openlineage_event("etl", "load", str(uuid.uuid4()), [("crm", "crm.src")], [("dw", output)],
                              column_lineage={output: column_lineage_facet(fields)})
    r = client.post(f"{API}/lineage/openlineage", headers=ETL, json=event)
    assert r.status_code == 202, r.text


def kpi(client) -> dict:
    r = client.get(f"{API}/kpi", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- is_cde の導出
def test_is_cde_is_derived_from_cde_columns(client):
    a = asset(client, "dw.facts", schema=[{"name": "amount", "cde": True}, {"name": "note"}])
    assert a["is_cde"] is True and a["schema"][0]["cde"] is True and a["schema"][1]["cde"] is False
    r = client.post(f"{API}/assets", headers=ADMIN, json={
        "qualified_name": "dw.bad", "name": "x", "data_class": "analytical", "sensitivity": "internal", "is_cde": False,
        "schema": [{"name": "amount", "cde": True}]})
    assert r.status_code == 422 and "is_cde" in r.text
    # 従来どおりデータセット単位の CDE (cde のカラム無し) も残せる
    assert asset(client, "dw.legacy", is_cde=True)["is_cde"] is True


def test_is_cde_follows_schema_updates(client):
    a = asset(client, "dw.facts", schema=[{"name": "amount"}])
    assert a["is_cde"] is False
    r = client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"schema": [{"name": "amount", "cde": True}]})
    assert r.status_code == 200 and r.json()["is_cde"] is True
    r = client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"is_cde": False})
    assert r.status_code == 422
    r = client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"is_cde": False, "schema": [{"name": "amount"}]})
    assert r.status_code == 200 and r.json()["is_cde"] is False


# ---------------------------------------------------------------- LIN-002
def test_lin002_requires_column_lineage_for_cde_columns_with_upstream(client):
    asset(client, "crm.src", schema=SRC)
    source = asset(client, "dw.source_only", schema=[{"name": "amount", "cde": True}])
    target = asset(client, "dw.facts", schema=[{"name": "amount", "cde": True}, {"name": "total", "cde": True}])
    plain = asset(client, "dw.plain", schema=[{"name": "amount"}])
    assert status(client, source["id"], "LIN-002") is None  # 上流が無い
    assert status(client, plain["id"], "LIN-002") is None
    edge(client, "crm.src", "dw.facts")
    result = policy_result(client, target["id"], "LIN-002")
    assert result["status"] == "fail" and "amount" in result["message"] and "total" in result["message"]
    assert kpi(client)["cde_column_lineage_rate"] == 0.0
    facet(client, "dw.facts", {"amount": [input_field("crm", "crm.src", "amount", subtype="IDENTITY")]})
    result = policy_result(client, target["id"], "LIN-002")
    assert result["status"] == "fail" and "amount" not in result["message"] and "total" in result["message"]
    assert kpi(client)["cde_column_lineage_rate"] == 0.5
    facet(client, "dw.facts", {"total": [input_field("crm", "crm.src", "amount", subtype="AGGREGATION")]})
    assert status(client, target["id"], "LIN-002") == "pass"
    assert kpi(client)["cde_column_lineage_rate"] == 1.0


def test_kpi_is_none_without_cde_columns(client):
    asset(client, "dw.plain", schema=[{"name": "amount"}])
    assert kpi(client)["cde_column_lineage_rate"] is None


# ---------------------------------------------------------------- PRV-006
def test_prv006_flags_pii_flowing_into_undeclared_columns(client):
    asset(client, "crm.src", schema=SRC, contains_pii=True, retention_days=30)
    target = asset(client, "dw.facts", schema=[{"name": "contact"}, {"name": "bucket"}, {"name": "amount"}])
    assert status(client, target["id"], "PRV-006") is None  # カラム単位のリネージが無い
    facet(client, "dw.facts", {"bucket": [input_field("crm", "crm.src", "email", type="INDIRECT", subtype="FILTER")],
                               "amount": [input_field("crm", "crm.src", "amount", subtype="IDENTITY")]})
    assert status(client, target["id"], "PRV-006") == "pass"  # INDIRECT は値を運ばない
    # masking=true でも DIRECT の経路は PII を運ぶとみなす
    facet(client, "dw.facts", {"contact": [input_field("crm", "crm.src", "email", subtype="TRANSFORMATION", masking=True)]})
    result = policy_result(client, target["id"], "PRV-006")
    assert result["status"] == "fail" and "contact" in result["message"] and "crm.src.email" in result["message"]
    r = client.patch(f"{API}/assets/{target['id']}", headers=ADMIN, json={"schema": [
        {"name": "contact", "pii": True, "pii_category": "contact", "processing_purposes": ["analytics"], "legal_basis": "contract"},
        {"name": "bucket"}, {"name": "amount"}]})
    assert r.status_code == 200, r.text
    assert status(client, target["id"], "PRV-006") == "pass"


def test_prv006_follows_multiple_hops_and_stops_at_anonymized_assets(client):
    asset(client, "crm.src", schema=SRC, contains_pii=True, retention_days=30)
    asset(client, "dw.mid", schema=[{"name": "contact"}])
    leaf = asset(client, "dw.leaf", schema=[{"name": "c2"}])
    facet(client, "dw.mid", {"contact": [input_field("crm", "crm.src", "email", subtype="IDENTITY")]})
    event = openlineage_event("etl", "leaf", str(uuid.uuid4()), [("dw", "dw.mid")], [("dw", "dw.leaf")],
                              column_lineage={"dw.leaf": column_lineage_facet({"c2": [input_field("dw", "dw.mid", "contact")]})})
    assert client.post(f"{API}/lineage/openlineage", headers=ETL, json=event).status_code == 202
    result = policy_result(client, leaf["id"], "PRV-006")
    assert result["status"] == "fail" and "crm.src.email" in result["message"]
    mid = client.get(f"{API}/assets", headers=ADMIN, params={"q": "dw.mid"}).json()[0]
    r = client.patch(f"{API}/assets/{mid['id']}", headers=ADMIN, json={"privacy_technique": "anonymized", "privacy_params": {"k": 10}})
    assert r.status_code == 200, r.text
    assert status(client, leaf["id"], "PRV-006") == "pass"


# ---------------------------------------------------------------- ODCS
def test_odcs_round_trip_keeps_cde(client):
    _purpose(client)
    ctx = setup_contract(client, sensitivity="confidential")
    source = _doc()
    source["schema"][0]["properties"][0]["criticalDataElement"] = True
    body = _import(client, ctx["port"]["id"], source)
    assert body["warnings"] == [] and body["version"]["schema"][0]["cde"] is True and body["version"]["schema"][1]["cde"] is False
    exported = yaml.safe_load(client.get(f"{MESH}/contracts/{body['contract_id']}/export", headers=ADMIN,
                                         params={"version": "1.2.0"}).text)
    props = exported["schema"][0]["properties"]
    assert props[0]["criticalDataElement"] is True and "criticalDataElement" not in props[1]
    other = setup_contract(client, sensitivity="confidential", qn="sales.second")
    again = _import(client, other["port"]["id"], exported)
    assert again["version"]["schema"] == body["version"]["schema"]


def test_odcs_invalid_cde_value_is_a_warning(client):
    _purpose(client)
    ctx = setup_contract(client, sensitivity="confidential")
    source = _doc()
    source["schema"][0]["properties"][0]["criticalDataElement"] = "yes"
    body = _import(client, ctx["port"]["id"], source, headers=STEWARD)
    assert any("criticalDataElement" in w for w in body["warnings"]) and body["version"]["schema"][0]["cde"] is False
