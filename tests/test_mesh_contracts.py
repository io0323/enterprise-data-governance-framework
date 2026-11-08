"""Phase 11b (§15 Standardization): Data Contract と互換性の判定."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from edgf.db import engine
from edgf.services import mesh_compat
from edgf.services.mesh_compat import COMPAT_RULES, Bump, compare, type_change
from tests.mesh_helpers import (ADMIN, API, COLUMNS, COMMITTEE, CUSTODIAN, MESH, PO, STEWARD, U, contract_body, domain, draft,
                                publish, published, setup_contract)

BASE = {"schema_": [{"name": "id", "type": "bigint", "nullable": False, "pii": False, "description": "ID"},
                    {"name": "amount", "type": "int", "nullable": True, "pii": False, "description": "金額"},
                    {"name": "email", "type": "string", "nullable": True, "pii": True, "description": "連絡先"}],
        "quality_sla": {"min_score": 0.9, "rule_ids": ["r1"]}, "freshness_sla_hours": 24, "sensitivity": "confidential",
        "purposes": ["a", "b"], "conforms_to": "party@1", "description": "d"}


def _col(name):
    return next(c for c in BASE["schema_"] if c["name"] == name)


def _with_col(name, **kw):
    return {"schema_": [{**c, **kw} if c["name"] == name else c for c in BASE["schema_"]]}


def _without(name):
    return {"schema_": [c for c in BASE["schema_"] if c["name"] != name]}


def _plus(**col):
    return {"schema_": [*BASE["schema_"], {"pii": False, "description": "", **col}]}


# ---------------------------------------------------------------- 判定表
@pytest.mark.parametrize("code,proposal", [
    ("column_removed", _without("amount")),
    ("column_renamed", {"schema_": [{**c, "name": "total"} if c["name"] == "amount" else c for c in BASE["schema_"]]}),
    ("column_added_required", _plus(name="must", type="int", nullable=False)),
    ("type_incompatible", _with_col("amount", type="string")),
    ("type_incompatible", _with_col("id", type="int")),  # 縮小
    ("nullable_relaxed", _with_col("id", nullable=True)),
    ("sensitivity_raised", {"sensitivity": "restricted"}),
    ("pii_added", _with_col("amount", pii=True)),
    ("pii_added", _plus(name="tel", type="string", nullable=True, pii=True)),
    ("quality_sla_lowered", {"quality_sla": {"min_score": 0.8, "rule_ids": ["r1"]}}),
    ("quality_sla_lowered", {"quality_sla": {"min_score": None, "rule_ids": ["r1"]}}),
    ("quality_sla_lowered", {"quality_sla": {"min_score": 0.9, "rule_ids": []}}),
    ("freshness_sla_lowered", {"freshness_sla_hours": 48}),
    ("freshness_sla_lowered", {"freshness_sla_hours": None}),
    ("usage_narrowed", {"purposes": ["a"]}),
    ("conforms_to_changed", {"conforms_to": None}),
    ("conforms_to_changed", {"conforms_to": "party@2"}),
])
def test_breaking_changes(code, proposal):
    changes = compare(BASE, {**BASE, **proposal})
    assert code in {c["code"] for c in changes}
    assert next(c for c in changes if c["code"] == code)["breaking"] is True
    assert mesh_compat.required_bump(changes) == Bump.MAJOR


@pytest.mark.parametrize("code,proposal,bump", [
    ("column_added_nullable", _plus(name="note", type="string", nullable=True), Bump.MINOR),
    ("type_widened", _with_col("amount", type="bigint"), Bump.MINOR),
    ("nullable_tightened", _with_col("amount", nullable=False), Bump.MINOR),
    ("sensitivity_lowered", {"sensitivity": "internal"}, Bump.MINOR),
    ("pii_removed", _with_col("email", pii=False), Bump.MINOR),
    ("quality_sla_raised", {"quality_sla": {"min_score": 0.95, "rule_ids": ["r1"]}}, Bump.MINOR),
    ("quality_sla_raised", {"quality_sla": {"min_score": 0.9, "rule_ids": ["r1", "r2"]}}, Bump.MINOR),
    ("freshness_sla_raised", {"freshness_sla_hours": 12}, Bump.MINOR),
    ("usage_widened", {"purposes": ["a", "b", "c"]}, Bump.MINOR),
    ("column_reference_changed", _with_col("id", identifier_type="order_id"), Bump.MINOR),
    ("description_changed", _with_col("amount", description="合計金額"), Bump.PATCH),
    ("description_changed", {"description": "新しい説明"}, Bump.PATCH),
])
def test_non_breaking_changes(code, proposal, bump):
    changes = compare(BASE, {**BASE, **proposal})
    assert [c["code"] for c in changes] == [code]
    assert changes[0]["breaking"] is False and mesh_compat.required_bump(changes) == bump


def test_additions_to_empty_sla_and_cdm_are_minor():
    empty = {**BASE, "quality_sla": {}, "freshness_sla_hours": None, "conforms_to": None}
    changes = compare(empty, BASE)
    assert {c["code"] for c in changes} == {"quality_sla_raised", "freshness_sla_raised", "conforms_to_added"}
    assert mesh_compat.required_bump(changes) == Bump.MINOR


def test_no_change_is_patch_and_rename_can_be_explicit():
    assert compare(BASE, BASE) == [] and mesh_compat.required_bump([]) == Bump.PATCH
    # 型が変わる名前の変更は renamed_from で明示できる。明示が無ければ削除と追加になる
    new = {**BASE, "schema_": [c for c in BASE["schema_"] if c["name"] != "amount"]
           + [{"name": "total", "type": "bigint", "nullable": True, "pii": False, "description": "金額", "renamed_from": "amount"}]}
    codes = {c["code"] for c in compare(BASE, new)}
    assert {"column_renamed", "type_widened"} <= codes and "column_removed" not in codes
    implicit = {**new, "schema_": [{k: v for k, v in c.items() if k != "renamed_from"} for c in new["schema_"]]}
    assert {"column_removed", "column_added_nullable"} <= {c["code"] for c in compare(BASE, implicit)}


@pytest.mark.parametrize("old,new,expected", [
    ("int", "bigint", "type_widened"), ("INTEGER", "int", None), ("smallint", "bigint", "type_widened"),
    ("bigint", "int", "type_incompatible"), ("float", "double", "type_widened"), ("int", "double", "type_widened"),
    ("bigint", "double", "type_incompatible"), ("date", "timestamp", "type_widened"), ("timestamp", "date", "type_incompatible"),
    ("varchar(10)", "varchar(20)", "type_widened"), ("varchar(20)", "varchar(10)", "type_incompatible"),
    ("varchar(10)", "text", "type_widened"), ("varchar(10)", "varchar", "type_widened"), ("varchar", "varchar(10)", "type_incompatible"),
    ("decimal(10,2)", "decimal(12,2)", "type_widened"), ("decimal(10,2)", "decimal(10)", "type_incompatible"),
    ("decimal(10,2)", "numeric(10,2)", None), ("int", "string", "type_incompatible"), ("array<int>", "array<int>", None),
    ("array<int>", "array<bigint>", "type_incompatible"),
])
def test_type_table(old, new, expected):
    assert type_change(old, new) == expected


def test_every_rule_is_declared_with_a_label():
    assert all(isinstance(bump, Bump) and label for bump, label in COMPAT_RULES.values())


def test_version_helpers():
    assert mesh_compat.actual_bump((1, 2, 3), (1, 2, 3)) is None and mesh_compat.actual_bump((1, 2, 3), (1, 1, 9)) is None
    assert mesh_compat.actual_bump((1, 2, 3), (2, 0, 0)) == Bump.MAJOR and mesh_compat.actual_bump((1, 2, 3), (1, 3, 0)) == Bump.MINOR
    assert mesh_compat.actual_bump((1, 2, 3), (1, 2, 4)) == Bump.PATCH
    assert mesh_compat.next_version((1, 2, 3), Bump.MAJOR) == "2.0.0" and mesh_compat.next_version((1, 2, 3), Bump.MINOR) == "1.3.0"
    assert mesh_compat.next_version((1, 2, 3), Bump.PATCH) == "1.2.4"


# ---------------------------------------------------------------- Contract の版
@pytest.fixture()
def ctx(client):
    return {"client": client, **setup_contract(client)}


def test_contract_creation(ctx):
    client, c = ctx["client"], ctx["contract"]
    assert client.post(f"{MESH}/contracts", headers=STEWARD, json={"output_port_id": ctx["port"]["id"]}).status_code == 409
    assert client.post(f"{MESH}/contracts", headers=U("n"), json={"output_port_id": ctx["port"]["id"]}).status_code == 403
    assert client.post(f"{MESH}/contracts", headers=STEWARD, json={"output_port_id": "none"}).status_code == 404
    assert client.get(f"{MESH}/ports/{ctx['port']['id']}", headers=ADMIN).json()["contract_id"] == c["id"]
    assert client.get(f"{MESH}/contracts/{c['id']}", headers=ADMIN).json()["output_port_id"] == ctx["port"]["id"]
    assert client.get(f"{MESH}/contracts/none", headers=ADMIN).status_code == 404
    assert client.get(f"{MESH}/contracts", headers=ADMIN, params={"product_id": ctx["product"]["id"]}).headers["X-Total-Count"] == "1"


def test_draft_validation(ctx):
    client, c = ctx["client"], ctx["contract"]
    url = f"{MESH}/contracts/{c['id']}/versions"
    assert client.post(url, headers=U("n"), json=contract_body()).status_code == 403
    assert client.post(url, headers=STEWARD, json=contract_body("v1")).status_code == 422
    assert client.post(url, headers=STEWARD, json=contract_body("1.0")).status_code == 422
    assert client.post(url, headers=STEWARD, json=contract_body(purposes=["none"])).status_code == 422
    assert client.post(url, headers=STEWARD, json=contract_body(quality_sla={"rule_ids": ["none"]})).status_code == 422
    assert client.post(url, headers=STEWARD, json=contract_body(quality_sla={"min_score": 2})).status_code == 422
    assert client.post(url, headers=STEWARD, json=contract_body(schema=[])).status_code == 422
    assert client.post(url, headers=STEWARD, json=contract_body(schema=[COLUMNS[0], COLUMNS[0]])).status_code == 422
    assert client.post(url, headers=STEWARD, json=contract_body(samples=["x"])).status_code == 422
    assert client.post(url, headers=STEWARD, json=contract_body(schema=[{**COLUMNS[0], "example": "x"}])).status_code == 422
    assert client.post(url, headers=STEWARD, json=contract_body(conforms_to="no-version")).status_code == 422
    v = draft(client, c["id"])
    assert v["status"] == "draft" and v["major"] == 1 and v["schema"][0]["name"] == "order_id"
    assert client.post(url, headers=STEWARD, json=contract_body()).status_code == 409


def test_published_version_is_immutable(ctx):
    client, c = ctx["client"], ctx["contract"]
    v = draft(client, c["id"])
    url = f"{MESH}/contract-versions/{v['id']}"
    assert client.put(url, headers=U("n"), json=contract_body()).status_code == 403
    r = client.put(url, headers=STEWARD, json=contract_body("1.0.1", description="修正"))
    assert r.json()["version"] == "1.0.1" and r.json()["description"] == "修正"
    draft(client, c["id"], "1.5.0")
    assert client.put(url, headers=STEWARD, json=contract_body("1.5.0")).status_code == 409
    assert client.put(url, headers=STEWARD, json=contract_body("1.0.1", purposes=["none"])).status_code == 422
    done = publish(client, v["id"])
    assert done["status"] == "published" and done["published_by"] == "po" and done["published_at"]
    assert client.put(url, headers=STEWARD, json=contract_body("1.0.1", description="改ざん")).status_code == 409
    publish(client, v["id"], code=409)
    assert client.get(url, headers=ADMIN).json()["description"] == "修正"
    assert client.get(f"{MESH}/contract-versions/none", headers=ADMIN).status_code == 404
    assert client.put(f"{MESH}/contract-versions/none", headers=STEWARD, json=contract_body()).status_code == 404
    assert client.get(f"{MESH}/contracts/{c['id']}/versions", headers=ADMIN).headers["X-Total-Count"] == "2"


def test_publish_requires_product_owner(ctx):
    client, c = ctx["client"], ctx["contract"]
    v = draft(client, c["id"])
    for headers in (STEWARD, CUSTODIAN, COMMITTEE, ADMIN, U("asset-owner"), U("x", "owner", "sales")):
        publish(client, v["id"], code=403, headers=headers)
    publish(client, v["id"])
    denied = client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000}).json()
    assert any(e["action"] == "authz.denied" for e in denied)
    assert client.post(f"{MESH}/contract-versions/none/publish", headers=PO).status_code == 404


def test_breaking_change_with_minor_is_rejected_and_recorded(ctx):
    client, c = ctx["client"], ctx["contract"]
    published(client, c["id"])
    v = draft(client, c["id"], "1.1.0", schema=[COLUMNS[0]])  # amount の削除
    r = client.post(f"{MESH}/contract-versions/{v['id']}/publish", headers=PO)
    assert r.status_code == 409 and r.json()["type"].endswith("contract-version-bump-required")
    assert r.json()["required_bump"] == "major" and r.json()["actual_bump"] == "minor"
    assert r.json()["changes"][0]["code"] == "column_removed"
    assert client.get(f"{MESH}/contract-versions/{v['id']}", headers=ADMIN).json()["status"] == "draft"
    events = client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000}).json()
    rejected = [e for e in events if e["action"] == "mesh.contract.publish_rejected"]
    assert len(rejected) == 1 and rejected[0]["payload"]["breaking"] == ["column_removed"] and rejected[0]["payload"]["domain"] == "sales"


def test_version_must_advance_and_match_required_bump(ctx):
    client, c = ctx["client"], ctx["contract"]
    published(client, c["id"], "1.2.0")
    publish(client, draft(client, c["id"], "1.1.0")["id"], code=409)  # 古い
    # 非破壊でも、追加は minor が必要
    added = [*COLUMNS, {"name": "note", "type": "string", "nullable": True, "description": "備考"}]
    r = client.post(f"{MESH}/contract-versions/{draft(client, c['id'], '1.2.1', schema=added)['id']}/publish", headers=PO)
    assert r.status_code == 409 and r.json()["required_bump"] == "minor"
    assert published(client, c["id"], "1.3.0", schema=added)["status"] == "published"
    assert published(client, c["id"], "1.3.1", schema=added, description="説明だけ")["status"] == "published"
    # 破壊的ではない拒否は KPI の破壊的変更に数えられるよう breaking が空になる
    events = client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000}).json()
    assert [e["payload"]["breaking"] for e in events if e["action"] == "mesh.contract.publish_rejected"] == [[]]


def test_contract_sensitivity_cannot_be_below_asset(client):
    ctx = setup_contract(client, sensitivity="confidential")
    v = draft(client, ctx["contract"]["id"], sensitivity="internal")
    publish(client, v["id"], code=422)
    assert published(client, ctx["contract"]["id"], "1.0.1", sensitivity="confidential")["status"] == "published"


# ---------------------------------------------------------------- check API
def test_check_reports_changes_and_required_bump(ctx):
    client, c, a = ctx["client"], ctx["contract"], ctx["asset"]
    url = f"{MESH}/contracts/{c['id']}/check"
    r = client.post(url, headers=U("cust"), json={"schema": COLUMNS, "sensitivity": "internal"})
    assert r.status_code == 200 and r.json()["baseline_version"] is None and r.json()["required_bump"] is None
    assert r.json()["suggested_version"] == "1.0.0" and r.json()["allowed"] is True
    published(client, c["id"], "1.0.0")
    body = client.post(url, headers=U("cust"), json={"schema": [COLUMNS[0]], "version": "1.1.0"}).json()
    assert body["required_bump"] == "major" and body["suggested_version"] == "2.0.0" and body["allowed"] is False
    assert [x["code"] for x in body["breaking_changes"]] == ["column_removed"] and body["baseline_version"] == "1.0.0"
    assert client.post(url, headers=U("cust"), json={"schema": [COLUMNS[0]], "version": "2.0.0"}).json()["allowed"] is True
    ok = client.post(url, headers=U("cust"), json={"freshness_sla_hours": 12}).json()
    assert ok["required_bump"] == "minor" and ok["breaking_changes"] == [] and ok["allowed"] is None
    # ステートレス: 版は増えず、監査にも残らない
    assert client.get(f"{MESH}/contracts/{c['id']}/versions", headers=ADMIN).headers["X-Total-Count"] == "1"
    events = client.get(f"{API}/audit/events", headers=ADMIN, params={"entity_id": a["id"], "limit": 1000}).json()
    assert not [e for e in events if "check" in e["action"]]


def test_check_permissions_and_payload_limits(ctx):
    client, c = ctx["client"], ctx["contract"]
    url = f"{MESH}/contracts/{c['id']}/check"
    for headers, code in ((U("cust"), 200), (U("asset-owner"), 200), (U("asset-steward"), 200), (U("stranger"), 403), (PO, 403), (COMMITTEE, 403)):
        assert client.post(url, headers=headers, json={}).status_code == code
    assert client.post(url, headers=U("cust"), json={"sample_rows": ["x"]}).status_code == 422
    assert client.post(f"{MESH}/contracts/none/check", headers=U("cust"), json={}).status_code == 404


def test_check_impact_matches_impact_analysis(ctx):
    client, c, a = ctx["client"], ctx["contract"], ctx["asset"]
    domain(client, "bi", owner="bi-owner")
    for qn, owner in (("bi.mart", "mart-owner"), ("bi.report", "report-owner")):
        r = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": qn, "name": qn, "domain": "bi", "data_class": "analytical",
                                                              "sensitivity": "internal", "owner": owner})
        assert r.status_code == 201
    for s, t in ((a["qualified_name"], "bi.mart"), ("bi.mart", "bi.report"), ("bi.report", "ext.unregistered")):
        assert client.post(f"{API}/lineage/edges", headers=ADMIN, json={"source": s, "target": t}).status_code == 201
    impact = client.post(f"{MESH}/contracts/{c['id']}/check", headers=U("cust"), json={"schema": [COLUMNS[0]]}).json()["impact"]
    expected = client.get(f"{API}/lineage/{a['qualified_name']}/impact", headers=U("cust")).json()
    assert impact == expected
    assert impact["owners_to_notify"] == ["mart-owner", "report-owner"]
    assert impact["unregistered_downstream"] == ["ext.unregistered"]


def test_check_reports_quality_sla(ctx):
    client, c, a = ctx["client"], ctx["contract"], ctx["asset"]
    url = f"{MESH}/contracts/{c['id']}/check"
    q = client.post(url, headers=U("cust"), json={}).json()["quality"]
    assert q == {"declared": False, "ok": False, "score": None, "breaches": []}
    q = client.post(url, headers=U("cust"), json={"quality_sla": {"min_score": 0.9}}).json()["quality"]
    assert q["ok"] is False and "実績" in q["breaches"][0]
    qa = U("qa", "admin,owner,custodian", "*")
    rule = client.post(f"{API}/quality/assets/{a['id']}/rules", headers=qa, json={
        "name": "nn", "dimension": "completeness", "rule_type": "not_null", "column": "order_id"}).json()
    assert client.post(f"{API}/quality/assets/{a['id']}/runs", headers=qa, json={"records": [{"order_id": 1}, {"order_id": None}]}).status_code == 201
    q = client.post(url, headers=U("cust"), json={"quality_sla": {"min_score": 0.9, "rule_ids": [rule["id"]]}}).json()["quality"]
    assert q["ok"] is False and len(q["breaches"]) == 2 and q["score"] == 0.5
    q = client.post(url, headers=U("cust"), json={"quality_sla": {"min_score": 0.5}}).json()["quality"]
    assert q["ok"] is True
    q = client.post(url, headers=U("cust"), json={"quality_sla": {"rule_ids": ["unknown-rule"]}}).json()["quality"]
    assert q["ok"] is False


# ---------------------------------------------------------------- スキーマのずれ
def _drift_issues(client, contract_id):
    rows = client.get(f"{API}/issues", headers=ADMIN, params={"limit": 1000}).json()
    return [i for i in rows if i["source_ref"] == f"mesh-drift:{contract_id}"]


def test_schema_drift_opens_issue_and_catalog_update_succeeds(ctx):
    client, c, a = ctx["client"], ctx["contract"], ctx["asset"]
    published(client, c["id"])
    assert _drift_issues(client, c["id"]) == []
    # 説明だけの違いはずれではない
    same = [{"name": "order_id", "type": "bigint", "nullable": False, "description": "別の説明"},
            {"name": "amount", "type": "int", "nullable": True, "description": "金額"}]
    assert client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"schema": same}).status_code == 200
    assert _drift_issues(client, c["id"]) == []
    r = client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"schema": [same[0]]})
    assert r.status_code == 200 and [x["name"] for x in r.json()["schema"]] == ["order_id"]  # 更新そのものは成功する
    issues = _drift_issues(client, c["id"])
    assert len(issues) == 1 and issues[0]["assignee"] == "po" and "column_removed(amount)" in issues[0]["description"]
    assert issues[0]["asset_id"] == a["id"]
    # 重複起票しない
    assert client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"schema": [{**same[0], "type": "string"}]}).status_code == 200
    assert len(_drift_issues(client, c["id"])) == 1
    events = client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000}).json()
    assert [e for e in events if e["action"] == "mesh.contract.drift"]


@pytest.mark.parametrize("change", [
    {"schema": [{"name": "order_id", "type": "string", "nullable": False}, {"name": "amount", "type": "int", "nullable": True}]},
    {"schema": [{"name": "order_id", "type": "bigint", "nullable": True}, {"name": "amount", "type": "int", "nullable": True}]},
    {"schema": [{"name": "order_id", "type": "bigint"}, {"name": "amount", "type": "int", "pii": True}]},
    {"schema": [{"name": "order_id", "type": "bigint"}, {"name": "amount", "type": "int"}, {"name": "extra", "type": "int"}]},
    {"sensitivity": "restricted"},
])
def test_drift_patterns(ctx, change):
    client, c, a = ctx["client"], ctx["contract"], ctx["asset"]
    published(client, c["id"])
    assert client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json=change).status_code == 200
    assert len(_drift_issues(client, c["id"])) == 1


def test_catalog_without_nullable_is_not_drift_and_unrelated_asset_is_ignored(ctx):
    client, c, a = ctx["client"], ctx["contract"], ctx["asset"]
    published(client, c["id"])
    r = client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"schema": [{"name": "order_id", "type": "bigint"}, {"name": "amount", "type": "int"}]})
    assert r.status_code == 200 and _drift_issues(client, c["id"]) == []
    # Contract の無い資産、下書きだけの Contract ではずれを見ない
    other = setup_contract(client, qn="sales.other")
    draft(client, other["contract"]["id"])
    assert client.patch(f"{API}/assets/{other['asset']['id']}", headers=ADMIN, json={"schema": []}).status_code == 200
    assert _drift_issues(client, other["contract"]["id"]) == []


def test_publish_with_existing_drift_opens_issue(ctx):
    client, c, a = ctx["client"], ctx["contract"], ctx["asset"]
    assert client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"schema": []}).status_code == 200
    published(client, c["id"])
    assert len(_drift_issues(client, c["id"])) == 1


def test_contract_reads_follow_abac(client):
    ctx = setup_contract(client, sensitivity="restricted")
    c = ctx["contract"]
    v = draft(client, c["id"], sensitivity="restricted")
    reader = U("reader", clearance="confidential")
    for url in (f"{MESH}/contracts/{c['id']}", f"{MESH}/contracts/{c['id']}/versions", f"{MESH}/contract-versions/{v['id']}"):
        assert client.get(url, headers=reader).status_code == 403
    assert client.get(f"{MESH}/contracts", headers=reader).json() == []
    assert client.get(f"{MESH}/contract-versions/{v['id']}", headers=U("asset-owner")).status_code == 200


# ---------------------------------------------------------------- 並行実行 (実 DB のみ)
@pytest.mark.skipif(engine.dialect.name == "sqlite", reason="インメモリの SQLite は 1 接続を共有するため並行実行できない")
def test_concurrent_publish_cannot_skip_compatibility_check(ctx):
    """同時に公開しても、片方は公開後の版と比べて判定される (破壊的な変更がすり抜けない)。"""
    client, c = ctx["client"], ctx["contract"]
    published(client, c["id"])
    versions = [draft(client, c["id"], f"1.0.{i}", description=f"説明{i}")["id"] for i in range(1, 5)]
    barrier = threading.Barrier(len(versions))

    def go(version_id):
        barrier.wait()
        return client.post(f"{MESH}/contract-versions/{version_id}/publish", headers=PO).status_code

    with ThreadPoolExecutor(len(versions)) as pool:
        codes = list(pool.map(go, versions))
    rows = [v for v in client.get(f"{MESH}/contracts/{c['id']}/versions", headers=ADMIN).json() if v["status"] == "published"]
    assert 200 in codes and set(codes) <= {200, 409}
    assert len(rows) == 1 + codes.count(200)
