"""Phase 15b: 資産一覧の取り込み、差分の検出、スキーマの反映、Catalog 登録率."""
from __future__ import annotations

import pytest

from edgf.services import discovery_rules, kpi
from edgf.services.discovery import merged_schema
from tests.process_helpers import ADMIN, API, COMMITTEE, U, asset, issues_of

SNAP = f"{API}/discovery/snapshots"
DP = U("dp-sales", "custodian", "sales")  # sales ドメインの Data Plane のサービスの主体
COLS = [{"name": "order_id", "type": "bigint", "nullable": False}, {"name": "amount", "type": "int", "nullable": True}]


def push(client, source, assets, who=DP, code=201):
    r = client.post(SNAP, headers=who, json={"source": source, "assets": assets})
    assert r.status_code == code, r.text
    return r.json()


def item(name, schema=None, **kw):
    return {"qualified_name": name, "schema": schema if schema is not None else COLS, **kw}


@pytest.fixture()
def sales(client):
    for name in ("sales", "hr"):
        client.post(f"{API}/domains", headers=ADMIN, json={"name": name})
    return client


def catalog_asset(client, qn, **kw):
    return asset(client, qn, domain="sales", schema=[{**c, "description": "説明"} for c in COLS], **kw)


# ---------------------------------------------------------------- 差分
def test_first_snapshot_detects_unregistered_assets(sales):
    catalog_asset(sales, "sales.orders")
    result = push(sales, "warehouse.sales", [item("sales.orders"), item("sales.shadow")])
    assert result["added"] == ["sales.orders", "sales.shadow"] and result["unregistered"] == ["sales.shadow"] and result["missing"] == []
    opened = issues_of(sales, "discovery-unregistered:sales.shadow")
    assert len(opened) == 1 and opened[0]["assignee"] == "sales-steward" and opened[0]["kind"] == "discovery"
    # 再送しても重複して起票しない
    again = push(sales, "warehouse.sales", [item("sales.orders"), item("sales.shadow")])
    assert again["added"] == [] and again["unregistered"] == ["sales.shadow"]
    assert len(issues_of(sales, "discovery-unregistered:sales.shadow")) == 1


def test_missing_and_changed_are_per_source(sales):
    catalog_asset(sales, "sales.orders")
    push(sales, "warehouse.sales", [item("sales.orders"), item("sales.gone")])
    push(sales, "warehouse.sales.eu", [item("sales.eu_orders")])
    # warehouse.sales だけを送る。送られてこない warehouse.sales.eu の資産は消失にしない
    result = push(sales, "warehouse.sales", [item("sales.orders", [*COLS, {"name": "note", "type": "string", "nullable": True}])])
    assert result["missing"] == ["sales.gone"] and result["changed"] == ["sales.orders"]
    assets = {a["qualified_name"]: a for a in sales.get(f"{API}/discovery/assets", headers=ADMIN).json()}
    assert assets["sales.gone"]["status"] == "missing" and assets["sales.eu_orders"]["status"] == "present"
    # 消えた資産が戻れば added
    back = push(sales, "warehouse.sales", [item("sales.orders"), item("sales.gone")])
    assert back["added"] == ["sales.gone"] and back["missing"] == []


def test_missing_catalog_asset_opens_issue_for_its_steward(sales):
    catalog_asset(sales, "sales.orders")
    push(sales, "warehouse.sales", [item("sales.orders")])
    push(sales, "warehouse.sales", [])
    opened = issues_of(sales, "discovery-missing:warehouse.sales")
    assert len(opened) == 1 and opened[0]["assignee"] == "asset-steward"
    push(sales, "warehouse.sales", [])
    assert len(issues_of(sales, "discovery-missing:warehouse.sales")) == 1


# ---------------------------------------------------------------- 入力
@pytest.mark.parametrize("bad", [
    {"qualified_name": "sales.x", "sample": [{"email": "zz@example.com"}]},
    {"qualified_name": "sales.x", "rows": [[1, 2]]},
    {"qualified_name": "sales.x", "statistics": {"row_count": 3}},
    {"qualified_name": "sales.x", "schema": [{"name": "a", "type": "int", "min": 1, "max": 9}]},
    {"qualified_name": "sales.x", "schema": [{"name": "a", "type": "int", "values": ["x"]}]},
    {"qualified_name": "sales.x", "schema": [{"name": "a", "type": "int", "top_values": ["x"]}]},
    {"qualified_name": "sales.x", "unknown_field": 1},
])
def test_values_samples_and_statistics_are_rejected(sales, bad):
    r = sales.post(SNAP, headers=DP, json={"source": "warehouse.sales", "assets": [bad]})
    assert r.status_code == 422 and r.json()["type"] == "urn:edgf:problem:validation-error"
    assert "zz@example.com" not in r.text and "[1, 2]" not in r.text
    assert sales.get(f"{API}/discovery/assets", headers=ADMIN).json() == []


def test_snapshot_level_extra_fields_and_duplicates(sales):
    assert sales.post(SNAP, headers=DP, json={"source": "warehouse.sales", "assets": [], "data": [1]}).status_code == 422
    assert sales.post(SNAP, headers=DP, json={"source": "warehouse.sales", "assets": [item("a.b"), item("a.b")]}).status_code == 422
    assert sales.post(SNAP, headers=DP, json={"source": "bad source!", "assets": []}).status_code == 422


def test_unknown_source_and_authorization(sales):
    r = sales.post(SNAP, headers=DP, json={"source": "elsewhere.db", "assets": []})
    assert r.status_code == 422 and r.json()["type"] == "urn:edgf:problem:discovery-source-unknown"
    # 別ドメインの custodian、ドメイン外の custodian、consumer は送れない
    for who in (U("dp-hr", "custodian", "hr"), U("x", "custodian", ""), U("reader")):
        assert sales.post(SNAP, headers=who, json={"source": "warehouse.sales", "assets": []}).status_code == 403
    assert push(sales, "lake.hr", [], who=U("dp-hr", "custodian", "hr"))["domain"] == "hr"
    assert push(sales, "warehouse.sales", [], who=ADMIN)["domain"] == "sales"


def test_read_scope(sales):
    push(sales, "warehouse.sales", [item("sales.a")])
    push(sales, "lake.hr", [item("hr.a")], who=ADMIN)
    names = lambda who: [a["qualified_name"] for a in sales.get(f"{API}/discovery/assets", headers=who).json()]  # noqa: E731
    assert names(COMMITTEE) == ["hr.a", "sales.a"] and names(U("own", "owner", "sales")) == ["sales.a"]
    assert sales.get(f"{API}/discovery/assets", headers=U("reader")).status_code == 403
    assert sales.get(f"{API}/discovery/assets", headers=DP).status_code == 403  # 送れても読めない
    sources = sales.get(f"{API}/discovery/sources", headers=ADMIN).json()
    assert [(s["namespace"], s["present_count"]) for s in sources] == [("lake.hr", 1), ("warehouse.sales", 1)]
    assert [a["qualified_name"] for a in sales.get(f"{API}/discovery/assets", headers=ADMIN, params={"registered": "false", "namespace": "lake.hr"}).json()] == ["hr.a"]


# ---------------------------------------------------------------- スキーマの反映
def test_non_breaking_change_is_applied_and_resets_certification(sales):
    a = catalog_asset(sales, "sales.orders")
    from edgf.db import SessionLocal
    from edgf.models import DataAsset
    with SessionLocal() as db:
        db.get(DataAsset, a["id"]).certification = "certified"
        db.commit()
    result = push(sales, "warehouse.sales", [item("sales.orders", [*COLS, {"name": "note", "type": "string", "nullable": True}])])
    assert result["schema_applied"] == ["sales.orders"] and result["schema_pending"] == []
    after = sales.get(f"{API}/assets/{a['id']}", headers=ADMIN).json()
    assert [c["name"] for c in after["schema"]] == ["order_id", "amount", "note"]
    assert after["schema"][0]["description"] == "説明"  # 業務のメタデータは残す
    assert after["certification"] == "draft"
    events = sales.get(f"{API}/audit/events", headers=ADMIN, params={"entity_id": a["id"], "action": "asset.update"}).json()
    assert events[0]["actor"] == "system:discovery" and events[0]["payload"]["certification_reset"] is True


def test_breaking_change_is_held_and_applied_by_steward(sales):
    a = catalog_asset(sales, "sales.orders")
    result = push(sales, "warehouse.sales", [item("sales.orders", [COLS[0]])])  # amount を削除
    assert result["schema_pending"] == ["sales.orders"] and result["schema_applied"] == []
    assert len(sales.get(f"{API}/assets/{a['id']}", headers=ADMIN).json()["schema"]) == 2  # 反映しない
    opened = issues_of(sales, "discovery-breaking")
    assert len(opened) == 1 and opened[0]["severity"] == "high"
    proposals = sales.get(f"{API}/discovery/schema-changes", headers=ADMIN).json()
    assert len(proposals) == 1 and proposals[0]["changes"][0]["code"] == "column_removed"
    # 同じ案の再送では作り直さない
    push(sales, "warehouse.sales", [item("sales.orders", [COLS[0]])])
    assert len(sales.get(f"{API}/discovery/schema-changes", headers=ADMIN).json()) == 1
    pid = proposals[0]["id"]
    # 承認行為: Custodian と無関係の利用者は反映できない
    for who in (U("asset-custodian", "custodian", "sales"), U("reader"), U("other-owner", "owner", "sales")):
        assert sales.post(f"{API}/discovery/schema-changes/{pid}/apply", headers=who).status_code == 403
    applied = sales.post(f"{API}/discovery/schema-changes/{pid}/apply", headers=U("asset-steward"))
    assert applied.status_code == 200 and applied.json()["status"] == "applied" and applied.json()["decided_by"] == "dev:asset-steward"
    assert [c["name"] for c in sales.get(f"{API}/assets/{a['id']}", headers=ADMIN).json()["schema"]] == ["order_id"]
    assert issues_of(sales, "discovery-breaking")[0]["status"] == "resolved"
    assert sales.post(f"{API}/discovery/schema-changes/{pid}/apply", headers=U("asset-steward")).status_code == 409
    assert sales.post(f"{API}/discovery/schema-changes/none/apply", headers=ADMIN).status_code == 404


def test_new_breaking_proposal_supersedes_and_compatible_state_clears(sales):
    catalog_asset(sales, "sales.orders")
    push(sales, "warehouse.sales", [item("sales.orders", [COLS[0]])])
    push(sales, "warehouse.sales", [item("sales.orders", [{"name": "order_id", "type": "string", "nullable": False}, COLS[1]])])
    statuses = sorted(p["status"] for p in sales.get(f"{API}/discovery/schema-changes", headers=ADMIN, params={"status": ""}).json())
    assert statuses == ["pending", "superseded"]
    push(sales, "warehouse.sales", [item("sales.orders")])  # 元に戻った
    assert sales.get(f"{API}/discovery/schema-changes", headers=ADMIN).json() == []


def test_asset_without_schema_in_snapshot_is_not_changed(sales):
    a = catalog_asset(sales, "sales.orders")
    result = push(sales, "warehouse.sales", [item("sales.orders", [])])
    assert result["schema_applied"] == result["schema_pending"] == []
    assert len(sales.get(f"{API}/assets/{a['id']}", headers=ADMIN).json()["schema"]) == 2


def test_merged_schema_keeps_business_metadata():
    catalog = [{"name": "a", "type": "int", "description": "d", "pii": True}]
    assert merged_schema(catalog, [{"name": "a", "type": "bigint", "nullable": True}, {"name": "b", "type": "int"}]) == [
        {"name": "a", "type": "bigint", "description": "d", "pii": True, "nullable": True}, {"name": "b", "type": "int"}]


# ---------------------------------------------------------------- Catalog 登録率と成熟度
def test_registration_rate_enterprise_and_domain(sales):
    for qn in ("sales.a", "sales.b", "sales.c"):
        catalog_asset(sales, qn)
    push(sales, "warehouse.sales", [item("sales.a"), item("sales.b"), item("sales.c"), item("sales.x")])
    push(sales, "lake.hr", [item("hr.a")], who=ADMIN)
    k = sales.get(f"{API}/kpi", headers=ADMIN).json()
    assert k["discovered_asset_count"] == 5 and k["unregistered_asset_count"] == 2 and k["catalog_registration_rate"] == 0.6
    by_domain = {d["scope"]: d for d in sales.get(f"{API}/kpi/domains", headers=ADMIN).json()}
    assert by_domain["sales"]["catalog_registration_rate"] == 0.75 and by_domain["hr"]["catalog_registration_rate"] == 0.0
    # 消失した資産は分母から外す
    push(sales, "warehouse.sales", [item("sales.a"), item("sales.b"), item("sales.c")])
    assert sales.get(f"{API}/kpi", headers=ADMIN).json()["catalog_registration_rate"] == 0.75


def test_registration_rate_is_none_without_inventory(sales):
    k = sales.get(f"{API}/kpi", headers=ADMIN).json()
    assert k["catalog_registration_rate"] is None and k["discovered_asset_count"] == 0


def test_level_three_now_requires_catalog_registration():
    """メタデータ完備率による代替を廃止した。Catalog 登録率が 95% に届かないと Level 3 にならない。"""
    base = {"owner_assignment_rate": 1, "quality_score_avg": 1, "compliance_rate": 1, "metadata_completeness": 1}
    assert kpi.maturity_indicator(base)["indicative_level"] == 2
    assert kpi.maturity_indicator({**base, "catalog_registration_rate": 0.94})["indicative_level"] == 2
    assert kpi.maturity_indicator({**base, "catalog_registration_rate": 0.95})["indicative_level"] == 3
    conditions = kpi.maturity_indicator(base)["next_level"]["conditions"]
    assert {"metric": "catalog_registration_rate", "minimum": 0.95, "value": None, "met": False} in conditions


# ---------------------------------------------------------------- 対応表
def test_rules_are_validated(tmp_path):
    (tmp_path / "a.yaml").write_text("version: 1\nsources:\n  - {namespace: 'x.*', domain: d, steward: s}\n", encoding="utf-8")
    assert discovery_rules.load_rules(tmp_path)[0].namespace == "x.*"
    for body in ("version: 2\nsources: []\n", "version: 1\nsources:\n  - {namespace: x, domain: d}\n", "::: not yaml", "version: 1\nsources: x\n"):
        (tmp_path / "a.yaml").write_text(body, encoding="utf-8")
        with pytest.raises(discovery_rules.DiscoveryRulesError):
            discovery_rules.load_rules(tmp_path)


def test_first_matching_rule_wins():
    assert discovery_rules.rule_for("warehouse.sales").steward == "sales-steward"
    assert discovery_rules.rule_for("warehouse.sales.eu").domain == "sales"
    assert discovery_rules.rule_for("warehouse.other") is None
