from datetime import datetime, timedelta

from tests.conftest import H

API = "/api/v1"


def _asset(client, qn, **kw):
    body = {"qualified_name": qn, "name": qn, "data_class": "analytical", "sensitivity": "internal",
            "owner": "o", "steward": "s", "description": "d", "domain": "sales",
            "controls": ["encryption_at_rest", "encryption_in_transit"], **kw}
    r = client.post(f"{API}/assets", headers=H(), json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_openlineage_and_impact(client, sales_asset):
    _asset(client, "sales.daily_revenue", owner="finance-owner", is_cde=True)
    ev = {"eventType": "COMPLETE", "run": {"runId": "r1"}, "job": {"namespace": "etl", "name": "daily_agg"},
          "inputs": [{"name": "sales.orders"}], "outputs": [{"name": "sales.daily_revenue"}]}
    assert client.post(f"{API}/lineage/openlineage", headers=H(), json=ev).json()["edges"] == 1
    client.post(f"{API}/lineage/edges", headers=H(), json={"source": "sales.daily_revenue", "target": "ai.forecast_features"})
    client.post(f"{API}/lineage/edges", headers=H(), json={"source": "ai.forecast_features", "target": "sales.orders"})  # 循環
    imp = client.get(f"{API}/lineage/sales.orders/impact").json()
    assert imp["risk"] == "high"
    assert imp["owners_to_notify"] == ["finance-owner", "owner1"]
    assert imp["unregistered_downstream"] == ["ai.forecast_features"]
    up = client.get(f"{API}/lineage/ai.forecast_features/upstream", params={"depth": 2}).json()
    assert {e["from"] for e in up["edges"]} == {"sales.daily_revenue", "sales.orders"}


def test_policy_evaluation_and_exception(client, sales_asset):
    a = _asset(client, "sales.report")  # analytical: lineage なし → LIN-001 違反
    res = client.get(f"{API}/policies/evaluate/assets/{a['id']}").json()
    assert not res["compliant"]
    assert [r["policy_id"] for r in res["results"] if r["status"] == "fail"] == ["LIN-001"]
    exp = (datetime.utcnow() + timedelta(days=30)).isoformat()
    ex = client.post(f"{API}/policies/exceptions", headers=H("req"), json={
        "policy_id": "LIN-001", "asset_id": a["id"], "reason": "移行中", "expires_at": exp}).json()
    assert client.post(f"{API}/policies/exceptions/{ex['id']}/decision", headers=H("req"), json={"approve": True}).status_code == 403
    client.post(f"{API}/policies/exceptions/{ex['id']}/decision", headers=H("committee"), json={"approve": True})
    assert client.get(f"{API}/policies/evaluate/assets/{a['id']}").json()["compliant"]
    too_long = (datetime.utcnow() + timedelta(days=400)).isoformat()
    assert client.post(f"{API}/policies/exceptions", headers=H(), json={
        "policy_id": "LIN-001", "asset_id": a["id"], "reason": "x", "expires_at": too_long}).status_code == 422


def test_pii_and_ai_policies(client):
    client.post(f"{API}/domains", json={"name": "sales"}, headers=H())
    a = _asset(client, "ai.train_set", data_class="ai", contains_pii=True, sensitivity="internal")
    fails = {r["policy_id"] for r in client.get(f"{API}/policies/evaluate/assets/{a['id']}").json()["results"] if r["status"] == "fail"}
    assert {"PRV-001", "PRV-002", "AI-001", "LIN-001"} <= fails


def test_evaluate_all_opens_issues(client, sales_asset):
    rep = client.post(f"{API}/policies/evaluate", params={"open_issues": True}, headers=H()).json()
    assert rep["issues_opened"] >= 1 and 0 < rep["compliance_rate"] < 1
    again = client.post(f"{API}/policies/evaluate", params={"open_issues": True}, headers=H()).json()
    assert again["issues_opened"] == 0


def test_certification_gate(client, sales_asset):
    aid = sales_asset["id"]
    chk = client.get(f"{API}/assets/{aid}/certification/check").json()
    assert not chk["certifiable"]
    client.post(f"{API}/assets/{aid}/lifecycle", headers=H(), json={"target_state": "active"})
    client.post(f"{API}/quality/assets/{aid}/rules", headers=H(), json={
        "name": "id必須", "dimension": "completeness", "rule_type": "not_null", "column": "order_id"})
    client.post(f"{API}/quality/assets/{aid}/runs", headers=H(), json={"records": [{"order_id": "1"}]})
    assert client.post(f"{API}/assets/{aid}/certification", headers=H("stranger"), json={"certification": "certified"}).status_code == 403
    r = client.post(f"{API}/assets/{aid}/certification", headers=H("owner1"), json={"certification": "certified"})
    assert r.status_code == 200 and r.json()["certification"] == "certified"
    # スキーマ変更で再認証が必要になる
    r = client.patch(f"{API}/assets/{aid}", headers=H(), json={"schema": [{"name": "order_id"}]})
    assert r.json()["certification"] == "draft"


def test_domain_policy_cannot_override_global(tmp_path):
    import pytest
    from edgf.services.policy import PolicyLoadError, load_policies
    (tmp_path / "domains").mkdir()
    (tmp_path / "global.yaml").write_text("policies:\n  - {id: GOV-001, name: a, check: {type: glossary_linked}}\n")
    (tmp_path / "domains" / "x.yaml").write_text("scope: domain\ndomain: x\npolicies:\n  - {id: GOV-001, name: weaker, check: {type: glossary_linked}}\n")
    with pytest.raises(PolicyLoadError):
        load_policies(tmp_path)


def test_audit_chain_and_kpi(client, sales_asset):
    assert client.get(f"{API}/audit/verify").json()["valid"]
    from edgf.db import engine
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("UPDATE audit_events SET actor='mallory' WHERE seq=1"))
    v = client.get(f"{API}/audit/verify").json()
    assert v == {"valid": False, "events_checked": 0, "broken_at_seq": 1}
    k = client.get(f"{API}/kpi").json()
    assert k["asset_count"] == 1 and k["owner_assignment_rate"] == 1.0
    assert k["maturity"]["indicative_level"] >= 1
    assert client.get(f"{API}/kpi/domains").json()[0]["scope"] == "sales"
