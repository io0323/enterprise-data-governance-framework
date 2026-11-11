"""Phase 15: SDK から Data Plane 向けの API をすべて呼ぶ (サーバを別プロセスで起動)."""
from __future__ import annotations

import uuid

import pytest

from edgf_client import Forbidden, NotFound, ValidationFailed
from tests.data_plane_e2e.conftest import DATA_PLANE

HASH = "ab" * 32
RAW = "dp.person@example.com"


def ok(response, *codes):
    assert response.status_code in (codes or (200, 201)), response.text
    return response.json()


@pytest.fixture(scope="module")
def world(as_user):
    admin, committee = as_user("root", ["admin"]), as_user("chair", ["committee"])
    steward, owner = as_user("st", ["steward"], ["sales"]), as_user("po", ["owner"], ["sales"])
    for name in ("sales", "master"):
        ok(admin.post("/domains", json={"name": name, "owner": "po"}))
    columns = [{"name": "order_id", "type": "bigint", "nullable": False, "description": "受注 ID"}]

    def asset(qn, **kw):
        body = {"qualified_name": qn, "name": qn, "description": "d", "domain": "sales", "data_class": "transactional", "sensitivity": "internal",
                "owner": "po", "steward": "st", "custodian": DATA_PLANE, **kw}
        return ok(admin.post("/assets", json=body))

    orders = asset("sales.orders", schema=columns)
    customers = asset("crm.customers", sensitivity="confidential", data_class="master", schema=[{"name": "email", "identifier_type": "email"}])
    party = asset("mdm.party", domain="master", data_class="master", sensitivity="confidential")
    source = asset("docs.public", sensitivity="public")
    corpus = asset("ai.rag", data_class="ai", ai_kind="rag_corpus")
    ok(admin.post("/lineage/edges", json={"source": "docs.public", "target": "ai.rag"}))
    ok(owner.post(f"/quality/assets/{orders['id']}/rules", json={"name": "nn", "dimension": "completeness", "rule_type": "not_null", "column": "order_id"}))
    product = ok(steward.post("/mesh/products", json={"name": "orders", "domain": "sales", "owner": "po", "description": "受注"}))
    port = ok(steward.post(f"/mesh/products/{product['id']}/ports", json={"name": "main", "asset_id": orders["id"], "port_type": "table",
                                                                         "address_uri": "warehouse://sales/orders"}))
    contract = ok(steward.post("/mesh/contracts", json={"output_port_id": port["id"]}))
    version = ok(steward.post(f"/mesh/contracts/{contract['id']}/versions", json={"version": "1.0.0", "description": "受注", "sensitivity": "internal",
                                                                                 "schema": columns}))
    ok(owner.post(f"/mesh/contract-versions/{version['id']}/publish"))
    return {"admin": admin, "committee": committee, "orders": orders, "customers": customers, "party": party, "source": source, "corpus": corpus,
            "contract": contract, "columns": columns}


def test_quality_lineage_and_refresh(sdk, world):
    run = sdk.submit_quality_run(world["orders"]["id"], [{"order_id": 1}, {"order_id": None}])
    assert run["record_count"] == 2 and run["score"] < 1
    accepted = sdk.send_openlineage({"eventType": "COMPLETE", "run": {"runId": str(uuid.uuid4())}, "job": {"namespace": "etl", "name": "load_orders"},
                                     "inputs": [{"namespace": "crm", "name": "crm.customers"}], "outputs": [{"namespace": "dw", "name": "sales.orders"}]})
    assert accepted is not None
    refreshed = sdk.notify_refreshed(world["orders"]["id"])
    assert refreshed["last_refreshed_at"]


def test_idempotency_key_prevents_double_runs(sdk, world):
    key = f"dp-{uuid.uuid4()}"
    first = sdk.submit_quality_run(world["orders"]["id"], [{"order_id": 1}], idempotency_key=key)
    again = sdk.submit_quality_run(world["orders"]["id"], [{"order_id": 1}], idempotency_key=key)
    assert first["id"] == again["id"]
    runs = world["admin"].get(f"/quality/assets/{world['orders']['id']}/runs", params={"limit": 100}).json()
    assert [r["id"] for r in runs].count(first["id"]) == 1


def test_contract_check_for_ci(sdk, world):
    same = sdk.check_contract(world["contract"]["id"], {"schema": world["columns"]})
    assert same["baseline_version"] == "1.0.0" and same["breaking_changes"] == []
    dropped = sdk.check_contract(world["contract"]["id"], {"schema": []})
    assert [c["target"] for c in dropped["breaking_changes"]] == ["order_id"] and dropped["required_bump"] == "major"


def test_mdm_match_compose_verify(sdk, world):
    record = {"source_system": "crm", "key": "c1", "attributes": {"email": RAW, "full_name": "顧客一郎"}}
    matched = sdk.mdm_match("party", record)
    golden = matched["golden_id"]
    composed = sdk.mdm_compose("party", golden, [record])
    assert sdk.mdm_verify("party", golden, composed["attributes"])["match"] is True
    assert sdk.mdm_verify("party", golden, {**composed["attributes"], "full_name": "別人"})["match"] is False
    with pytest.raises(ValidationFailed):
        sdk.mdm_match("party", {"source_system": "crm", "attributes": {}})  # key が無い


def test_rag_authorize(sdk, world):
    result = sdk.rag_authorize(world["corpus"]["id"], {"subject": "reader", "roles": ["consumer"]}, [world["source"]["id"]])
    assert result["allowed_asset_ids"] == [world["source"]["id"]]


def test_erasure_tasks_ack_and_complete(sdk, world):
    committee, admin = world["committee"], world["admin"]
    request = ok(admin.post("/privacy/erasure-requests", json={"identifier_type": "email", "identifier": RAW}))
    for action in ("scope", "approve", "dispatch"):
        ok(committee.post(f"/privacy/erasure-requests/{request['id']}/{action}"))
    tasks = [t for t in sdk.erasure_tasks() if t["request_id"] == request["id"]]
    assert tasks
    for task in tasks:
        sdk.ack_erasure_task(task["id"])
        done = sdk.complete_erasure_task(task["id"], records_deleted=1, evidence_hash=HASH)
        assert done["status"] == "completed"


def test_inventory_snapshot(sdk, world):
    result = sdk.push_inventory("warehouse.sales", [
        {"qualified_name": "sales.orders", "kind": "table", "schema": [{k: c[k] for k in ("name", "type", "nullable")} for c in world["columns"]]},
        {"qualified_name": "sales.shadow_table", "kind": "table", "schema": [{"name": "id", "type": "bigint", "nullable": False}]},
    ])
    assert result["unregistered"] == ["sales.shadow_table"] and result["domain"] == "sales"
    with pytest.raises(ValidationFailed):
        sdk.push_inventory("warehouse.sales", [{"qualified_name": "sales.orders", "sample": [{"order_id": 1}]}])
    assert world["admin"].get("/kpi").json()["catalog_registration_rate"] == 0.5


def test_errors_are_typed(sdk, world):
    with pytest.raises(NotFound):
        sdk.notify_refreshed("00000000-0000-0000-0000-000000000000")
    with pytest.raises(Forbidden) as e:
        sdk.request("POST", "/api/v1/domains", json={"name": "x"})
    assert e.value.extensions["action"] == "domain.create" and e.value.request_id
