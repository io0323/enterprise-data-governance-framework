"""Phase 11c (§15 / §4 Data Consumer): 利用登録とメジャー版の更新."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from edgf.db import engine
from tests.mesh_helpers import (ADMIN, API, COLUMNS, COMMITTEE, MESH, PO, STEWARD, U, draft, mark_published, publish, published, purpose,
                                setup_contract, subscribe)


@pytest.fixture()
def world(client):
    ctx = setup_contract(client)
    purpose(client)
    purpose(client, "marketing")
    published(client, ctx["contract"]["id"], purposes=["analytics", "marketing"])
    mark_published(ctx["product"]["id"])
    return {"client": client, **ctx, "port_id": ctx["port"]["id"], "cid": ctx["contract"]["id"]}


def _issues(client, assignee):
    rows = client.get(f"{API}/issues", headers=ADMIN, params={"limit": 1000}).json()
    return [i for i in rows if i["assignee"] == assignee]


def _decide(client, sid, approve=True, comment="", who=PO):
    return client.post(f"{MESH}/subscriptions/{sid}/decision", headers=who, json={"approve": approve, "comment": comment})


# ---------------------------------------------------------------- 申請
def test_internal_port_is_active_immediately(world):
    client = world["client"]
    s = subscribe(client, "alice", world["port_id"])
    assert s["status"] == "active" and s["major"] == 1 and s["subscriber"] == "alice"
    subscribe(client, "alice", world["port_id"], code=409)  # 重複
    subscribe(client, "bob", world["port_id"], purpose_id="none", code=422)
    subscribe(client, "bob", world["port_id"], major=9, code=409)
    assert client.post(f"{MESH}/subscriptions", headers=U("bob"), json={"output_port_id": "none", "purpose_id": "analytics"}).status_code == 404


def test_purpose_must_be_in_usage_terms(world):
    client = world["client"]
    purpose(client, "other")
    subscribe(client, "alice", world["port_id"], purpose_id="other", code=422)


def test_empty_usage_terms_accept_any_purpose(client):
    ctx = setup_contract(client)
    purpose(client)
    published(client, ctx["contract"]["id"])
    mark_published(ctx["product"]["id"])
    assert subscribe(client, "alice", ctx["port"]["id"])["status"] == "active"


def test_product_and_contract_state_are_required(client):
    ctx = setup_contract(client)
    purpose(client)
    # Contract が公開されていない、Product が公開されていない
    mark_published(ctx["product"]["id"])
    subscribe(client, "alice", ctx["port"]["id"], code=409)
    published(client, ctx["contract"]["id"])
    with engine.begin() as conn:
        conn.execute(text("UPDATE data_products SET status = 'draft' WHERE id = :id"), {"id": ctx["product"]["id"]})
    subscribe(client, "alice", ctx["port"]["id"], code=409)
    # Contract が無い Output Port
    from tests.mesh_helpers import add_port, port_asset
    a = port_asset(client, "sales.no_contract")
    port = add_port(client, ctx["product"]["id"], a["id"], name="no-contract")
    subscribe(client, "alice", port["id"], code=409)


@pytest.mark.parametrize("sensitivity", ["confidential", "restricted"])
def test_confidential_or_higher_requires_owner_approval(client, sensitivity):
    ctx = setup_contract(client, sensitivity=sensitivity)
    purpose(client)
    published(client, ctx["contract"]["id"], sensitivity=sensitivity)
    mark_published(ctx["product"]["id"])
    s = subscribe(client, "alice", ctx["port"]["id"])
    assert s["status"] == "pending"
    for who in (U("alice"), STEWARD, COMMITTEE, ADMIN, U("asset-owner")):
        assert _decide(client, s["id"], who=who).status_code == 403
    r = _decide(client, s["id"], comment="業務に必要")
    assert r.json()["status"] == "active" and r.json()["decided_by"] == "po"
    assert _decide(client, s["id"]).status_code == 409
    assert _decide(client, "none").status_code == 404


def test_rejection_requires_reason_and_owner_cannot_self_approve(client):
    ctx = setup_contract(client, sensitivity="confidential")
    purpose(client)
    published(client, ctx["contract"]["id"], sensitivity="confidential")
    mark_published(ctx["product"]["id"])
    s = subscribe(client, "alice", ctx["port"]["id"])
    assert _decide(client, s["id"], approve=False).status_code == 422
    assert _decide(client, s["id"], approve=False, comment="目的が不明").json()["status"] == "rejected"
    assert subscribe(client, "alice", ctx["port"]["id"])["status"] == "pending"  # 却下後は再申請できる
    own = subscribe(client, "po", ctx["port"]["id"])
    r = _decide(client, own["id"])
    assert r.status_code == 403 and "職務分離" in r.json()["detail"]


def test_inherited_sensitivity_requires_approval(client):
    """資産の実効機密度が Contract より高くなっていれば承認を求める。"""
    ctx = setup_contract(client)
    purpose(client)
    published(client, ctx["contract"]["id"])
    mark_published(ctx["product"]["id"])
    assert client.patch(f"{API}/assets/{ctx['asset']['id']}", headers=ADMIN, json={"sensitivity": "confidential"}).status_code == 200
    assert subscribe(client, "alice", ctx["port"]["id"])["status"] == "pending"


def test_subscription_does_not_grant_read(client):
    ctx = setup_contract(client, sensitivity="restricted")
    purpose(client)
    published(client, ctx["contract"]["id"], sensitivity="restricted")
    mark_published(ctx["product"]["id"])
    s = subscribe(client, "alice", ctx["port"]["id"])
    assert _decide(client, s["id"]).json()["status"] == "active"
    alice = U("alice", clearance="confidential")
    aid = ctx["asset"]["id"]
    assert client.get(f"{API}/assets/{aid}", headers=alice).status_code == 403
    assert client.get(f"{MESH}/contracts/{ctx['contract']['id']}", headers=alice).status_code == 403
    assert client.get(f"{MESH}/ports/{ctx['port']['id']}", headers=alice).json()["redacted"] is True
    # 閲覧は Owner の閲覧承認でだけ開く
    exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    assert client.post(f"{API}/assets/{aid}/access-grants", headers=U("asset-owner"), json={
        "subject": "alice", "purpose": "分析", "expires_at": exp}).status_code == 201
    assert client.get(f"{API}/assets/{aid}", headers=alice).status_code == 200


def test_subscription_reads(world):
    client = world["client"]
    s = subscribe(client, "alice", world["port_id"])
    url = f"{MESH}/subscriptions"
    for who, code in ((U("alice"), 200), (PO, 200), (COMMITTEE, 200), (ADMIN, 200), (U("bob"), 403)):
        assert client.get(f"{url}/{s['id']}", headers=who).status_code == code
    assert client.get(f"{url}/none", headers=PO).status_code == 404
    for who, count in ((U("alice"), "1"), (PO, "1"), (COMMITTEE, "1"), (U("bob"), "0")):
        assert client.get(url, headers=who, params={"status": "active", "output_port_id": world["port_id"]}).headers["X-Total-Count"] == count


def test_cancel(world):
    client = world["client"]
    s = subscribe(client, "alice", world["port_id"])
    url = f"{MESH}/subscriptions/{s['id']}/cancel"
    assert client.post(url, headers=U("bob")).status_code == 403
    assert client.post(url, headers=U("alice")).json()["status"] == "cancelled"
    assert client.post(url, headers=U("alice")).status_code == 409
    assert subscribe(client, "alice", world["port_id"])["status"] == "active"


# ---------------------------------------------------------------- メジャー版の更新
def test_major_publish_deprecates_old_and_notifies(world):
    client, cid = world["client"], world["cid"]
    subscribe(client, "alice", world["port_id"])
    r = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "bi.mart", "name": "m", "data_class": "analytical",
                                                          "sensitivity": "internal", "owner": "mart-owner"})
    assert r.status_code == 201
    assert client.post(f"{API}/lineage/edges", headers=ADMIN, json={"source": world["asset"]["qualified_name"], "target": "bi.mart"}).status_code == 201
    new = published(client, cid, "2.0.0", schema=[COLUMNS[0]], purposes=["analytics", "marketing"])
    assert new["status"] == "published"
    old = [v for v in client.get(f"{MESH}/contracts/{cid}/versions", headers=ADMIN).json() if v["version"] == "1.0.0"][0]
    assert old["status"] == "deprecated"
    remaining = datetime.fromisoformat(old["sunset_at"]) - datetime.fromisoformat(old["deprecated_at"])
    assert remaining == timedelta(days=90)
    notified = _issues(client, "alice")
    assert len(notified) == 1 and "2.0.0" in notified[0]["title"] and notified[0]["asset_id"] is None
    assert len(_issues(client, "mart-owner")) == 1
    # 影響する下流と利用者は check API でも確認できる
    body = client.post(f"{MESH}/contracts/{cid}/check", headers=U("cust"), json={}).json()
    assert body["subscribers"] == [{"subscriber": "alice", "major": 1, "purpose_id": "analytics", "status": "active"}]
    assert body["impact"]["owners_to_notify"] == ["mart-owner"]
    # deprecated のメジャー版には新規登録できず、古い版は公開できない
    subscribe(client, "bob", world["port_id"], major=1, code=409)
    publish(client, draft(client, cid, "1.1.0")["id"], code=409)
    assert subscribe(client, "bob", world["port_id"])["major"] == 2


def test_notifications_are_visible_to_subscriber_without_read(client):
    ctx = setup_contract(client, sensitivity="restricted")
    purpose(client)
    published(client, ctx["contract"]["id"], sensitivity="restricted")
    mark_published(ctx["product"]["id"])
    s = subscribe(client, "alice", ctx["port"]["id"])
    _decide(client, s["id"])
    published(client, ctx["contract"]["id"], "2.0.0", sensitivity="restricted", schema=[COLUMNS[0]])
    seen = client.get(f"{API}/issues", headers=U("alice", clearance="internal")).json()
    assert [i["assignee"] for i in seen] == ["alice"] and ctx["asset"]["qualified_name"] not in seen[0]["title"]


def test_deprecation_days_are_configurable(world):
    from edgf.config import settings
    object.__setattr__(settings, "contract_deprecation_days", 10)
    try:
        client, cid = world["client"], world["cid"]
        published(client, cid, "2.0.0", purposes=["analytics", "marketing"])
        old = [v for v in client.get(f"{MESH}/contracts/{cid}/versions", headers=ADMIN).json() if v["version"] == "1.0.0"][0]
        assert datetime.fromisoformat(old["sunset_at"]) - datetime.fromisoformat(old["deprecated_at"]) == timedelta(days=10)
    finally:
        object.__setattr__(settings, "contract_deprecation_days", 90)


def _expire(cid):
    with engine.begin() as conn:
        conn.execute(text("UPDATE contract_versions SET sunset_at = :t WHERE contract_id = :c AND status = 'deprecated'"),
                     {"t": datetime(2020, 1, 1), "c": cid})


def _retire(client, cid, major=1, who=PO):
    return client.post(f"{MESH}/contracts/{cid}/majors/{major}/retire", headers=who)


def test_retire_requires_expiry_and_no_valid_subscriptions(world):
    client, cid = world["client"], world["cid"]
    s = subscribe(client, "alice", world["port_id"])
    assert _retire(client, cid).status_code == 409  # deprecated ではない
    published(client, cid, "2.0.0", purposes=["analytics", "marketing"])
    r = _retire(client, cid)  # 期間中かつ利用登録あり
    assert r.status_code == 409 and r.json()["type"].endswith("contract-retire-blocked") and len(r.json()["blockers"]) == 2
    _expire(cid)
    r = _retire(client, cid)  # 期間は満了したが利用登録が残っている
    assert r.status_code == 409 and len(r.json()["blockers"]) == 1 and "利用登録" in r.json()["blockers"][0]
    # 移行すると retire できる
    moved = client.post(f"{MESH}/subscriptions/{s['id']}/migrate", headers=U("alice"), json={"major": 2})
    assert moved.json()["major"] == 2 and moved.json()["status"] == "active"
    for who in (STEWARD, COMMITTEE, ADMIN):
        assert _retire(client, cid, who=who).status_code == 403
    done = _retire(client, cid)
    assert done.status_code == 200 and done.json()[0]["status"] == "retired"
    assert _retire(client, cid).status_code == 409
    assert _retire(client, cid, major=9).status_code == 422
    assert _retire(client, cid, major=2).status_code == 409  # 現行は不可
    assert _retire(client, "none").status_code == 404
    client_events = client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000}).json()
    assert {"mesh.subscription.migrate", "mesh.contract.retire"} <= {e["action"] for e in client_events}


def test_retire_during_period_blocked_only_by_period(world):
    client, cid = world["client"], world["cid"]
    published(client, cid, "2.0.0", purposes=["analytics", "marketing"])
    r = _retire(client, cid)
    assert r.status_code == 409 and r.json()["blockers"][0].startswith("併存期間")


def test_committee_can_shorten_period_with_reason(world):
    client, cid = world["client"], world["cid"]
    published(client, cid, "2.0.0", purposes=["analytics", "marketing"])
    url = f"{MESH}/contracts/{cid}/majors/1/expedite-retire"
    for who in (PO, ADMIN, STEWARD):
        assert client.post(url, headers=who, json={"reason": "脆弱性"}).status_code == 403
    assert client.post(url, headers=COMMITTEE, json={}).status_code == 422  # 理由なし
    assert client.post(url, headers=COMMITTEE, json={"reason": "  "}).status_code == 422
    late = (datetime.now(timezone.utc) + timedelta(days=400)).isoformat()
    assert client.post(url, headers=COMMITTEE, json={"reason": "r", "sunset_at": late}).status_code == 422  # 延長は不可
    assert client.post(f"{MESH}/contracts/{cid}/majors/2/expedite-retire", headers=COMMITTEE, json={"reason": "r"}).status_code == 409
    assert client.post(f"{MESH}/contracts/none/majors/1/expedite-retire", headers=COMMITTEE, json={"reason": "r"}).status_code == 404
    assert client.post(url, headers=COMMITTEE, json={"reason": "脆弱性の対応"}).status_code == 200
    old = [v for v in client.get(f"{MESH}/contracts/{cid}/versions", headers=ADMIN).json() if v["version"] == "1.0.0"][0]
    assert old["sunset_shortened_by"] == "chair" and old["sunset_shortened_reason"] == "脆弱性の対応"
    assert _retire(client, cid).status_code == 200
    events = client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000}).json()
    assert any(e["action"] == "mesh.contract.expedite_retire" and e["payload"]["reason"] == "脆弱性の対応" for e in events)


def test_shortening_does_not_bypass_valid_subscriptions(world):
    client, cid = world["client"], world["cid"]
    subscribe(client, "alice", world["port_id"])
    published(client, cid, "2.0.0", purposes=["analytics", "marketing"])
    assert client.post(f"{MESH}/contracts/{cid}/majors/1/expedite-retire", headers=COMMITTEE, json={"reason": "r"}).status_code == 200
    assert _retire(client, cid).status_code == 409


# ---------------------------------------------------------------- 移行
def test_migrate_rules(world):
    client, cid = world["client"], world["cid"]
    s = subscribe(client, "alice", world["port_id"])
    url = f"{MESH}/subscriptions/{s['id']}/migrate"
    assert client.post(url, headers=U("alice"), json={"major": 9}).status_code == 409
    assert client.post(url, headers=U("bob"), json={"major": 1}).status_code == 403
    published(client, cid, "2.0.0", purposes=["analytics", "marketing"])
    assert client.post(url, headers=U("alice"), json={"major": 1}).status_code == 409  # deprecated へは不可
    assert client.post(url, headers=U("alice"), json={"major": 2, "purpose_id": "none"}).status_code == 422
    r = client.post(url, headers=PO, json={"major": 2, "purpose_id": "marketing"})  # Owner も付け替えできる
    assert r.json()["major"] == 2 and r.json()["purpose_id"] == "marketing"
    client.post(f"{MESH}/subscriptions/{s['id']}/cancel", headers=U("alice"))
    assert client.post(url, headers=U("alice"), json={"major": 2}).status_code == 409


def test_migration_to_higher_sensitivity_needs_new_approval(world):
    client, cid = world["client"], world["cid"]
    s = subscribe(client, "alice", world["port_id"])
    published(client, cid, "2.0.0", sensitivity="confidential", purposes=["analytics", "marketing"])
    r = client.post(f"{MESH}/subscriptions/{s['id']}/migrate", headers=U("alice"), json={"major": 2})
    assert r.json()["status"] == "pending" and r.json()["decided_by"] is None
    assert _decide(client, s["id"]).json()["status"] == "active"


def test_migration_keeps_approval_when_sensitivity_is_unchanged(client):
    ctx = setup_contract(client, sensitivity="confidential")
    purpose(client)
    cid = ctx["contract"]["id"]
    published(client, cid, sensitivity="confidential")
    mark_published(ctx["product"]["id"])
    s = subscribe(client, "alice", ctx["port"]["id"])
    _decide(client, s["id"])
    published(client, cid, "2.0.0", sensitivity="confidential", schema=[COLUMNS[0]])
    r = client.post(f"{MESH}/subscriptions/{s['id']}/migrate", headers=U("alice"), json={"major": 2})
    assert r.json()["status"] == "active" and r.json()["decided_by"] == "po"


# ---------------------------------------------------------------- 停止
def test_withdrawing_purpose_suspends_subscriptions(world):
    client = world["client"]
    s = subscribe(client, "alice", world["port_id"])
    other = subscribe(client, "bob", world["port_id"], purpose_id="marketing")
    url = f"{API}/privacy/purposes/analytics/withdraw"
    assert client.post(url, headers=U("x", "owner"), json={"reason": "r"}).status_code == 403
    assert client.post(url, headers=COMMITTEE, json={}).status_code == 422
    r = client.post(url, headers=COMMITTEE, json={"reason": "事業の終了"})
    assert r.status_code == 200 and r.json()["subscriptions_suspended"] == [s["id"]]
    assert r.json()["purpose"]["withdrawn_reason"] == "事業の終了"
    got = client.get(f"{MESH}/subscriptions/{s['id']}", headers=U("alice")).json()
    assert got["status"] == "suspended" and "analytics" in got["suspended_reason"]
    assert client.get(f"{MESH}/subscriptions/{other['id']}", headers=U("bob")).json()["status"] == "active"
    assert len(_issues(client, "alice")) == 1
    assert client.post(url, headers=COMMITTEE, json={"reason": "r"}).status_code == 409
    assert client.post(f"{API}/privacy/purposes/none/withdraw", headers=COMMITTEE, json={"reason": "r"}).status_code == 404
    # 撤回後は新しい利用登録を受け付けない。別の目的に変えれば再開できる
    subscribe(client, "carol", world["port_id"], code=422)
    r = client.post(f"{MESH}/subscriptions/{s['id']}/migrate", headers=U("alice"), json={"major": 1})
    assert r.status_code == 422
    r = client.post(f"{MESH}/subscriptions/{s['id']}/migrate", headers=U("alice"), json={"major": 1, "purpose_id": "marketing"})
    assert r.json()["status"] == "active" and r.json()["suspended_reason"] is None


def test_withdrawn_purpose_blocks_consent_but_subject_withdrawal_does_not_suspend(world):
    """主体ごとの同意の撤回では、利用登録は止めない (1 人の撤回で全員を止めないため)。"""
    client = world["client"]
    client.post(f"{API}/privacy/purposes", headers=COMMITTEE, json={"id": "opt", "description": "d", "default_legal_basis": "consent"})
    consent = {"identifier_type": "email", "identifier": "a@example.com", "purpose_id": "opt"}
    assert client.post(f"{API}/privacy/consents", headers=COMMITTEE, json={**consent, "status": "granted"}).status_code == 201
    published(client, world["cid"], "1.1.0", purposes=["analytics", "marketing", "opt"])
    s = subscribe(client, "alice", world["port_id"], purpose_id="opt")
    assert client.post(f"{API}/privacy/consents", headers=COMMITTEE, json={**consent, "status": "withdrawn"}).status_code == 201
    assert client.get(f"{MESH}/subscriptions/{s['id']}", headers=U("alice")).json()["status"] == "active"
    assert client.post(f"{API}/privacy/purposes/opt/withdraw", headers=COMMITTEE, json={"reason": "r"}).status_code == 200
    assert client.post(f"{API}/privacy/consents", headers=COMMITTEE, json={**consent, "status": "granted"}).status_code == 409
    assert client.post(f"{API}/privacy/consents", headers=COMMITTEE, json={**consent, "status": "withdrawn"}).status_code == 201
    chk = client.post(f"{API}/privacy/consents/check", headers=COMMITTEE, json={"identifier_type": "email", "identifier": "a@example.com", "purpose_id": "opt"})
    assert chk.json()["allowed"] is False and "撤回" in chk.json()["reason"]


def test_narrowed_usage_suspends_affected_subscriptions(world):
    client, cid = world["client"], world["cid"]
    keep = subscribe(client, "alice", world["port_id"])
    drop = subscribe(client, "bob", world["port_id"], purpose_id="marketing")
    published(client, cid, "2.0.0", purposes=["analytics"])
    assert client.get(f"{MESH}/subscriptions/{keep['id']}", headers=U("alice")).json()["status"] == "active"
    got = client.get(f"{MESH}/subscriptions/{drop['id']}", headers=U("bob")).json()
    assert got["status"] == "suspended" and "marketing" in got["suspended_reason"]
    assert any("縮小" in i["title"] for i in _issues(client, "bob"))
    # suspended は有効な利用登録に数えない
    assert client.post(f"{MESH}/contracts/{cid}/check", headers=U("cust"), json={}).json()["subscribers"][0]["subscriber"] == "alice"
    assert len(client.post(f"{MESH}/contracts/{cid}/check", headers=U("cust"), json={}).json()["subscribers"]) == 1
