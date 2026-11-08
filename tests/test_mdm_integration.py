"""Phase 9f: Right to Erasure / Policy / KPI との統合、マイグレーションの往復."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from edgf.services.mdm import make_source_ref
from edgf.services.subject_ref import to_subject_ref
from tests.conftest import dump_database
from tests.mdm_helpers import ADMIN, API, COMMITTEE, HUB, MDM, OWNER, STEWARD, master_asset
from tests.test_mdm_matching import compose, match, rec
from tests.test_privacy_erasure import answer, receive, step, tasks_for

ROOT = Path(__file__).resolve().parent.parent
PRIVACY = f"{API}/privacy"
RAW = "Erasure.Raw.Person@Example.com"
NAME = "消去太郎ERASEUNIQUE"


@pytest.fixture()
def party(client):
    aid = master_asset(client)
    a = rec("crm", "c1", email=RAW, full_name=NAME)
    b = rec("erp", "e1", email=RAW, full_name=NAME + "別表記")
    gid = match(client, a)["golden_id"]
    match(client, b, [{"golden_id": gid, "attributes": {"email": RAW}}])
    out = compose(client, gid, [a, b])
    other = match(client, rec("crm", "c2", email="other.person@example.com"))["golden_id"]
    return client, gid, other, aid, out


def get_golden(client, gid):
    return client.get(f"{MDM}/party/golden-records/{gid}", headers=STEWARD).json()


def _to_dispatch(client):
    rid = receive(client)["id"]
    scoped = step(client, rid, "scope")
    step(client, rid, "approve")
    return rid, scoped, step(client, rid, "dispatch")


def test_party_golden_record_is_in_scope_and_tombstoned(party, edgf_logs):
    client, gid, other, aid, composed = party
    rid, scoped, _ = _to_dispatch(client)
    t = {x["qualified_name"]: x for x in scoped["tasks"]}["mdm.party"]
    assert t["origin"] == "mdm_golden" and t["golden_ids"] == [gid] and t["asset_id"] == aid
    assert other not in t["golden_ids"]
    # MDM Hub (master 資産の Custodian) が pull して応答する
    work = tasks_for(client, HUB, status="pending")[0]
    assert work["golden_ids"] == [gid] and work["subject_ref"] == to_subject_ref("email", RAW)
    assert get_golden(client, gid)["status"] == "active"  # 完了までは有効
    answer(client, "hub", work["id"])
    # tombstone: 内容ハッシュと source_ref は証跡として残す
    g = get_golden(client, gid)
    assert g["status"] == "tombstoned" and g["tombstoned_at"] and g["source_refs"] == []
    v = g["version"]
    assert v["tombstoned"] is True and v["content_hash"] == composed["content_hash"] and v["valid_to"]
    assert sorted(v["source_refs"]) == sorted([make_source_ref("crm", "c1"), make_source_ref("erp", "e1")])
    xr = client.get(f"{MDM}/xref", headers=HUB, params={"source_system": "crm", "key": "c1"}).json()
    assert xr[0]["status"] == "tombstoned" and xr[0]["golden_id"] == gid
    # 他の Golden Record には影響しない
    assert get_golden(client, other)["status"] == "active"
    # 消去後は subject_ref と結び付かず、値の検証もできない
    dump = dump_database()
    assert composed["content_hash"] in dump and make_source_ref("crm", "c1") in dump
    r = client.post(f"{MDM}/party/golden-records/{gid}/verify", headers=HUB, json={"attributes": composed["attributes"], "version": 1})
    assert r.status_code == 409
    from sqlalchemy import text
    from edgf.db import engine
    with engine.connect() as conn:
        assert conn.execute(text("SELECT hash_salt FROM golden_records WHERE id = :i"), {"i": gid}).scalar() is None
        assert conn.execute(text("SELECT count(*) FROM mdm_subject_links WHERE golden_id = :i"), {"i": gid}).scalar() == 0
    # 消去証明に Golden Record が含まれる
    cert = client.get(f"{PRIVACY}/erasure-requests/{rid}/certificate", headers=COMMITTEE).json()
    assert {t["qualified_name"]: t["golden_ids"] for t in cert["tasks"]}["mdm.party"] == [gid]
    # 生の値はどこにも残らない
    logs = edgf_logs.text()
    for value in (RAW.lower(), NAME.lower()):
        assert value not in dump and value not in logs


def test_tombstoned_records_reject_further_operations(party):
    client, gid, other, aid, composed = party
    _to_dispatch(client)
    answer(client, "hub", tasks_for(client, HUB, status="pending")[0]["id"])
    a = rec("crm", "c1", email=RAW)
    # 消去済みのソースは再度取り込めない
    r = client.post(f"{MDM}/party/match", headers=HUB, json={"record": a})
    assert r.status_code == 409 and "消去済み" in r.json()["detail"]
    compose(client, gid, [a], code=409)
    r = client.post(f"{MDM}/party/golden-records/{gid}/split", headers=STEWARD, json={"source_refs": ["0" * 64], "reason": "x"})
    assert r.status_code == 409
    r = client.post(f"{MDM}/party/match", headers=HUB, json={"record": rec("web", "w", email="z@example.com"),
                                                              "candidates": [{"golden_id": gid, "attributes": {}}]})
    assert r.status_code == 422  # 消去済みは候補にできない
    assert client.get(f"{MDM}/party/golden-records", headers=STEWARD, params={"status": "tombstoned"}).headers["x-total-count"] == "1"


def test_compose_after_tombstone_is_conflict(party):
    client, gid, *_ = party
    from edgf.db import SessionLocal
    from edgf.services import mdm
    with SessionLocal() as s:
        mdm.tombstone(s, [gid, "missing"], "req")
        mdm.tombstone(s, [gid], "req")  # 冪等
        s.commit()
    assert compose(client, gid, [rec("crm", "c1", email=RAW)], code=409)


def test_legal_hold_and_failure_do_not_tombstone(party):
    client, gid, other, aid, composed = party
    client.post(f"{API}/assets/{aid}/legal-hold", headers=COMMITTEE, json={"legal_hold": True, "reason": "訴訟"})
    rid = receive(client)["id"]
    t = {x["qualified_name"]: x for x in step(client, rid, "scope")["tasks"]}["mdm.party"]
    assert t["status"] == "withheld" and t["golden_ids"] == [gid]
    step(client, rid, "approve")
    assert step(client, rid, "dispatch")["status"] == "partially_completed"
    assert get_golden(client, gid)["status"] == "active"
    # Hold を解除して再度要求し、失敗を報告しても tombstone にしない
    client.post(f"{API}/assets/{aid}/legal-hold", headers=COMMITTEE, json={"legal_hold": False, "reason": "解除"})
    _to_dispatch(client)
    w = tasks_for(client, HUB, status="pending")[0]
    client.post(f"{PRIVACY}/erasure-tasks/{w['id']}/ack", headers=HUB)
    client.post(f"{PRIVACY}/erasure-tasks/{w['id']}/complete", headers=HUB, json={"outcome": "failed"})
    assert get_golden(client, gid)["status"] == "active"


def test_lineage_from_master_asset_is_included(party):
    client, gid, other, aid, composed = party
    client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "bi.customer_mart", "name": "m", "data_class": "analytical",
                                                      "sensitivity": "confidential", "custodian": "c2", "owner": "o2",
                                                      "schema": [{"name": "x", "pii": True}]})
    client.post(f"{API}/lineage/edges", headers=ADMIN, json={"source": "mdm.party", "target": "bi.customer_mart"})
    rid = receive(client)["id"]
    tasks = {t["qualified_name"]: t for t in step(client, rid, "scope")["tasks"]}
    assert tasks["bi.customer_mart"]["origin"] == "lineage" and tasks["bi.customer_mart"]["depth"] == 1
    assert tasks["bi.customer_mart"]["golden_ids"] == []


def test_identifier_column_on_master_keeps_origin_and_attaches_goldens(client):
    aid = master_asset(client, schema=[{"name": "email", "identifier_type": "email"}])
    gid = match(client, rec("crm", "c1", email=RAW))["golden_id"]
    t = step(client, receive(client)["id"], "scope")["tasks"][0]
    assert t["asset_id"] == aid and t["origin"] == "identifier_match" and t["golden_ids"] == [gid]


def test_split_keeps_subject_link_on_both_until_recomposed(party):
    client, gid, other, aid, composed = party
    r = client.post(f"{MDM}/party/golden-records/{gid}/split", headers=STEWARD,
                    json={"source_refs": [make_source_ref("erp", "e1")], "reason": "別人"}).json()
    new = r["new_golden_id"]
    t = {x["qualified_name"]: x for x in step(client, receive(client)["id"], "scope")["tasks"]}["mdm.party"]
    assert sorted(t["golden_ids"]) == sorted([gid, new])  # 再構成までは消去の対象から漏らさない


def test_merged_review_subject_link_is_kept(client):
    master_asset(client)
    n1 = "裁定太郎LINKUNIQUE"
    gid = match(client, rec("crm", "c1", full_name=n1, address="東京都"))["golden_id"]
    r = match(client, rec("erp", "e1", email=RAW, full_name=n1 + "WXYZWXYZ", address="東京都"),
              [{"golden_id": gid, "attributes": {"full_name": n1, "address": "東京都"}}])
    assert r["decision"] == "review"
    client.post(f"{MDM}/party/reviews/{r['review_ids'][0]}/decision", headers=STEWARD, json={"decision": "merge"})
    t = step(client, receive(client)["id"], "scope")["tasks"][0]
    assert t["golden_ids"] == [gid]  # 裁定で紐付いたソースの識別子も対象特定に使える


# ---------------------------------------------------------------- Policy MDM-001
def _mdm001(client, aid):
    res = client.get(f"{API}/policies/evaluate/assets/{aid}", headers=COMMITTEE).json()["results"]
    return {r["policy_id"]: r for r in res}.get("MDM-001")


def test_mdm001(client):
    aid = master_asset(client)
    r = _mdm001(client, aid)
    assert r["status"] == "fail" and "承認" in r["message"] and r["severity"] == "high"
    assert client.post(f"{MDM}/entities/party/approve", headers=OWNER).status_code == 200
    assert _mdm001(client, aid)["status"] == "pass"
    undefined = master_asset(client, "mdm.undefined")
    r = _mdm001(client, undefined)
    assert r["status"] == "fail" and "エンティティ定義" in r["message"]
    chk = client.get(f"{API}/assets/{undefined}/certification/check", headers=COMMITTEE).json()
    assert any("MDM-001" in b for b in chk["blockers"])
    other = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "s.op", "name": "o", "data_class": "operational",
                                                              "sensitivity": "internal"}).json()["id"]
    assert _mdm001(client, other) is None


# ---------------------------------------------------------------- KPI
def test_kpi_duplicate_metrics(client):
    k = client.get(f"{API}/kpi", headers=COMMITTEE).json()
    assert k["mdm_review_pending_count"] == 0 and k["mdm_duplicate_rate"] is None and k["mdm_auto_merge_rate"] is None
    master_asset(client)
    gid = match(client, rec("crm", "c1", email="a@example.com"))["golden_id"]
    match(client, rec("erp", "e1", email="a@example.com"), [{"golden_id": gid, "attributes": {"email": "a@example.com"}}])
    n1 = "KPI太郎UNIQUEVALUE"
    g2 = match(client, rec("crm", "c2", full_name=n1, address="東京"))["golden_id"]
    cand = [{"golden_id": g2, "attributes": {"full_name": n1, "address": "東京"}}]
    r = match(client, rec("erp", "e2", full_name=n1 + "WXYZWXYZ", address="東京"), cand)
    k = client.get(f"{API}/kpi", headers=COMMITTEE).json()
    assert k["mdm_review_pending_count"] == 1
    assert k["mdm_duplicate_rate"] == round(1 / 3, 4) and k["mdm_auto_merge_rate"] == 1.0
    client.post(f"{MDM}/party/reviews/{r['review_ids'][0]}/decision", headers=STEWARD, json={"decision": "merge"})
    k = client.get(f"{API}/kpi", headers=COMMITTEE).json()
    assert k["mdm_review_pending_count"] == 0 and k["mdm_duplicate_rate"] == 0.5 and k["mdm_auto_merge_rate"] == 0.5
    by_domain = {d["scope"]: d for d in client.get(f"{API}/kpi/domains", headers=COMMITTEE).json()}
    assert by_domain["master"]["mdm_duplicate_rate"] == 0.5


# ---------------------------------------------------------------- マイグレーション
def _alembic(args, url):
    res = subprocess.run([str(Path(sys.executable).parent / "alembic"), *args], cwd=ROOT,
                         env={**os.environ, "EDGF_DATABASE_URL": url}, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def _state(db):
    with sqlite3.connect(db) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return version, tables


def test_migrations_round_trip(tmp_path):
    db = tmp_path / "m.db"
    url = f"sqlite:///{db}"
    _alembic(["upgrade", "0011"], url)
    version, tables = _state(db)
    expected = {"mdm_rule_approvals", "golden_records", "mdm_xrefs", "golden_versions", "mdm_subject_links", "mdm_reviews",
                "reference_data_sets", "reference_data_versions", "reference_codes"}
    assert version == "0011" and expected <= tables
    steps = [("0010", None), ("0009", "reference_codes"), ("0008", "mdm_reviews"), ("0007", "golden_records"),
             ("0006", "mdm_rule_approvals")]
    for rev, gone in steps:
        _alembic(["downgrade", "-1"], url)
        version, tables = _state(db)
        assert version == rev and (gone is None or gone not in tables)
    with sqlite3.connect(db) as conn:
        assert "golden_ids" not in [r[1] for r in conn.execute("PRAGMA table_info(erasure_tasks)")]
    _alembic(["upgrade", "0011"], url)
    assert _state(db)[0] == "0011"
    with sqlite3.connect(db) as conn:
        assert "golden_ids" in [r[1] for r in conn.execute("PRAGMA table_info(erasure_tasks)")]
