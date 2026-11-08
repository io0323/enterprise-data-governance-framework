"""Phase 9d (§8 Versioning): 版の情報、as-of の照会、verify、xref の検索."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from edgf.db import engine
from sqlalchemy import text
from edgf.services.mdm import make_source_ref
from tests.conftest import dump_database
from tests.mdm_helpers import COMMITTEE, HUB, MDM, OWNER, STEWARD, U, master_asset
from tests.test_mdm_matching import compose, match, rec

VERIFY_VALUE = "VERIFYUNIQUE検証値"
EMAIL = "version.person@example.com"
RAW_KEY = "XREF-RAW-KEY-4242"


@pytest.fixture()
def golden(client):
    master_asset(client)
    a = rec("crm", "c1", email=EMAIL, full_name=VERIFY_VALUE)
    gid = match(client, a)["golden_id"]
    return client, gid, a


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get(client, gid, who=STEWARD, code=200, **params):
    r = client.get(f"{MDM}/party/golden-records/{gid}", headers=who, params=params)
    assert r.status_code == code, r.text
    return r.json()


def verify(client, gid, attributes, code=200, who=HUB, **kw):
    r = client.post(f"{MDM}/party/golden-records/{gid}/verify", headers=who, json={"attributes": attributes, **kw})
    assert r.status_code == code, r.text
    return r.json()


def test_version_info_contains_no_values(golden):
    client, gid, a = golden
    before = get(client, gid)
    assert before["version"] is None and before["recompose_required"] is True and before["current_version"] == 0
    out = compose(client, gid, [a])
    v = get(client, gid)
    assert v["recompose_required"] is False and v["current_version"] == 1
    info = v["version"]
    assert info["version"] == 1 and info["valid_to"] is None and info["content_hash"] == out["content_hash"]
    assert info["source_refs"] == [make_source_ref("crm", "c1")] and info["reason"] == "compose"
    assert info["provenance"]["full_name"] == {"source_ref": make_source_ref("crm", "c1"), "source_system": "crm", "rule": "source_priority"}
    r = client.get(f"{MDM}/party/golden-records/{gid}", headers=STEWARD)
    assert VERIFY_VALUE not in r.text and EMAIL not in r.text and "salt" not in r.text
    assert get(client, "nope", code=404)
    listing = client.get(f"{MDM}/party/golden-records", headers=STEWARD)
    assert listing.headers["x-total-count"] == "1" and "salt" not in listing.text
    assert client.get(f"{MDM}/party/golden-records", headers=STEWARD, params={"status": "tombstoned"}).json() == []
    assert client.get(f"{MDM}/party/golden-records", headers=STEWARD, params={"recompose_required": True}).json() == []


def test_as_of_returns_version_at_that_time(golden):
    client, gid, a = golden
    assert get(client, gid, as_of=now_iso(), code=404)  # 版がまだ無い
    v1 = compose(client, gid, [a])
    t1 = now_iso()
    time.sleep(0.01)
    v2 = compose(client, gid, [rec("crm", "c1", email=EMAIL, full_name="更新後の名前")])
    assert v2["version"] == 2
    assert get(client, gid, as_of=t1)["version"]["content_hash"] == v1["content_hash"]
    assert get(client, gid, as_of=now_iso())["version"]["content_hash"] == v2["content_hash"]
    assert get(client, gid)["version"]["version"] == 2
    early = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    assert get(client, gid, as_of=early, code=404)
    versions = client.get(f"{MDM}/party/golden-records/{gid}/versions", headers=STEWARD).json()
    assert [x["version"] for x in versions] == [1, 2]
    assert versions[0]["valid_to"] == versions[1]["valid_from"] and versions[1]["valid_to"] is None


def test_split_keeps_pre_split_version_queryable_and_rewires_xref(golden):
    """受け入れ基準: 誤マージは Steward が分割でき、その証跡が残る。"""
    client, gid, a = golden
    b = rec("erp", "e1", email=EMAIL, full_name="別人の名前")
    linked = match(client, b, [{"golden_id": gid, "attributes": {"email": EMAIL}}])
    assert linked["golden_id"] == gid
    merged = compose(client, gid, [a, b])
    before_split = now_iso()
    time.sleep(0.01)
    r = client.post(f"{MDM}/party/golden-records/{gid}/split", headers=STEWARD,
                    json={"source_refs": [linked["source_ref"]], "reason": "別人と判明"}).json()
    new_id = r["new_golden_id"]
    # as-of で分割前の版を照会できる
    old = get(client, gid, as_of=before_split)["version"]
    assert old["content_hash"] == merged["content_hash"] and linked["source_ref"] in old["source_refs"]
    # 現在の版は再構成待ちで、xref は付け替わっている
    cur = get(client, gid)
    assert cur["recompose_required"] is True and cur["source_refs"] == [make_source_ref("crm", "c1")]
    assert cur["version"]["reason"] == "split" and cur["version"]["content_hash"] is None
    assert get(client, new_id)["source_refs"] == [linked["source_ref"]]
    assert get(client, new_id)["version"]["reason"] == "split"
    xr = client.get(f"{MDM}/xref", headers=HUB, params={"source_system": "erp", "key": "e1"}).json()
    assert xr[0]["golden_id"] == new_id and xr[0]["link_type"] == "split"
    # 分割前の値はハッシュで検証できる。再構成待ちの版は検証できない
    assert verify(client, gid, merged["attributes"], version=merged["version"])["match"] is True
    verify(client, gid, merged["attributes"], code=409)
    versions = client.get(f"{MDM}/party/golden-records/{gid}/versions", headers=STEWARD).json()
    assert [v["reason"] for v in versions] == ["compose", "split"]


def test_verify_detects_single_character_change(golden, edgf_logs):
    client, gid, a = golden
    out = compose(client, gid, [a])
    attrs = out["attributes"]
    good = verify(client, gid, attrs)
    assert good["match"] is True and good["version"] == 1
    assert verify(client, gid, dict(reversed(list(attrs.items()))))["match"] is True  # キーの順序に依存しない
    changed = {**attrs, "full_name": VERIFY_VALUE + "x"}
    assert verify(client, gid, changed)["match"] is False
    assert verify(client, gid, {**attrs, "full_name": VERIFY_VALUE[:-1]})["match"] is False
    assert verify(client, gid, {k: v for k, v in attrs.items() if k != "segment"})["match"] is False
    assert verify(client, gid, attrs, version=99, code=422)
    assert verify(client, gid, attrs, who=OWNER, code=403)["action"] == "mdm.compose"
    # 検証で送った値はどこにも残らない
    dump, logs = dump_database(), edgf_logs.text()
    for value in (VERIFY_VALUE.lower(), EMAIL):
        assert value not in dump and value not in logs
    ev = client.get(f"{MDM[:-4]}/audit/events", headers=COMMITTEE, params={"action": "mdm.verify"}).json()
    assert {e["payload"]["match"] for e in ev} == {True, False}


def test_content_hash_is_salted_per_golden_record(client):
    master_asset(client)
    same = {"full_name": "同じ値"}
    g1 = match(client, rec("crm", "c1", **same))["golden_id"]
    g2 = match(client, rec("crm", "c2", **same))["golden_id"]
    h1 = compose(client, g1, [rec("crm", "c1", **same)])["content_hash"]
    h2 = compose(client, g2, [rec("crm", "c2", **same)])["content_hash"]
    assert h1 != h2  # 同じ値でも別のハッシュ (辞書攻撃の対策)


def test_xref_lookup_translates_key_to_hmac(golden, edgf_logs):
    client, gid, a = golden
    xr = client.get(f"{MDM}/xref", headers=U("reader", "steward", "*"), params={"source_system": "crm", "key": "c1"})
    assert xr.status_code == 200 and xr.headers["x-total-count"] == "1"
    item = xr.json()[0]
    assert item["golden_id"] == gid and item["source_ref"] == make_source_ref("crm", "c1") and item["status"] == "active"
    assert client.get(f"{MDM}/xref", headers=HUB, params={"source_system": "crm", "key": "unknown"}).json() == []
    assert client.get(f"{MDM}/xref", headers=U("x"), params={"source_system": "crm", "key": "c1"}).json() == []  # ABAC
    assert client.get(f"{MDM}/xref", headers=HUB, params={"source_system": "crm"}).status_code == 422
    assert client.get(f"{MDM}/xref", headers=HUB, params={"source_system": "crm", "key": "  "}).status_code == 422
    # キーを URL に載せない経路。どちらの経路でもログと DB にキーは残らない
    match(client, rec("web", RAW_KEY, full_name="x"))
    client.get(f"{MDM}/xref", headers=HUB, params={"source_system": "web", "key": RAW_KEY})
    r = client.post(f"{MDM}/xref/lookup", headers=HUB, json={"source_system": "web", "key": RAW_KEY})
    assert r.json()[0]["source_ref"] == make_source_ref("web", RAW_KEY)
    assert RAW_KEY.lower() not in edgf_logs.text() and RAW_KEY.lower() not in dump_database()


def test_read_requires_clearance(golden):
    client, gid, a = golden
    assert get(client, gid, who=U("analyst"), code=403)["detail"]
    with engine.begin() as conn:
        conn.execute(text("UPDATE data_assets SET sensitivity = 'internal'"))
    assert get(client, gid, who=U("analyst"))["id"] == gid
