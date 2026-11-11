"""Phase 11e (§15 DATSIS): Data Product の出荷基準の自動判定."""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from edgf.config import settings
from edgf.db import engine
from edgf.services import policy
from edgf.services.policy import PolicyLoadError, load_policies
from tests.mesh_helpers import (ADMIN, API, COLUMNS, COMMITTEE, MESH, PO, STEWARD, U, add_port, cdm_asset, contract_body, domain, draft, import_cdm, port_asset, product,
                                purpose, shippable, subscribe)


def _publish(client, pid, headers=PO):
    return client.post(f"{MESH}/products/{pid}/publish", headers=headers)


def _datsis(client, pid):
    r = client.get(f"{MESH}/products/{pid}/datsis", headers=U("anyone"))
    assert r.status_code == 200
    return r.json()


def _assert_blocked_by(client, pid, policy_id):
    """その基準だけが不合格で、公開できないこと。"""
    body = _datsis(client, pid)
    failed = [r["policy_id"] for r in body["results"] if r["status"] == "fail"]
    assert failed == [policy_id], body
    r = _publish(client, pid)
    assert r.status_code == 409 and r.json()["type"].endswith("product-publish-blocked")
    assert r.json()["blockers"][0].startswith(policy_id)
    assert client.get(f"{MESH}/products/{pid}", headers=ADMIN).json()["status"] == "draft"


@pytest.fixture()
def ship(client):
    return {"client": client, **shippable(client)}


def test_all_criteria_pass_then_publish(ship):
    client, pid = ship["client"], ship["pid"]
    body = _datsis(client, pid)
    assert body["publishable"] is True and body["blockers"] == []
    assert [r["policy_id"] for r in body["results"]] == [f"MESH-00{i}" for i in range(1, 8)]
    assert all(r["status"] == "pass" for r in body["results"])
    for who in (STEWARD, COMMITTEE, ADMIN, U("asset-owner")):
        assert _publish(client, pid, who).status_code == 403
    r = _publish(client, pid)
    assert r.json()["status"] == "published" and r.json()["published_by"] == "po"
    assert _publish(client, pid).status_code == 409
    assert _publish(client, "none").status_code == 404
    assert client.get(f"{MESH}/products/none/datsis", headers=ADMIN).status_code == 404
    events = [e["action"] for e in client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000}).json()]
    assert "mesh.product.publish" in events


# ---------------------------------------------------------------- 6 基準のそれぞれ
def test_discoverable_needs_terms(ship):
    client = ship["client"]
    assert client.patch(f"{MESH}/products/{ship['pid']}", headers=STEWARD, json={"term_ids": []}).status_code == 200
    _assert_blocked_by(client, ship["pid"], "MESH-001")


def test_discoverable_needs_same_domain(ship):
    client = ship["client"]
    domain(client, "other", owner="oo")
    assert client.patch(f"{API}/assets/{ship['aid']}", headers=ADMIN, json={"domain": "other"}).status_code == 200
    _assert_blocked_by(client, ship["pid"], "MESH-001")


def test_addressable_needs_uri(ship):
    client = ship["client"]
    assert client.patch(f"{MESH}/ports/{ship['port']['id']}", headers=STEWARD, json={"address_uri": None}).status_code == 200
    _assert_blocked_by(client, ship["pid"], "MESH-002")


def test_trustworthy_needs_certified_asset(ship):
    client = ship["client"]
    assert client.post(f"{API}/assets/{ship['aid']}/certification", headers=U("asset-owner"), json={"certification": "draft"}).status_code == 200
    _assert_blocked_by(client, ship["pid"], "MESH-003")


def test_trustworthy_needs_quality_sla_met(client):
    w = shippable(client, quality_sla={"min_score": 0.5})
    qa = U("qa", "admin,owner,custodian", "*")
    assert client.post(f"{API}/quality/assets/{w['aid']}/runs", headers=qa, json={"records": [{"order_id": None}]}).status_code == 201
    _assert_blocked_by(client, w["pid"], "MESH-003")


def test_trustworthy_needs_declared_sla(client):
    w = shippable(client, quality_sla={})
    _assert_blocked_by(client, w["pid"], "MESH-003")


def test_trustworthy_needs_active_contract(ship):
    client, cid = ship["client"], ship["contract"]["id"]
    with engine.begin() as conn:
        conn.execute(text("UPDATE contract_versions SET status = 'deprecated' WHERE contract_id = :c"), {"c": cid})
    _assert_blocked_by(client, ship["pid"], "MESH-003")


def test_self_describing_needs_column_descriptions(client):
    w = shippable(client, schema=[{**COLUMNS[0], "description": ""}, COLUMNS[1]])
    _assert_blocked_by(client, w["pid"], "MESH-004")


@pytest.mark.parametrize("schema", [
    [{"name": "BadName", "type": "bigint", "description": "d"}],
    [{"name": "order_id", "type": "bigint", "description": "d", "reference_set": "unknown"}],
    [{"name": "order_id", "type": "bigint", "description": "d", "reference_set": "bad@x"}],
    [{"name": "order_id", "type": "bigint", "description": "d", "identifier_type": "email"}],
])
def test_interoperable_standards(client, schema):
    w = shippable(client, schema=schema)
    assert client.patch(f"{API}/assets/{w['aid']}", headers=ADMIN, json={"schema": [
        {"name": "order_id", "type": "bigint", "identifier_type": "order_id", "pii_category": "identifier"}]}).status_code == 200
    failed = [r["policy_id"] for r in _datsis(client, w["pid"])["results"] if r["status"] == "fail"]
    assert "MESH-005" in failed
    assert _publish(client, w["pid"]).status_code == 409


def test_interoperable_accepts_published_reference_data(client):
    ref = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "ref.c", "name": "c", "data_class": "reference", "sensitivity": "public", "owner": "o"}).json()
    writer = U("x", "owner", "*")
    assert client.post(f"{API}/reference-data/sets", headers=writer, json={"id": "country", "asset_id": ref["id"], "name": "c"}).status_code == 201
    assert client.post(f"{API}/reference-data/sets/country/versions", headers=writer, json={"codes": [{"code": "JP", "label": "日本"}]}).status_code == 201
    col = {"name": "country", "type": "string", "description": "d"}
    w = shippable(client, schema=[{**col, "reference_set": "country"}])
    # 下書きしか無い Reference Data は参照できない
    assert "MESH-005" in {r["policy_id"] for r in _datsis(client, w["pid"])["results"] if r["status"] == "fail"}
    assert client.post(f"{API}/reference-data/sets/country/versions/1/publish", headers=COMMITTEE).status_code == 200
    assert "MESH-005" not in {r["policy_id"] for r in _datsis(client, w["pid"])["results"] if r["status"] == "fail"}
    w2 = shippable(client, qn="sales.second", schema=[{**col, "reference_set": "country@1"}])
    assert "MESH-005" not in {r["policy_id"] for r in _datsis(client, w2["pid"])["results"] if r["status"] == "fail"}


def test_interoperable_requires_conformance(client):
    cdm = cdm_asset(client)
    import_cdm(client, cdm["id"])
    w = shippable(client, conforms_to="party@1", cdm_mappings={"party_id": "order_id", "total": "amount"})
    assert "MESH-005" not in {r["policy_id"] for r in _datsis(client, w["pid"])["results"] if r["status"] == "fail"}
    # 公開後に宣言と写しが食い違った場合 (写しの消失など) も不合格になる
    with engine.begin() as conn:
        conn.execute(text("UPDATE contract_versions SET conforms_to = 'party@9' WHERE id = :v"), {"v": w["version"]["id"]})
    _assert_blocked_by(client, w["pid"], "MESH-005")


def test_secure_needs_high_severity_policies(ship):
    client = ship["client"]
    # 証明を残したまま、高重大度のポリシー (SEC-001) に違反させる
    assert client.patch(f"{API}/assets/{ship['aid']}", headers=ADMIN, json={"controls": []}).status_code == 200
    _assert_blocked_by(client, ship["pid"], "MESH-006")


def test_product_without_ports_fails_every_port_criterion(client):
    domain(client)
    p = product(client)
    failed = {r["policy_id"] for r in _datsis(client, p["id"])["results"] if r["status"] == "fail"}
    assert failed == {f"MESH-00{i}" for i in range(1, 8)}


def test_port_without_contract_is_blocked(client):
    domain(client)
    p = product(client)
    add_port(client, p["id"], port_asset(client)["id"])
    failed = {r["policy_id"] for r in _datsis(client, p["id"])["results"] if r["status"] == "fail"}
    assert {"MESH-003", "MESH-004", "MESH-005"} <= failed


# ---------------------------------------------------------------- Domain Policy
@pytest.fixture()
def policy_dir(tmp_path):
    shutil.copy(settings.policy_dir / "global.yaml", tmp_path / "global.yaml")
    (tmp_path / "domains").mkdir()
    return tmp_path


def test_domain_policy_cannot_relax_mesh_criteria(policy_dir):
    (policy_dir / "domains" / "sales.yaml").write_text(
        "scope: domain\ndomain: sales\npolicies:\n"
        "  - {id: MESH-003, name: weaker, target: product, check: {type: mesh_addressable}}\n", encoding="utf-8")
    with pytest.raises(PolicyLoadError):
        load_policies(policy_dir)


def test_domain_policy_can_add_criteria(ship, policy_dir, monkeypatch):
    client, pid = ship["client"], ship["pid"]
    (policy_dir / "domains" / "sales.yaml").write_text(
        "scope: domain\ndomain: sales\npolicies:\n"
        "  - {id: SALES-MESH, name: 追加基準, severity: low, target: product, check: {type: field_in, field: owner, values: [someone-else]}}\n",
        encoding="utf-8")
    monkeypatch.setattr(policy, "_cache", load_policies(policy_dir))
    r = _publish(client, pid)
    assert r.status_code == 409 and r.json()["blockers"][0].startswith("SALES-MESH")
    # 別のドメインには適用されない
    (policy_dir / "domains" / "sales.yaml").write_text(
        "scope: domain\ndomain: other\npolicies:\n"
        "  - {id: OTHER-MESH, name: 追加基準, target: product, check: {type: field_in, field: owner, values: [x]}}\n", encoding="utf-8")
    monkeypatch.setattr(policy, "_cache", load_policies(policy_dir))
    assert _publish(client, pid).status_code == 200


def test_missing_required_policy_fails_closed(ship, monkeypatch):
    """MESH のポリシーが定義から消えても、公開できるようにはならない。"""
    client = ship["client"]
    kept = [p for p in load_policies() if p.id != "MESH-003"]
    monkeypatch.setattr(policy, "_cache", kept)
    r = _publish(client, ship["pid"])
    assert r.status_code == 409 and "MESH-003" in r.json()["blockers"][0]


def test_exception_cannot_relax_mesh_policy(ship):
    client = ship["client"]
    assert client.patch(f"{MESH}/ports/{ship['port']['id']}", headers=STEWARD, json={"address_uri": None}).status_code == 200
    exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    r = client.post(f"{API}/policies/exceptions", headers=U("x", "owner", "sales"), json={
        "policy_id": "MESH-002", "asset_id": ship["aid"], "reason": "移行中", "expires_at": exp})
    assert r.status_code == 201, r.text
    assert client.post(f"{API}/policies/exceptions/{r.json()['id']}/decision", headers=COMMITTEE, json={"approve": True}).status_code == 200
    assert _publish(client, ship["pid"]).status_code == 409


@pytest.mark.parametrize("body", [
    "policies:\n  - {id: X, name: x, target: table, check: {type: glossary_linked}}\n",  # 未知の target
    "policies:\n  - {id: X, name: x, target: product, check: {type: glossary_linked}}\n",  # 資産向けの check
    "policies:\n  - {id: X, name: x, check: {type: mesh_secure}}\n",  # Product 向けの check を資産に
])
def test_target_and_check_type_are_validated_at_load(tmp_path, body):
    (tmp_path / "global.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(PolicyLoadError):
        load_policies(tmp_path)


def test_mesh_policies_do_not_touch_asset_evaluation(ship):
    client = ship["client"]
    results = client.get(f"{API}/policies/evaluate/assets/{ship['aid']}", headers=ADMIN).json()["results"]
    assert not [r for r in results if r["policy_id"].startswith("MESH-")]
    listed = {p["id"]: p["target"] for p in client.get(f"{API}/policies", headers=ADMIN).json()}
    assert listed["MESH-001"] == "product" and listed["GOV-001"] == "asset"


# ---------------------------------------------------------------- ライフサイクル
def test_product_lifecycle(ship):
    client, pid = ship["client"], ship["pid"]
    url = f"{MESH}/products/{pid}"
    assert client.post(f"{url}/deprecate", headers=PO).status_code == 409  # draft
    assert client.post(f"{url}/retire", headers=PO).status_code == 409
    assert _publish(client, pid).status_code == 200
    purpose(client)
    s = subscribe(client, "alice", ship["port"]["id"])
    assert client.post(f"{url}/deprecate", headers=STEWARD).status_code == 403
    assert client.post(f"{url}/deprecate", headers=PO).json()["status"] == "deprecated"
    assert any(i["assignee"] == "alice" for i in client.get(f"{API}/issues", headers=ADMIN, params={"limit": 1000}).json())
    subscribe(client, "bob", ship["port"]["id"], code=409)  # 非推奨には新規登録できない
    assert client.post(f"{url}/retire", headers=PO).status_code == 409  # 利用登録が残っている
    assert client.post(f"{MESH}/subscriptions/{s['id']}/cancel", headers=U("alice")).status_code == 200
    assert client.post(f"{url}/retire", headers=STEWARD).status_code == 403
    assert client.post(f"{url}/retire", headers=PO).json()["status"] == "retired"
    # retired は変更できない
    assert client.patch(url, headers=STEWARD, json={"description": "x"}).status_code == 409
    b = port_asset(client, "sales.late")
    assert client.post(f"{url}/ports", headers=STEWARD, json={"name": "late", "asset_id": b["id"], "port_type": "file"}).status_code == 409
    cid = ship["contract"]["id"]
    assert client.post(f"{MESH}/contracts/{cid}/versions", headers=STEWARD, json=contract_body("2.0.0")).status_code == 409
    with engine.begin() as conn:  # 廃止の前に作られた下書きも公開できない
        conn.execute(text("UPDATE data_products SET status = 'draft' WHERE id = :id"), {"id": pid})
    pending = draft(client, cid, "2.0.0")
    with engine.begin() as conn:
        conn.execute(text("UPDATE data_products SET status = 'retired' WHERE id = :id"), {"id": pid})
    assert client.post(f"{MESH}/contract-versions/{pending['id']}/publish", headers=PO).status_code == 409
