"""Phase 9b (§8 / §18.2): ステートレスなマッチングと Golden Record の生成."""
from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy.exc import IntegrityError

from edgf.db import SessionLocal
from edgf.models import MdmXref
from edgf.services.mdm import make_source_ref
from edgf.services.subject_ref import to_subject_ref
from tests.conftest import dump_database
from tests.mdm_helpers import ADMIN, API, COMMITTEE, HUB, MDM, OWNER, STEWARD, U, master_asset

NAME = "ゴールデン太郎UNIQUEVALUE"
EMAIL = "golden.unique.person@example.com"
KEY = "CRM-RAW-KEY-77731"
ADDRESS = "東京都千代田区UNIQUEADDRESS1-1"


@pytest.fixture()
def party(client):
    master_asset(client)
    return client


def rec(system="crm", key=KEY, updated_at=None, **attrs):
    body = {"source_system": system, "key": key, "attributes": attrs}
    if updated_at:
        body["updated_at"] = updated_at
    return body


def match(client, record, candidates=(), who=HUB, entity="party", code=200):
    r = client.post(f"{MDM}/{entity}/match", headers=who, json={"record": record, "candidates": list(candidates)})
    assert r.status_code == code, r.text
    return r.json()


def compose(client, gid, records, who=HUB, entity="party", code=200, **kw):
    r = client.post(f"{MDM}/{entity}/golden-records/{gid}/compose", headers=who, json={"records": records, **kw})
    assert r.status_code == code, r.text
    return r.json()


# ---------------------------------------------------------------- 2 段階のマッチング
def test_exact_key_auto_merges(party):
    a = match(party, rec(email=EMAIL, full_name=NAME))
    assert a["decision"] == "new" and a["recompose_required"] is True
    gid = a["golden_id"]
    b = match(party, rec("erp", "ERP-1", email=EMAIL.upper(), full_name="全く別の名前"),
              [{"golden_id": gid, "attributes": {"email": EMAIL, "full_name": NAME}}])
    assert b["decision"] == "linked" and b["golden_id"] == gid and b["stage"] == "exact" and b["score"] == 1.0


def test_high_similarity_auto_merges(party):
    gid = match(party, rec(full_name="山田 太郎", address="東京都千代田区1-1"))["golden_id"]
    r = match(party, rec("erp", "E2", full_name="山田 太郎", address="東京都千代田区1-1"),
              [{"golden_id": gid, "attributes": {"full_name": "山田 太郎", "address": "東京都千代田区1-1"}}])
    assert r["decision"] == "linked" and r["stage"] == "similarity" and r["score"] == 1.0


def test_between_thresholds_goes_to_review_with_comparisons_only(party):
    gid = match(party, rec(full_name=NAME))["golden_id"]
    r = match(party, rec("erp", "E2", full_name=NAME + "XYZW", address=ADDRESS),
              [{"golden_id": gid, "attributes": {"full_name": NAME, "address": ADDRESS + "異なる"}}])
    assert r["decision"] == "review" and r["golden_id"] is None
    cand = r["candidates"][0]
    assert cand["golden_id"] == gid and 0.75 <= cand["score"] < 0.92
    assert cand["comparisons"]["full_name"]["result"] == "similar"
    assert cand["comparisons"]["birth_date"] == {"result": "missing"}
    assert NAME not in json.dumps(r, ensure_ascii=False) and ADDRESS not in json.dumps(r, ensure_ascii=False)


def test_below_review_threshold_creates_new(party):
    gid = match(party, rec(full_name="山田太郎"))["golden_id"]
    r = match(party, rec("erp", "E2", full_name="Completely Different"),
              [{"golden_id": gid, "attributes": {"full_name": "山田太郎"}}])
    assert r["decision"] == "new" and r["golden_id"] != gid and r["score"] < 0.75


def test_duplicates_in_source_produce_one_golden_record(party):
    """受け入れ基準: 重複を含むソースを取り込むと Golden Record が生成される。"""
    rows = [("crm", "1", {"email": "a@example.com", "full_name": "A"}),
            ("erp", "9", {"email": "a@example.com", "full_name": "A Inc"}),
            ("web", "w", {"email": "b@example.com", "full_name": "B"}),
            ("crm", "1", {"email": "a@example.com", "full_name": "A"})]
    hub: dict[str, dict] = {}
    seen = []
    for system, key, attrs in rows:
        cands = [{"golden_id": g, "attributes": v} for g, v in hub.items()]
        r = match(party, rec(system, key, **attrs), cands)
        hub.setdefault(r["golden_id"], attrs)
        seen.append(r["golden_id"])
    assert seen[0] == seen[1] == seen[3] and seen[2] != seen[0] and len(hub) == 2
    out = compose(party, seen[0], [rec("crm", "1", email="a@example.com", full_name="A"),
                                   rec("erp", "9", email="a@example.com", full_name="A Inc")])
    assert out["version"] == 1 and out["attributes"]["full_name"] == "A"


def test_same_source_returns_existing_link_and_xref_is_unique(party):
    a = match(party, rec(full_name=NAME))
    b = match(party, rec(full_name="別の値"))
    assert b["decision"] == "linked" and b["stage"] == "xref" and b["golden_id"] == a["golden_id"]
    # DB の一意制約でも保証する
    with SessionLocal() as s:
        s.add(MdmXref(entity_type="party", source_ref=a["source_ref"], source_system="crm", golden_id=a["golden_id"],
                      link_type="new", linked_by="x"))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()


def test_input_validation_does_not_echo_values(party):
    r = party.post(f"{MDM}/party/match", headers=HUB, json={"record": rec(unknown_attr=NAME)})
    assert r.status_code == 422 and NAME not in r.text
    r = party.post(f"{MDM}/party/match", headers=HUB, json={"record": rec("nosys", full_name=NAME)})
    assert r.status_code == 422 and NAME not in r.text and KEY not in r.text
    r = party.post(f"{MDM}/party/match", headers=HUB, json={"record": rec(key="   ", full_name=NAME)})
    assert r.status_code == 422
    r = party.post(f"{MDM}/party/match", headers=HUB, json={"record": {"source_system": "crm", "attributes": {}}})
    assert r.status_code == 422
    r = party.post(f"{MDM}/party/match", headers=HUB, json={
        "record": rec(full_name=NAME), "candidates": [{"golden_id": "nope", "attributes": {"full_name": NAME}}]})
    assert r.status_code == 422 and NAME not in r.text
    assert party.post(f"{MDM}/nope/match", headers=HUB, json={"record": rec()}).status_code == 404


def test_entity_requires_registered_master_asset(client):
    r = client.post(f"{MDM}/party/match", headers=HUB, json={"record": rec(full_name=NAME)})
    assert r.status_code == 409 and r.json()["type"] == "urn:edgf:problem:mdm-asset-missing"
    client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "mdm.party", "name": "p", "data_class": "operational",
                                                      "sensitivity": "internal", "custodian": "hub"})
    assert client.post(f"{MDM}/party/match", headers=HUB, json={"record": rec(full_name=NAME)}).status_code == 409


# ---------------------------------------------------------------- Survivorship
def _golden_of(client, records):
    gid = None
    for r in records:
        cands = [{"golden_id": gid, "attributes": {"customer_no": "SAME"}}] if gid else []
        res = match(client, {**r, "attributes": {**r["attributes"], "customer_no": "SAME"}}, cands)
        gid = res["golden_id"]
    return gid


def test_all_four_survivorship_rules_and_provenance(party):
    records = [
        rec("web", "w", updated_at="2026-03-01T00:00:00Z", email="new@example.com", full_name="Web Name",
            address="短い", segment="gold"),
        rec("crm", "c", updated_at="2026-01-01T00:00:00Z", email="old@example.com", full_name="CRM Name",
            address="これは一番長い住所の表記です", segment="silver"),
        rec("erp", "e", updated_at="2026-02-01T00:00:00Z", email="mid@example.com", full_name="ERP Name",
            address="中くらいの住所", segment=" GOLD "),
    ]
    gid = _golden_of(party, records)
    out = compose(party, gid, [{**r, "attributes": {**r["attributes"], "customer_no": "SAME"}} for r in records])
    attrs, prov = out["attributes"], out["provenance"]
    refs = {s: make_source_ref(s, k) for s, k in (("web", "w"), ("crm", "c"), ("erp", "e"))}
    # source_priority: crm(100) > erp(80) > web(10)
    assert attrs["full_name"] == "CRM Name"
    assert prov["full_name"] == {"source_ref": refs["crm"], "source_system": "crm", "rule": "source_priority"}
    # most_recent
    assert attrs["email"] == "new@example.com"
    assert prov["email"] == {"source_ref": refs["web"], "source_system": "web", "rule": "most_recent"}
    # most_complete
    assert attrs["address"] == "これは一番長い住所の表記です"
    assert prov["address"]["rule"] == "most_complete" and prov["address"]["source_system"] == "crm"
    # most_frequent: gold 2 件 (正規化で同一)。勝者は優先度の高い erp の値
    assert prov["segment"]["rule"] == "most_frequent" and prov["segment"]["source_system"] == "erp"
    assert attrs["segment"] == " GOLD "
    # 値の無い属性
    assert attrs["birth_date"] is None and prov["birth_date"]["source_ref"] is None
    assert out["reason"] == "compose" and out["version"] == 1 and out["changed"] is True
    assert len(out["content_hash"]) == 64


def test_compose_requires_exact_source_set_and_is_idempotent(party):
    a = rec("crm", "c", email="x@example.com")
    b = rec("erp", "e", email="x@example.com")
    gid = match(party, a)["golden_id"]
    match(party, b, [{"golden_id": gid, "attributes": {"email": "x@example.com"}}])
    r = compose(party, gid, [a], code=422)
    assert "不足" in r["detail"]
    r = compose(party, gid, [a, b, rec("web", "z")], code=422)
    assert "紐付いていない" in r["detail"]
    assert "重複" in compose(party, gid, [a, a, b], code=422)["detail"]
    first = compose(party, gid, [a, b])
    second = compose(party, gid, [a, b])
    assert first["version"] == second["version"] == 1 and second["changed"] is False
    assert first["content_hash"] == second["content_hash"]
    changed = compose(party, gid, [rec("crm", "c", email="x@example.com", full_name="新しい名前"), b])
    assert changed["version"] == 2 and changed["changed"] is True
    assert changed["content_hash"] != first["content_hash"]
    assert compose(party, "nope", [a], code=404)
    assert compose(party, gid, [a, b], entity="product", code=409)  # product の資産は未登録


def test_steward_override_requires_decide_permission(party):
    a, b = rec("crm", "c", email="x@example.com", full_name="CRM"), rec("web", "w", email="x@example.com", full_name="WEB")
    gid = match(party, a)["golden_id"]
    match(party, b, [{"golden_id": gid, "attributes": {"email": "x@example.com"}}])
    compose(party, gid, [a, b])
    r = compose(party, gid, [a, b], overrides={"full_name": "web"}, code=403)  # Custodian は裁定できない
    assert r["action"] == "mdm.review.decide"
    out = compose(party, gid, [a, b], who=STEWARD, overrides={"full_name": "web"})
    assert out["attributes"]["full_name"] == "WEB" and out["reason"] == "steward_override" and out["version"] == 2
    assert out["provenance"]["full_name"] == {"source_ref": make_source_ref("web", "w"), "source_system": "web", "rule": "steward_override"}
    assert compose(party, gid, [a, b], who=OWNER, overrides={"nope": "web"}, code=422)
    assert compose(party, gid, [a, b], who=OWNER, overrides={"birth_date": "web"}, code=422)


# ---------------------------------------------------------------- 認可
def test_match_compose_permissions(party):
    body = {"record": rec(full_name=NAME)}
    assert match(party, body["record"], who=U("st", "steward", "master"))["decision"] == "new"
    for who in (U("x"), U("st2", "steward", "hr"), U("other-hub", "custodian", "*"), ADMIN, COMMITTEE, OWNER):
        r = party.post(f"{MDM}/party/match", headers=who, json=body)
        assert r.status_code == 403, who


# ---------------------------------------------------------------- ステートレス
def test_source_values_are_never_persisted_or_logged(party, edgf_logs):
    values = [NAME, EMAIL, KEY, ADDRESS, "1999-12-31", "CANDIDATEVALUE9"]
    first = match(party, rec(email=EMAIL, full_name=NAME, address=ADDRESS, birth_date="1999-12-31"))
    gid = first["golden_id"]
    responses = [
        party.post(f"{MDM}/party/match", headers=HUB, json={
            "record": rec("erp", "ERP-RAW-KEY-5", full_name=NAME + "XYZW", address=ADDRESS),
            "candidates": [{"golden_id": gid, "attributes": {"full_name": "CANDIDATEVALUE9", "address": ADDRESS}}]}),
        party.post(f"{MDM}/party/golden-records/{gid}/compose", headers=HUB, json={
            "records": [rec(email=EMAIL, full_name=NAME, address=ADDRESS, birth_date="1999-12-31")]}),
    ]
    assert all(r.status_code == 200 for r in responses)
    dump = dump_database()
    logs = edgf_logs.text()
    assert first["source_ref"] in dump  # 走査が機能していることの確認
    for value in values + ["ERP-RAW-KEY-5"]:
        assert value.lower() not in dump, value
        assert value.lower() not in logs, value
    assert NAME not in responses[0].text  # match の応答に値は含まれない
    assert responses[1].json()["attributes"]["full_name"] == NAME  # compose の応答でだけ値を返す


def test_source_ref_and_party_subject_ref(party):
    r = match(party, rec(email=EMAIL, customer_no="C1", full_name=NAME))
    assert r["source_ref"] == make_source_ref("crm", KEY) and r["source_ref"] != make_source_ref("erp", KEY)
    dump = dump_database()
    assert to_subject_ref("email", EMAIL) in dump and to_subject_ref("customer_id", "C1") in dump
    assert hashlib.sha256(EMAIL.encode()).hexdigest() not in dump


def test_reasons_and_audit_contain_no_values(party):
    match(party, rec(email=EMAIL, full_name=NAME))
    ev = party.get(f"{API}/audit/events", headers=COMMITTEE, params={"action": "mdm.match"}).json()[0]
    assert ev["payload"]["decision"] == "new" and ev["actor"] == "dev:hub" and "source_ref" not in ev["payload"]
