"""Phase 16b: カラム単位の探索、影響分析、ABAC、Contract の判定の column_impact."""
from __future__ import annotations

import pytest

from edgf.db import SessionLocal
from edgf.services import column_lineage
from tests.process_helpers import ADMIN, API, U, asset

ETL = U("etl", "custodian")
LIN = f"{API}/lineage"


def edge(client, source, source_column, target, target_column=None, type="DIRECT", subtype=None, masking=False):  # noqa: A002
    body = {"source": source, "source_column": source_column, "target": target, "target_column": target_column, "type": type, "subtype": subtype,
            "masking": masking}
    r = client.post(f"{LIN}/column-edges", headers=ETL, json=body)
    assert r.status_code == 201, r.text


@pytest.fixture()
def chain(client):
    """raw.c → stage.c (DIRECT) → mart.total (AGGREGATION)。stage.region → mart.total (INDIRECT GROUP_BY)。
    filter.flag → mart 全体 (データセット単位の INDIRECT)。mart.total → report.t (DIRECT)。"""
    edge(client, "raw.t", "c", "stage.t", "c", subtype="IDENTITY")
    edge(client, "stage.t", "c", "mart.t", "total", subtype="AGGREGATION")
    edge(client, "stage.t", "region", "mart.t", "total", type="INDIRECT", subtype="GROUP_BY")
    edge(client, "filter.t", "flag", "mart.t")
    edge(client, "mart.t", "total", "report.t", "t")
    return client


def test_downstream_marks_direct_and_indirect_paths(chain):
    body = chain.get(f"{LIN}/raw.t/columns/c/downstream", headers=ADMIN).json()
    assert [(e["from"], e["from_column"], e["to"], e["to_column"], e["depth"], e["path"]) for e in body["edges"]] == [
        ("raw.t", "c", "stage.t", "c", 1, "DIRECT"), ("stage.t", "c", "mart.t", "total", 2, "DIRECT"), ("mart.t", "total", "report.t", "t", 3, "DIRECT")]
    indirect = chain.get(f"{LIN}/stage.t/columns/region/downstream", headers=ADMIN).json()["edges"]
    assert [(e["to"], e["to_column"], e["path"]) for e in indirect] == [("mart.t", "total", "INDIRECT"), ("report.t", "t", "INDIRECT")]


def test_dataset_level_input_reaches_every_column(chain):
    body = chain.get(f"{LIN}/filter.t/columns/flag/downstream", headers=ADMIN).json()
    assert [(e["to"], e["to_column"], e["path"]) for e in body["edges"]] == [("mart.t", None, "INDIRECT"), ("report.t", "t", "INDIRECT")]
    up = chain.get(f"{LIN}/mart.t/columns/total/upstream", headers=ADMIN, params={"depth": 1}).json()["edges"]
    assert sorted((e["from"], e["from_column"], e["to_column"]) for e in up) == [("filter.t", "flag", None), ("stage.t", "c", "total"),
                                                                                  ("stage.t", "region", "total")]


def test_depth_limit_and_cycles(client):
    edge(client, "a.t", "x", "b.t", "x")
    edge(client, "b.t", "x", "c.t", "x")
    edge(client, "c.t", "x", "a.t", "x")
    all_edges = client.get(f"{LIN}/a.t/columns/x/downstream", headers=ADMIN, params={"depth": 50}).json()["edges"]
    assert [(e["to"], e["depth"]) for e in all_edges] == [("b.t", 1), ("c.t", 2), ("a.t", 3)]  # 循環しても止まる
    one = client.get(f"{LIN}/a.t/columns/x/downstream", headers=ADMIN, params={"depth": 1}).json()["edges"]
    assert [e["to"] for e in one] == ["b.t"]
    assert client.get(f"{LIN}/a.t/columns/x/downstream", headers=ADMIN, params={"depth": 51}).status_code == 422


def test_direct_path_found_later_upgrades_the_node(client):
    edge(client, "s.t", "a", "m.t", "a", type="INDIRECT")
    edge(client, "s.t", "a", "m.t", "a", type="DIRECT")  # 同じカラムへの DIRECT の辺もある
    with SessionLocal() as db:
        found = column_lineage.impact(db, "s.t", "a")
    assert found["impacted"][0]["path"] == "DIRECT"


def test_impact_reports_owners_and_cde_columns(chain):
    asset(chain, "report.t", owner="report-owner", schema=[{"name": "t", "type": "int", "cde": True}])
    asset(chain, "mart.t", owner="mart-owner", schema=[{"name": "total", "type": "int"}])
    body = chain.get(f"{LIN}/raw.t/columns/c/impact", headers=ADMIN).json()
    assert body["changed_column"] == "c" and body["risk"] == "high" and body["cde_impacted"] is True
    assert body["owners_to_notify"] == ["mart-owner", "report-owner"]
    report = next(i for i in body["impacted"] if i["qualified_name"] == "report.t")
    assert report["cde_column"] is True and report["path"] == "DIRECT" and report["column"] == "t"
    assert chain.get(f"{LIN}/nothing.t/columns/x/impact", headers=ADMIN).json()["risk"] == "none"


def test_column_summary(chain):
    body = chain.get(f"{LIN}/mart.t/columns", headers=ADMIN).json()
    columns = {c["column"]: c for c in body["columns"]}
    assert set(columns) == {None, "total"}
    assert [u["qualified_name"] for u in columns["total"]["upstream"]] == ["stage.t", "stage.t"]
    assert columns[None]["upstream"][0]["column"] == "flag" and columns["total"]["downstream"][0]["qualified_name"] == "report.t"


def test_unreadable_assets_and_columns_are_redacted(client):
    asset(client, "sec.t", sensitivity="restricted", schema=[{"name": "salary", "type": "int"}])
    edge(client, "pub.t", "id", "sec.t", "salary")
    edge(client, "sec.t", "salary", "out.t", "band")
    reader = U("reader", clearance="internal")
    down = client.get(f"{LIN}/pub.t/columns/id/downstream", headers=reader).json()["edges"]
    assert down[0]["to"] == "redacted-1" and down[0]["to_column"] is None and down[0]["job"] is None and down[0]["to_redacted"] is True
    assert down[0]["from_redacted"] is False
    assert down[1]["from"] == "redacted-1" and down[1]["from_column"] is None and down[1]["to"] == "out.t"
    impact = client.get(f"{LIN}/pub.t/columns/id/impact", headers=reader).json()
    assert impact["impacted"][0] == {"qualified_name": "redacted-1", "column": None, "depth": 1, "path": "DIRECT", "registered": True, "redacted": True}
    root = client.get(f"{LIN}/sec.t/columns/salary/upstream", headers=reader).json()
    assert root["asset"] == "redacted-1" and root["column"] is None
    summary = client.get(f"{LIN}/sec.t/columns", headers=reader).json()
    assert summary == {"asset": "redacted-1", "redacted": True, "columns": []}
    for path in (f"{LIN}/pub.t/columns/id/downstream", f"{LIN}/pub.t/columns/id/impact", f"{LIN}/out.t/columns", f"{LIN}/sec.t/columns/salary/upstream"):
        assert "salary" not in client.get(path, headers=reader).text and "sec.t" not in client.get(path, headers=reader).text
    assert "salary" in client.get(f"{LIN}/pub.t/columns/id/downstream", headers=ADMIN).text  # 見える人には見える


def test_contract_check_reports_column_impact(client):
    from tests.mesh_helpers import published, setup_contract
    ctx = setup_contract(client)
    published(client, ctx["contract"]["id"])
    qn = ctx["asset"]["qualified_name"]
    edge(client, qn, "amount", "bi.revenue", "total", subtype="AGGREGATION")
    edge(client, qn, "order_id", "bi.revenue", type="INDIRECT", subtype="JOIN")
    proposal = {"schema": [{"name": "order_id", "type": "string", "nullable": False, "description": "受注 ID"}]}  # amount を削除し、型を変更
    r = client.post(f"{API}/mesh/contracts/{ctx["contract"]["id"]}/check", headers=U("cust"), json=proposal)
    assert r.status_code == 200, r.text
    body = r.json()
    by_column = {c["column"]: c for c in body["column_impact"]}
    assert by_column["amount"]["code"] == "column_removed" and by_column["amount"]["breaking"] is True
    assert [(i["qualified_name"], i["column"], i["path"]) for i in by_column["amount"]["impacted"]] == [("bi.revenue", "total", "DIRECT")]
    assert [(i["qualified_name"], i["column"], i["path"]) for i in by_column["order_id"]["impacted"]] == [("bi.revenue", None, "INDIRECT")]
    assert "impact" in body and "breaking_changes" in body  # 既存の項目は変えない
    from tests.mesh_helpers import COLUMNS
    same = client.post(f"{API}/mesh/contracts/{ctx['contract']['id']}/check", headers=U("cust"), json={"schema": COLUMNS})
    assert same.status_code == 200 and same.json()["column_impact"] == []
