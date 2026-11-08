"""Phase 9c (§8 Deduplication): Steward の裁定キュー."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from edgf.db import engine
from tests.conftest import dump_database
from tests.mdm_helpers import ADMIN, API, COMMITTEE, HUB, MDM, OWNER, STEWARD, U, master_asset
from tests.test_mdm_matching import compose, match, rec

N1 = "ゴールデン太郎REVIEWUNIQUE"
N2 = N1 + "WXYZWXYZ"
ADDR = "東京都千代田区REVIEWADDRESS1-1"


@pytest.fixture()
def queued(client):
    """閾値の間のペアを 1 件起票した状態。"""
    master_asset(client)
    gid = match(client, rec("crm", "c1", full_name=N1, address=ADDR))["golden_id"]
    r = match(client, rec("erp", "e1", full_name=N2, address=ADDR, email="review.person@example.com"),
              [{"golden_id": gid, "attributes": {"full_name": N1, "address": ADDR + "異"}}])
    assert r["decision"] == "review" and len(r["review_ids"]) == 1
    return client, gid, r["review_ids"][0]


def decide(client, rid, decision, who=STEWARD, code=200, reason=""):
    r = client.post(f"{MDM}/party/reviews/{rid}/decision", headers=who, json={"decision": decision, "reason": reason})
    assert r.status_code == code, r.text
    return r.json()


def _pair(client, gid):
    return match(client, rec("erp", "e1", full_name=N2, address=ADDR),
                 [{"golden_id": gid, "attributes": {"full_name": N1, "address": ADDR + "異"}}])


def test_queue_holds_only_comparison_results(queued, edgf_logs):
    client, gid, rid = queued
    item = client.get(f"{MDM}/party/reviews/{rid}", headers=STEWARD)
    assert item.status_code == 200
    body = item.json()
    assert body["status"] == "pending" and body["golden_id"] == gid and 0.75 <= body["score"] < 0.92
    assert body["comparisons"]["full_name"]["result"] == "similar" and "similarity" in body["comparisons"]["full_name"]
    assert body["comparisons"]["birth_date"]["result"] == "missing"
    listing = client.get(f"{MDM}/party/reviews", headers=U("x", "", ""), params={"status": "pending"})
    assert listing.status_code == 403  # confidential の資産は consumer に見えない
    listing = client.get(f"{MDM}/party/reviews", headers=U("x", "steward", "*"), params={"status": "pending"})
    assert listing.headers["x-total-count"] == "1"
    assert client.get(f"{MDM}/party/reviews", headers=STEWARD, params={"status": "merged"}).json() == []
    assert client.get(f"{MDM}/party/reviews/nope", headers=STEWARD).status_code == 404
    # API も DB もログも値を持たない
    dump, logs = dump_database(), edgf_logs.text()
    for value in (N1, N2, ADDR, "review.person@example.com"):
        assert value not in item.text and value not in listing.text
        assert value.lower() not in dump and value.lower() not in logs


def test_pair_is_not_requeued_while_pending(queued):
    client, gid, rid = queued
    again = _pair(client, gid)
    assert again["decision"] == "review" and again["review_ids"] == [rid]
    # 候補を渡さなくても、裁定待ちの間は新規を作らない
    assert match(client, rec("erp", "e1", full_name=N2))["review_ids"] == [rid]
    assert client.get(f"{MDM}/party/reviews", headers=STEWARD).headers["x-total-count"] == "1"


def test_merge_links_source_and_requires_recompose(queued):
    client, gid, rid = queued
    v1 = compose(client, gid, [rec("crm", "c1", full_name=N1)])
    assert v1["version"] == 1
    r = decide(client, rid, "merge", reason="同一人物と確認")
    assert r["status"] == "merged" and r["decided_by"] == "stw" and r["reason"] == "同一人物と確認"
    linked = _pair(client, gid)
    assert linked["decision"] == "linked" and linked["golden_id"] == gid and linked["stage"] == "xref"
    assert linked["recompose_required"] is True
    decide(client, rid, "merge", code=409)
    decide(client, rid, "not_match", code=409)
    out = compose(client, gid, [rec("crm", "c1", full_name=N1), rec("erp", "e1", full_name=N2)])
    assert out["version"] == 3 and out["reason"] == "compose"  # v2 は merge の版 (再構成待ち)


def test_not_match_is_never_requeued_and_creates_new_golden(queued):
    client, gid, rid = queued
    r = decide(client, rid, "not_match", who=OWNER, reason="別人")
    assert r["status"] == "not_match" and r["new_golden_id"] and r["new_golden_id"] != gid
    again = _pair(client, gid)
    assert again["decision"] == "linked" and again["golden_id"] == r["new_golden_id"]
    assert client.get(f"{MDM}/party/reviews", headers=STEWARD).headers["x-total-count"] == "1"
    assert client.get(f"{MDM}/party/reviews", headers=STEWARD, params={"status": "pending"}).json() == []


def test_not_match_with_other_candidates_pending(client):
    master_asset(client)
    g1 = match(client, rec("crm", "c1", full_name=N1))["golden_id"]
    g2 = match(client, rec("crm", "c2", full_name="完全に別の値"))["golden_id"]
    cands = [{"golden_id": g1, "attributes": {"full_name": N1, "address": ADDR + "異"}},
             {"golden_id": g2, "attributes": {"full_name": N1, "address": ADDR + "異"}}]
    r = match(client, rec("erp", "e1", full_name=N2, address=ADDR), cands)
    assert r["decision"] == "review" and len(r["review_ids"]) == 2
    by_golden = {c["golden_id"]: c for c in r["candidates"]}
    assert set(by_golden) == {g1, g2}
    items = {i["golden_id"]: i["id"] for i in client.get(f"{MDM}/party/reviews", headers=STEWARD).json()}
    first = decide(client, items[g1], "not_match")
    assert first["new_golden_id"] is None  # まだ g2 の裁定が残っている
    # not_match のペアは比較から外れ、再起票されない
    again = match(client, rec("erp", "e1", full_name=N2, address=ADDR), cands)
    assert again["decision"] == "review" and again["review_ids"] == [items[g2]]
    merged = decide(client, items[g2], "merge")
    assert merged["status"] == "merged"
    assert match(client, rec("erp", "e1"))["golden_id"] == g2


def test_auto_merge_supersedes_pending_reviews(queued):
    client, gid, rid = queued
    other = match(client, rec("crm", "c9", email="same@example.com"))["golden_id"]
    r = match(client, rec("erp", "e1", email="same@example.com"), [{"golden_id": other, "attributes": {"email": "same@example.com"}}])
    assert r["decision"] == "linked" and r["golden_id"] == other
    assert client.get(f"{MDM}/party/reviews/{rid}", headers=STEWARD).json()["status"] == "superseded"


@pytest.mark.parametrize("who", [HUB, ADMIN, COMMITTEE, U("x", "steward", "master"), U("y", "owner", "*")])
def test_only_asset_steward_or_owner_can_decide(queued, who):
    client, gid, rid = queued
    r = decide(client, rid, "merge", who=who, code=403)
    assert r["action"] == "mdm.review.decide"
    ev = client.get(f"{API}/audit/events", headers=COMMITTEE, params={"action": "authz.denied"}).json()[0]
    assert ev["payload"]["action"] == "mdm.review.decide"
    assert client.post(f"{MDM}/party/reviews/{rid}/decision", headers=STEWARD, json={"decision": "bogus"}).status_code == 422


def test_split_of_steward_merge_keeps_audit_trail(queued):
    client, gid, rid = queued
    decide(client, rid, "split", code=409)  # merge 前は分割できない
    decide(client, rid, "merge")
    r = decide(client, rid, "split", reason="誤マージ")
    assert r["status"] == "split" and r["new_golden_id"]
    assert match(client, rec("erp", "e1"))["golden_id"] == r["new_golden_id"]
    assert match(client, rec("crm", "c1"))["golden_id"] == gid
    actions = [e["action"] for e in client.get(f"{API}/audit/events", headers=COMMITTEE, params={"entity_id": rid}).json()]
    assert actions[:2] == ["mdm.review_split", "mdm.review_merge"]


def test_split_of_auto_merge(client):
    master_asset(client)
    a = rec("crm", "c1", email="x@example.com")
    gid = match(client, a)["golden_id"]
    b = match(client, rec("erp", "e1", email="x@example.com"), [{"golden_id": gid, "attributes": {"email": "x@example.com"}}])
    url = f"{MDM}/party/golden-records/{gid}/split"
    body = {"source_refs": [b["source_ref"]], "reason": "誤マージ"}
    r = client.post(url, headers=HUB, json=body)
    assert r.status_code == 403 and r.json()["action"] == "mdm.review.decide"
    assert client.post(url, headers=STEWARD, json={"source_refs": [b["source_ref"]], "reason": ""}).status_code == 422
    assert client.post(url, headers=STEWARD, json={"source_refs": ["0" * 64], "reason": "x"}).status_code == 422
    assert client.post(url, headers=STEWARD, json={"source_refs": [a_ref := match(client, a)["source_ref"], b["source_ref"]], "reason": "x"}).status_code == 422
    assert a_ref
    r = client.post(url, headers=STEWARD, json=body)
    assert r.status_code == 200
    out = r.json()
    assert out["golden_id"] == gid and set(out["recompose_required"]) == {gid, out["new_golden_id"]}
    assert match(client, rec("erp", "e1"))["golden_id"] == out["new_golden_id"]
    # 分割後は別々に再構成できる
    assert compose(client, gid, [a])["version"] == 1
    assert compose(client, out["new_golden_id"], [rec("erp", "e1", email="y@example.com")])["version"] == 2
    ev = client.get(f"{API}/audit/events", headers=COMMITTEE, params={"action": "mdm.split"}).json()[0]
    assert ev["actor"] == "dev:stw" and ev["payload"]["new_golden_id"] == out["new_golden_id"]


def test_reason_with_pii_is_rejected_by_audit_guard(queued):
    client, gid, rid = queued
    r = decide(client, rid, "merge", code=422, reason="連絡先 pii.person@example.com")
    assert r["type"] == "urn:edgf:problem:audit-payload-rejected"
    assert client.get(f"{MDM}/party/reviews/{rid}", headers=STEWARD).json()["status"] == "pending"
    assert "pii.person@example.com" not in dump_database()


def _expire(rid: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE mdm_reviews SET due_at = '2000-01-01 00:00:00' WHERE id = :i"), {"i": rid})


def test_overdue_reviews_open_and_escalate_issues(queued):
    client, gid, rid = queued
    sweep = lambda who=COMMITTEE: client.post(f"{MDM}/reviews/escalate-overdue", headers=who)  # noqa: E731
    assert sweep(STEWARD).status_code == 403
    assert sweep().json() == {"overdue_reviews": [], "issues_opened": [], "issues_escalated": []}
    _expire(rid)
    r = sweep().json()
    assert r["overdue_reviews"] == [rid] and len(r["issues_opened"]) == 1
    issue = client.get(f"{API}/issues", headers=COMMITTEE, params={"kind": "mdm"}).json()[0]
    assert issue["assignee"] == "stw" and rid in issue["title"] and N1 not in json.dumps(issue, ensure_ascii=False)
    assert sweep().json()["issues_opened"] == []
    with engine.begin() as conn:
        conn.execute(text("UPDATE issues SET due_at = '2000-01-01 00:00:00' WHERE kind = 'mdm'"))
    assert len(sweep().json()["issues_escalated"]) == 1
    assert client.get(f"{API}/issues", headers=COMMITTEE, params={"kind": "mdm"}).json()[0]["escalation_level"] == "owner"
