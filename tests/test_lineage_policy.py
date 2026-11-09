from datetime import datetime, timedelta

import pytest

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


def _graph_setup(client):
    client.post(f"{API}/domains", json={"name": "sales"}, headers=H())
    _asset(client, "up.raw")
    _asset(client, "mid.cde", is_cde=True)
    _asset(client, "down.secret", sensitivity="restricted", owner="so")
    for s, t in (("up.raw", "mid.cde"), ("mid.cde", "down.secret"), ("mid.cde", "ext.unregistered"), ("down.secret", "down.deep")):
        assert client.post(f"{API}/lineage/edges", headers=H(), json={"source": s, "target": t, "job": "etl"}).status_code == 201


def test_lineage_graph_levels_and_attributes(client):
    _graph_setup(client)
    g = client.get(f"{API}/lineage/mid.cde/graph", headers=H(), params={"depth": 1}).json()
    levels = {n["qualified_name"]: n["level"] for n in g["nodes"]}
    assert levels == {"up.raw": -1, "mid.cde": 0, "down.secret": 1, "ext.unregistered": 1}
    by_name = {n["qualified_name"]: n for n in g["nodes"]}
    assert by_name["mid.cde"]["is_cde"] is True and by_name["ext.unregistered"]["registered"] is False
    deeper = client.get(f"{API}/lineage/mid.cde/graph", headers=H(), params={"depth": 2}).json()
    assert {n["qualified_name"] for n in deeper["nodes"]} == set(levels) | {"down.deep"} and len(deeper["edges"]) == 4
    assert client.get(f"{API}/lineage/mid.cde/graph", headers=H(), params={"depth": 0}).status_code == 422
    assert client.get(f"{API}/lineage/mid.cde/graph", headers=H(), params={"depth": 51}).status_code == 422


def test_lineage_graph_redacts_unreadable_nodes(client):
    _graph_setup(client)
    reader = {"X-Actor": "reader", "X-Roles": "", "X-Domains": "", "X-Clearance": "confidential"}
    g = client.get(f"{API}/lineage/mid.cde/graph", headers=reader, params={"depth": 2}).json()
    hidden = next(n for n in g["nodes"] if n.get("redacted"))
    # 深さだけで、名前、資産の ID、属性は返さない。接するエッジの job も伏せる
    assert hidden == {"qualified_name": "redacted-1", "level": 1, "registered": True, "redacted": True}
    assert "down.secret" not in str(g)
    jobs = {(e["from"], e["to"]): e["job"] for e in g["edges"]}
    assert jobs[("mid.cde", "redacted-1")] is None and jobs[("redacted-1", "down.deep")] is None and jobs[("up.raw", "mid.cde")] == "etl"
    # 当事者には全部返す
    seen = client.get(f"{API}/lineage/mid.cde/graph", headers={"X-Actor": "so"}, params={"depth": 2}).json()
    assert next(n for n in seen["nodes"] if n["qualified_name"] == "down.secret").get("redacted") is None
    # 読めない資産を起点にしても詳細は出ない
    root = client.get(f"{API}/lineage/down.secret/graph", headers=reader).json()
    assert next(n for n in root["nodes"] if n["level"] == 0)["redacted"] is True and root["asset"] == "redacted-1"


def test_lineage_graph_handles_cycles_and_unknown_assets(client):
    client.post(f"{API}/lineage/edges", headers=H(), json={"source": "a", "target": "b"})
    client.post(f"{API}/lineage/edges", headers=H(), json={"source": "b", "target": "a"})
    g = client.get(f"{API}/lineage/a/graph", headers=H()).json()
    assert {n["qualified_name"] for n in g["nodes"]} == {"a", "b"} and len(g["edges"]) == 2
    empty = client.get(f"{API}/lineage/none/graph", headers=H()).json()
    assert [n["qualified_name"] for n in empty["nodes"]] == ["none"] and empty["edges"] == []
    assert client.get(f"{API}/lineage/a/graph").status_code == 200


# ---------------------------------------------------------------- Phase 14-0: ABAC の漏れの修正
READER = {"X-Actor": "reader", "X-Roles": "", "X-Domains": "", "X-Clearance": "internal"}


def _secret_chain(client):
    client.post(f"{API}/domains", json={"name": "sales"}, headers=H())
    _asset(client, "open.src")
    _asset(client, "hidden.payroll", sensitivity="restricted", owner="so")
    _asset(client, "open.dst")
    for s, t in (("open.src", "hidden.payroll"), ("hidden.payroll", "open.dst"), ("open.src", "open.dst")):
        assert client.post(f"{API}/lineage/edges", headers=H(), json={"source": s, "target": t, "job": "secret-job", "transformation": "SELECT salary"}).status_code == 201


@pytest.mark.parametrize("path,direction_key", [("open.src/downstream", "to"), ("open.dst/upstream", "from")])
def test_traversal_hides_names_and_transformation(client, path, direction_key):
    _secret_chain(client)
    r = client.get(f"{API}/lineage/{path}", headers=READER)
    assert r.status_code == 200
    assert "hidden.payroll" not in r.text
    edges = r.json()["edges"]
    # 項目は削らず、値だけを伏せる
    assert all(set(e) == {"from", "to", "job", "transformation", "depth"} for e in edges)
    secret = [e for e in edges if "redacted-1" in (e["from"], e["to"])]
    assert secret and all(e["job"] is None and e["transformation"] is None for e in secret)
    # 閲覧できる資産どうしのエッジはそのまま
    direct = next(e for e in edges if e["from"] == "open.src" and e["to"] == "open.dst")
    assert direct["job"] == "secret-job" and direct["transformation"] == "SELECT salary"


def test_traversal_is_deprecated_with_link_to_graph(client):
    _secret_chain(client)
    r = client.get(f"{API}/lineage/open.src/upstream", headers=READER)
    assert r.headers["Deprecation"] == "true"
    assert r.headers["Link"] == '</api/v1/lineage/open.src/graph>; rel="successor-version"'
    assert client.get(f"{API}/lineage/open.src/graph", headers=READER).headers.get("Deprecation") is None


def test_party_still_sees_full_traversal(client):
    _secret_chain(client)
    body = client.get(f"{API}/lineage/open.src/downstream", headers={"X-Actor": "so"}).json()
    assert {"open.src", "hidden.payroll", "open.dst"} >= {e["to"] for e in body["edges"]} and "hidden.payroll" in str(body)
    assert all(e["transformation"] == "SELECT salary" for e in body["edges"])


def test_requesting_hidden_asset_by_name_does_not_confirm_details(client):
    _secret_chain(client)
    body = client.get(f"{API}/lineage/hidden.payroll/upstream", headers=READER).json()
    assert body["asset"] == "redacted-1" and "hidden.payroll" not in str(body)
    assert client.get(f"{API}/lineage/hidden.payroll/impact", headers=READER).json()["changed_asset"] == "redacted-1"


def test_impact_hides_name_but_keeps_owners_and_risk(client):
    _secret_chain(client)
    body = client.get(f"{API}/lineage/open.src/impact", headers=READER).json()
    assert "hidden.payroll" not in str(body)
    assert "so" in body["owners_to_notify"] and body["risk"] in ("medium", "high")


def test_kpi_domains_excludes_unreadable_and_scopes_counts(client):
    for name in ("sales", "secretdom"):
        assert client.post(f"{API}/domains", json={"name": name}, headers=H()).status_code == 201
    _asset(client, "s.open")
    _asset(client, "s.secret", sensitivity="restricted", owner="so")
    _asset(client, "x.only", domain="secretdom", sensitivity="restricted", owner="so")

    def scopes(headers):
        return {d["scope"]: d for d in client.get(f"{API}/kpi/domains", headers=headers).json()}

    reader = scopes(READER)
    assert set(reader) == {"sales"} and reader["sales"]["asset_count"] == 1  # 閲覧できる資産だけで集計する
    assert client.get(f"{API}/kpi/domains", headers=READER).headers["X-Total-Count"] == "1"
    committee = scopes(H("c", "committee", ""))
    assert set(committee) == {"sales", "secretdom"} and committee["sales"]["asset_count"] == 2
    assert scopes({"X-Actor": "so"})["sales"]["asset_count"] == 2
    # 所属するドメインは、閲覧できる資産が無くても出る (件数は閲覧できる範囲)
    member = scopes({"X-Actor": "m", "X-Roles": "steward", "X-Domains": "secretdom", "X-Clearance": "internal"})
    assert member["secretdom"]["asset_count"] == 0
