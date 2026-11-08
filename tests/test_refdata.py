"""Phase 9e (§8 Reference Data): 版管理、公開の承認、差分、品質ルールからの参照."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from edgf.db import engine
from tests.mdm_helpers import ADMIN, API, COMMITTEE, U

RD = f"{API}/reference-data"
STEWARD = U("rd-steward", "steward", "ref")
OWNER = U("rd-owner")


def ref_asset(client, qn="ref.country", **kw):
    client.post(f"{API}/domains", headers=COMMITTEE, json={"name": "ref"})
    r = client.post(f"{API}/assets", headers=ADMIN, json={
        "qualified_name": qn, "name": qn, "domain": "ref", "data_class": "reference", "sensitivity": "public",
        "owner": "rd-owner", "steward": "rd-steward", "description": "d", **kw})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def codes(*pairs, **extra):
    return [{"code": c, "label": l, **extra} for c, l in pairs]


V1 = codes(("JP", "日本"), ("US", "米国"), ("DE", "西ドイツ"), standard="ISO 3166-1", standard_version="2025")
V2 = codes(("JP", "日本"), ("US", "アメリカ合衆国"), ("FR", "フランス"), standard="ISO 3166-1", standard_version="2026")


@pytest.fixture()
def rset(client):
    aid = ref_asset(client)
    r = client.post(f"{RD}/sets", headers=STEWARD, json={"id": "iso3166-1", "asset_id": aid, "name": "国コード"})
    assert r.status_code == 201, r.text
    return client, "iso3166-1"


def draft(client, sid, c, who=STEWARD, code=201, **kw):
    r = client.post(f"{RD}/sets/{sid}/versions", headers=who, json={"codes": c, **kw})
    assert r.status_code == code, r.text
    return r.json()


def publish(client, sid, n, who=OWNER, code=200):
    r = client.post(f"{RD}/sets/{sid}/versions/{n}/publish", headers=who)
    assert r.status_code == code, r.text
    return r.json()


# ---------------------------------------------------------------- 値を保持できる条件 (§18.2 の例外)
@pytest.mark.parametrize("kw, match", [
    ({"sensitivity": "confidential"}, "confidential"),
    ({"sensitivity": "restricted"}, "restricted"),
    ({"contains_pii": True, "sensitivity": "internal"}, "PII"),
    ({"schema": [{"name": "x", "pii": True}]}, "PII"),
    ({"data_class": "master"}, "data_class=reference"),
])
def test_sensitive_or_pii_assets_are_rejected(client, kw, match):
    aid = ref_asset(client, **kw)
    who = U("rd-owner", "owner,steward", "*")  # 資産の当事者なので ABAC は通過し、値を保持できる条件で 422 になる
    r = client.post(f"{RD}/sets", headers=who, json={"id": "x", "asset_id": aid, "name": "x"})
    assert r.status_code == 422 and match in r.json()["detail"]


def test_asset_turning_sensitive_blocks_new_versions_and_publish(rset):
    client, sid = rset
    draft(client, sid, V1)
    with engine.begin() as conn:
        conn.execute(text("UPDATE data_assets SET sensitivity = 'confidential'"))
    who = U("rd-steward", "steward", "ref")
    r = client.post(f"{RD}/sets/{sid}/versions", headers=who, json={"codes": V2})
    assert r.status_code == 422
    assert "confidential" in publish(client, sid, 1, code=422)["detail"]
    with engine.begin() as conn:
        conn.execute(text("UPDATE data_assets SET sensitivity = 'internal', contains_pii = :t"), {"t": True})
    assert "PII" in publish(client, sid, 1, code=422)["detail"]


def test_personal_data_in_codes_is_rejected(rset):
    client, sid = rset
    r = client.post(f"{RD}/sets/{sid}/versions", headers=STEWARD, json={"codes": codes(("A", "担当 taro@example.com"))})
    assert r.status_code == 422 and r.json()["type"] == "urn:edgf:problem:audit-payload-rejected"


# ---------------------------------------------------------------- 版と公開
def test_draft_edit_publish_and_immutability(rset):
    client, sid = rset
    v = draft(client, sid, V1, note="初版")
    assert v["version"] == 1 and v["status"] == "draft" and v["content_hash"] is None
    assert {c["code"] for c in v["codes"]} == {"JP", "US", "DE"} and v["codes"][0]["standard"] == "ISO 3166-1"
    r = client.put(f"{RD}/sets/{sid}/versions/1/codes", headers=STEWARD, json=V1 + codes(("CN", "中国")))
    assert r.status_code == 200 and len(r.json()["codes"]) == 4
    assert client.put(f"{RD}/sets/{sid}/versions/1/codes", headers=STEWARD, json=V1).status_code == 200
    assert client.get(f"{RD}/sets/{sid}/latest", headers=U("x")).status_code == 404  # 公開前
    p = publish(client, sid, 1)
    assert p["status"] == "published" and p["published_by"] == "rd-owner" and len(p["content_hash"]) == 64
    # 公開済みの版は変更できない
    r = client.put(f"{RD}/sets/{sid}/versions/1/codes", headers=STEWARD, json=V2)
    assert r.status_code == 409 and "公開済み" in r.json()["detail"]
    publish(client, sid, 1, code=409)
    assert {c["code"] for c in client.get(f"{RD}/sets/{sid}/versions/1", headers=U("x")).json()["codes"]} == {"JP", "US", "DE"}
    assert draft(client, sid, V2)["version"] == 2


def test_validation(rset):
    client, sid = rset
    draft(client, sid, codes(("A", "x"), ("A", "y")), code=422)
    draft(client, sid, codes(("A", "x"), valid_from="2026-02-01", valid_to="2026-01-01"), code=422)
    empty = draft(client, sid, [])
    publish(client, sid, empty["version"], code=409)
    assert client.post(f"{RD}/sets", headers=STEWARD, json={"id": sid, "asset_id": client.get(f"{RD}/sets/{sid}", headers=U("x")).json()["asset_id"], "name": "x"}).status_code == 409
    assert client.post(f"{RD}/sets/nope/versions", headers=STEWARD, json={"codes": []}).status_code == 404
    assert client.get(f"{RD}/sets/{sid}/versions/99", headers=U("x")).status_code == 404


@pytest.mark.parametrize("who", [STEWARD, ADMIN, U("other-owner", "owner", "*"), U("c", "custodian", "*"), U("x")])
def test_publish_requires_owner_or_committee(rset, who):
    client, sid = rset
    draft(client, sid, V1)
    assert publish(client, sid, 1, who=who, code=403)["action"] == "refdata.publish"
    assert client.get(f"{RD}/sets/{sid}/versions/1", headers=U("x")).json()["status"] == "draft"
    ev = client.get(f"{API}/audit/events", headers=COMMITTEE, params={"action": "authz.denied"}).json()[0]
    assert ev["payload"]["action"] == "refdata.publish"


def test_committee_can_publish_and_write_permissions(rset):
    client, sid = rset
    aid = client.get(f"{RD}/sets/{sid}", headers=U("x")).json()["asset_id"]
    draft(client, sid, V1)
    assert publish(client, sid, 1, who=COMMITTEE)["published_by"] == "chair"
    for who in (U("hr", "steward", "hr"), U("c", "custodian", "*"), U("x"), ADMIN, COMMITTEE):
        r = client.post(f"{RD}/sets/{sid}/versions", headers=who, json={"codes": V2})
        assert r.status_code == 403 and r.json()["action"] == "refdata.write", who
        assert client.post(f"{RD}/sets", headers=who, json={"id": "y", "asset_id": aid, "name": "y"}).status_code == 403
    assert client.put(f"{RD}/sets/{sid}/versions/1/codes", headers=U("x"), json=V2).status_code == 403
    assert draft(client, sid, V2, who=U("dom-owner", "owner", "ref"))["version"] == 2


# ---------------------------------------------------------------- 照会
def test_latest_version_as_of_and_listing(rset):
    client, sid = rset
    draft(client, sid, V1)
    publish(client, sid, 1)
    with engine.begin() as conn:
        conn.execute(text("UPDATE reference_data_versions SET published_at = '2026-01-01 00:00:00' WHERE version = 1"))
    draft(client, sid, V2)
    latest = client.get(f"{RD}/sets/{sid}/latest", headers=U("x")).json()
    assert latest["version"] == 1  # 下書きは最新の公開版にならない
    publish(client, sid, 2)
    assert client.get(f"{RD}/sets/{sid}/latest", headers=U("x")).json()["version"] == 2
    assert client.get(f"{RD}/sets/{sid}/versions/1", headers=U("x")).json()["version"] == 1
    at = lambda v: client.get(f"{RD}/sets/{sid}/as-of", headers=U("x"), params={"at": v})  # noqa: E731
    assert at("2026-06-01T00:00:00Z").json()["version"] == 1
    assert at((datetime.now(timezone.utc) + timedelta(days=1)).isoformat()).json()["version"] == 2
    assert at("2020-01-01T00:00:00Z").status_code == 404
    r = client.get(f"{RD}/sets/{sid}/versions", headers=U("x"), params={"limit": 1})
    assert r.headers["x-total-count"] == "2" and "codes" not in r.json()[0]
    assert [s["id"] for s in client.get(f"{RD}/sets", headers=U("x")).json()] == [sid]


def test_as_of_filters_codes_by_validity(rset):
    client, sid = rset
    draft(client, sid, [{"code": "OLD", "label": "廃止", "valid_to": "2020-01-01"},
                        {"code": "NOW", "label": "現行", "valid_from": "2020-01-01"}])
    publish(client, sid, 1)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    r = client.get(f"{RD}/sets/{sid}/as-of", headers=U("x"), params={"at": future}).json()
    assert [c["code"] for c in r["codes"]] == ["NOW"]
    assert len(client.get(f"{RD}/sets/{sid}/latest", headers=U("x")).json()["codes"]) == 2


def test_diff(rset):
    client, sid = rset
    draft(client, sid, V1)
    draft(client, sid, V2)
    d = client.get(f"{RD}/sets/{sid}/diff", headers=U("x"), params={"from_version": 1, "to_version": 2}).json()
    assert [c["code"] for c in d["added"]] == ["FR"]
    assert [c["code"] for c in d["retired"]] == ["DE"]
    assert d["label_changed"] == [{"code": "US", "from": "米国", "to": "アメリカ合衆国"}]
    assert d["from_version"] == 1 and d["to_version"] == 2
    assert client.get(f"{RD}/sets/{sid}/diff", headers=U("x"), params={"from_version": 1, "to_version": 9}).status_code == 404
    assert client.get(f"{RD}/sets/{sid}/diff", headers=U("x"), params={"from_version": 1}).status_code == 422


def test_read_is_filtered_by_abac(client):
    aid = ref_asset(client, sensitivity="internal", owner="rd-owner")
    client.post(f"{RD}/sets", headers=STEWARD, json={"id": "s", "asset_id": aid, "name": "s"})
    collector = U("cmp", "consent_collector")  # clearance = public
    assert client.get(f"{RD}/sets", headers=collector).json() == []
    assert client.get(f"{RD}/sets/s", headers=collector).status_code == 403
    assert client.get(f"{RD}/sets", headers=U("x")).headers["x-total-count"] == "1"


# ---------------------------------------------------------------- 公開時の影響通知
def test_publish_notifies_downstream_owners(rset):
    client, sid = rset
    for qn, owner in (("sales.orders", "o1"), ("bi.mart", "o2")):
        client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": qn, "name": qn, "data_class": "analytical",
                                                          "sensitivity": "internal", "owner": owner, "steward": "s"})
    for src, dst in (("ref.country", "sales.orders"), ("sales.orders", "bi.mart"), ("bi.mart", "ext.unknown")):
        client.post(f"{API}/lineage/edges", headers=ADMIN, json={"source": src, "target": dst})
    draft(client, sid, V1)
    p = publish(client, sid, 1)
    assert len(p["issues_opened"]) == 2 and p["owners_notified"] == ["o1", "o2"]
    assert p["unregistered_downstream"] == ["ext.unknown"]
    issues = client.get(f"{API}/issues", headers=COMMITTEE, params={"kind": "refdata"}).json()
    assert {i["assignee"] for i in issues} == {"o1", "o2"} and all("第1版" in i["title"] for i in issues)
    draft(client, sid, V2)
    assert len(publish(client, sid, 2)["issues_opened"]) == 2  # 版ごとに起票する


# ---------------------------------------------------------------- 品質ルールからの参照
def _quality(client, sid_ref, rule_type="allowed_values"):
    r = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "sales.q", "name": "q", "data_class": "operational",
                                                          "sensitivity": "internal", "owner": "o", "steward": "qst"})
    aid = r.json()["id"]
    r = client.post(f"{API}/quality/assets/{aid}/rules", headers=U("qst"), json={
        "name": "国コード", "dimension": "validity", "rule_type": rule_type, "column": "country", "params": {"reference_set": sid_ref}})
    return aid, r


def _run(client, aid, values):
    r = client.post(f"{API}/quality/assets/{aid}/runs", headers=U("qst"), json={"records": [{"country": v} for v in values]})
    return r


def test_quality_rule_references_latest_and_pinned_version(rset):
    client, sid = rset
    draft(client, sid, V1)
    publish(client, sid, 1)
    aid, r = _quality(client, sid)
    assert r.status_code == 201, r.text
    run = _run(client, aid, ["JP", "DE", "XX"]).json()
    assert run["results"][0]["passed"] == 2 and run["results"][0]["evaluated"] == 3
    # 新しい版を公開すると最新の版で評価される
    draft(client, sid, V2)
    publish(client, sid, 2)
    run = _run(client, aid, ["JP", "DE", "FR"]).json()
    assert run["results"][0]["passed"] == 2  # DE は廃止、FR は追加
    # 版を固定した参照
    rule = client.post(f"{API}/quality/assets/{aid}/rules", headers=U("qst"), json={
        "name": "固定", "dimension": "integrity", "rule_type": "referential", "column": "country",
        "params": {"reference_set": f"{sid}@1"}}).json()
    run = _run(client, aid, ["DE"]).json()
    by = {x["rule_id"]: x for x in run["results"]}
    assert by[rule["id"]]["passed"] == 1
    # ルール本体は値で書き換えられない
    stored = {x["id"]: x for x in client.get(f"{API}/quality/assets/{aid}/rules", headers=U("qst")).json()}
    assert stored[rule["id"]]["params"] == {"reference_set": f"{sid}@1"}


def test_latest_reference_ignores_expired_codes(rset):
    client, sid = rset
    draft(client, sid, [{"code": "OLD", "label": "廃止", "valid_to": "2020-01-01"}, {"code": "NOW", "label": "現行"}])
    publish(client, sid, 1)
    aid, _ = _quality(client, sid)
    assert _run(client, aid, ["OLD", "NOW"]).json()["results"][0]["passed"] == 1


def test_reference_rule_validation(rset):
    client, sid = rset
    aid, r = _quality(client, "nope")
    assert r.status_code == 422 and "unknown reference set" in r.json()["detail"]
    post = lambda params, rt="allowed_values": client.post(f"{API}/quality/assets/{aid}/rules", headers=U("qst"), json={  # noqa: E731
        "name": "r", "dimension": "validity", "rule_type": rt, "column": "country", "params": params})
    assert post({"reference_set": f"{sid}@abc"}).status_code == 422
    assert post({"reference_set": f"{sid}@0"}).status_code == 422
    assert post({"reference_set": ""}).status_code == 422
    assert post({"reference_set": f"{sid}@1"}).status_code == 422  # 未公開
    assert post({"reference_set": sid}, "not_null").status_code == 422
    assert post({"reference_set": sid}).status_code == 201  # 公開前でも定義はできるが
    r = _run(client, aid, ["JP"])
    assert r.status_code == 409 and "公開済みの版がありません" in r.json()["detail"]  # 実行はできない


def test_inline_reference_values_remain_compatible(client):
    r = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "s.q", "name": "q", "data_class": "operational",
                                                          "sensitivity": "internal", "owner": "o", "steward": "qst"})
    aid = r.json()["id"]
    for rt, params in (("referential", {"reference_values": ["a"]}), ("allowed_values", {"values": ["a"]})):
        assert client.post(f"{API}/quality/assets/{aid}/rules", headers=U("qst"), json={
            "name": rt, "dimension": "validity", "rule_type": rt, "column": "c", "params": params}).status_code == 201
    run = client.post(f"{API}/quality/assets/{aid}/runs", headers=U("qst"), json={"records": [{"c": "a"}, {"c": "b"}]}).json()
    assert [x["passed"] for x in run["results"]] == [1, 1]
