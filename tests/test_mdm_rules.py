"""Phase 9a (§8): エンティティ定義とルール."""
from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
import yaml

from edgf.services import mdm_rules
from edgf.services.mdm_rules import MdmRuleError, load_entities, parse_entity
from tests.mdm_helpers import ADMIN, COMMITTEE, HUB, MDM, OWNER, STEWARD, U, master_asset

ROOT = Path(__file__).resolve().parent.parent
BASE = {
    "entity_type": "thing", "asset": "m.thing",
    "attributes": [{"name": "a"}, {"name": "b", "identifier_type": "email"}],
    "sources": {"s1": 10, "s2": 5},
    "match": {"exact_keys": [["a"]], "similarity": {"weights": {"b": 1}, "auto_merge": 0.9, "review": 0.7}},
    "survivorship": {"a": {"rule": "source_priority"}, "b": {"rule": "most_recent"}},
}


def variant(mutate):
    doc = copy.deepcopy(BASE)
    mutate(doc)
    return doc


def test_shipped_definitions_are_valid():
    entities = load_entities(ROOT / "policies")
    assert set(entities) == {"party", "product"}
    party = entities["party"]
    assert party.asset == "mdm.party" and party.sources["crm"] == 100
    assert party.exact_keys == (("email",), ("customer_no",))
    assert party.review < party.auto_merge and len(party.rule_hash) == 64
    assert party.attributes["email"].identifier_type == "email"
    assert party.survivorship["segment"] == "most_frequent"


@pytest.mark.parametrize("mutate, match", [
    (lambda d: d.pop("asset"), "asset"),
    (lambda d: d.update(entity_type="Bad Type"), "entity_type"),
    (lambda d: d.update(attributes=[]), "attributes"),
    (lambda d: d["attributes"].append({"name": "a"}), "重複"),
    (lambda d: d["attributes"].append({"name": "Bad"}), "属性名"),
    (lambda d: d["attributes"].append({"name": "c", "identifier_type": "Bad"}), "identifier_type"),
    (lambda d: d.update(sources={}), "sources"),
    (lambda d: d["sources"].update(s3="high"), "整数"),
    (lambda d: d["match"].update(exact_keys=[["nope"]]), "未定義"),
    (lambda d: d["match"].update(exact_keys=["a"]), "リスト"),
    (lambda d: d["match"]["similarity"]["weights"].update(nope=1), "未定義"),
    (lambda d: d["match"]["similarity"].update(weights={"a": 0}), "正の数"),
    (lambda d: d["match"]["similarity"].update(weights={"a": "x"}), "数値"),
    (lambda d: d["match"]["similarity"].update(weights={}), "weights"),
    (lambda d: d["match"]["similarity"].pop("review"), "閾値"),
    (lambda d: d["match"]["similarity"].update(review=0.95), "閾値"),  # 大小関係
    (lambda d: d["match"]["similarity"].update(review=0.9), "閾値"),
    (lambda d: d["match"]["similarity"].update(auto_merge=1.5), "閾値"),
    (lambda d: d["match"]["similarity"].update(similar_floor=1), "similar_floor"),
    (lambda d: d["survivorship"].update(nope={"rule": "most_recent"}), "未定義"),
    (lambda d: d["survivorship"]["a"].update(rule="newest"), "ルール種別"),
    (lambda d: d["survivorship"].pop("b"), "Survivorship"),
])
def test_invalid_definitions_are_rejected(mutate, match):
    with pytest.raises(MdmRuleError, match=match):
        parse_entity(variant(mutate))


def test_duplicate_entity_or_asset_across_files(tmp_path):
    (tmp_path / "mdm").mkdir()
    (tmp_path / "mdm" / "a.yaml").write_text(yaml.safe_dump(BASE), encoding="utf-8")
    (tmp_path / "mdm" / "b.yaml").write_text(yaml.safe_dump(BASE), encoding="utf-8")
    with pytest.raises(MdmRuleError, match="entity_type が重複"):
        load_entities(tmp_path)
    (tmp_path / "mdm" / "b.yaml").write_text(yaml.safe_dump({**BASE, "entity_type": "other"}), encoding="utf-8")
    with pytest.raises(MdmRuleError, match="対応付け済み"):
        load_entities(tmp_path)
    (tmp_path / "mdm" / "b.yaml").write_text("a: [", encoding="utf-8")
    with pytest.raises(MdmRuleError, match="YAML"):
        load_entities(tmp_path)


def test_entities_api_and_reload_permissions(client):
    r = client.get(f"{MDM}/entities", headers=U("analyst"))
    assert r.status_code == 200 and r.headers["x-total-count"] == "2"
    assert {e["entity_type"] for e in r.json()} == {"party", "product"}
    assert all(e["asset_registered"] is False for e in r.json())
    assert client.get(f"{MDM}/entities/nope", headers=U("analyst")).status_code == 404
    for who in (U("x", "steward", "*"), U("c", "custodian", "*"), OWNER):
        r = client.post(f"{MDM}/entities/reload", headers=who)
        assert r.status_code == 403 and r.json()["action"] == "mdm.rule.write"
    for who in (ADMIN, COMMITTEE):
        assert client.post(f"{MDM}/entities/reload", headers=who).json() == {"loaded": 2}


def test_reload_rejects_invalid_definitions_and_keeps_serving(client, tmp_path, monkeypatch):
    shutil.copytree(ROOT / "policies", tmp_path / "policies")
    bad = variant(lambda d: d["match"]["similarity"].update(review=0.99))
    (tmp_path / "policies" / "mdm" / "bad.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")
    monkeypatch.setattr(mdm_rules, "settings", type("S", (), {"policy_dir": tmp_path / "policies"})())
    r = client.post(f"{MDM}/entities/reload", headers=COMMITTEE)
    assert r.status_code == 422 and "閾値" in r.json()["detail"]
    assert client.get(f"{MDM}/entities/party", headers=COMMITTEE).status_code == 200


def test_owner_approval_is_bound_to_rule_hash(client, monkeypatch):
    assert client.post(f"{MDM}/entities/party/approve", headers=OWNER).status_code == 409  # 資産が未登録
    master_asset(client)
    for who in (STEWARD, HUB, ADMIN, COMMITTEE):
        r = client.post(f"{MDM}/entities/party/approve", headers=who)
        assert r.status_code == 403 and r.json()["action"] == "mdm.rule.approve"
    r = client.post(f"{MDM}/entities/party/approve", headers=OWNER)
    assert r.status_code == 200 and r.json()["approved_by"] == "own" and r.json()["asset_registered"] is True
    assert client.post(f"{MDM}/entities/party/approve", headers=OWNER).status_code == 200  # 冪等
    ev = client.get(f"{MDM[:-4]}/audit/events", headers=COMMITTEE, params={"action": "mdm.rule_approve"}).json()
    assert len(ev) == 1 and "rule_hash" in ev[0]["payload"]
    # ルールを変更すると承認は無効になる
    entities = load_entities(ROOT / "policies")
    from dataclasses import replace
    changed = {**entities, "party": replace(entities["party"], rule_hash="0" * 64)}
    monkeypatch.setattr(mdm_rules, "_cache", changed)
    assert client.get(f"{MDM}/entities/party", headers=OWNER).json()["approved_by"] is None


def test_entity_on_non_master_or_unreadable_asset(client):
    client.post(f"{MDM[:-4]}/domains", headers=COMMITTEE, json={"name": "master"})
    client.post(f"{MDM[:-4]}/assets", headers=ADMIN, json={"qualified_name": "mdm.party", "name": "p", "data_class": "operational", "sensitivity": "internal"})
    assert client.get(f"{MDM}/entities/party", headers=U("x")).json()["asset_registered"] is False
    master_asset(client, "mdm.product", sensitivity="restricted")
    assert client.get(f"{MDM}/entities/product", headers=U("x")).status_code == 403
    assert {e["entity_type"] for e in client.get(f"{MDM}/entities", headers=U("x")).json()} == {"party"}
