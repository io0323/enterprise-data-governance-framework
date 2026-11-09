"""Phase 12d (§16 Review / §11 定期棚卸)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from edgf.config import settings
from edgf.db import SessionLocal, engine
from edgf.models import utcnow
from edgf.services import approvals, reviews
from tests.mesh_helpers import mark_published, published, purpose, setup_contract, subscribe
from tests.process_helpers import ADMIN, API, APPROVALS, COMMITTEE, OWNER, U, asset, audit_actions, future, inbox, issues_of

REVIEWS = f"{API}/reviews"
ACTOR = "system:jobs"


def _grant(client, asset_id, subject="alice") -> dict:
    r = client.post(f"{API}/assets/{asset_id}/access-grants", headers=OWNER, json={"subject": subject, "purpose": "分析", "expires_at": future(300)})
    assert r.status_code == 201, r.text
    return r.json()


def _campaign(kind=reviews.ACCESS, now=None):
    with SessionLocal() as db:
        c = reviews.ensure_campaign(db, ACTOR, kind, now)
        db.commit()
    return c


def _process(now=None) -> dict:
    with SessionLocal() as db:
        out = reviews.process_deadlines(db, ACTOR, now)
        db.commit()
    return out


def _items(client, who=COMMITTEE, **params) -> list[dict]:
    r = client.get(f"{REVIEWS}/items", headers=who, params={"limit": 1000, **params})
    assert r.status_code == 200, r.text
    return r.json()


def _decide(client, item_id, keep=True, comment="", who=OWNER):
    return client.post(f"{REVIEWS}/items/{item_id}/decision", headers=who, json={"keep": keep, "comment": comment})


def _deadlines(campaign):
    return campaign.due_at + timedelta(seconds=1), campaign.grace_until + timedelta(seconds=1)


# ---------------------------------------------------------------- キャンペーンの作成
def test_periods():
    assert reviews.period_of(reviews.ACCESS, datetime(2026, 1, 1)) == "2026-Q1" and reviews.period_of(reviews.ACCESS, datetime(2026, 12, 31)) == "2026-Q4"
    assert reviews.period_of(reviews.OWNERSHIP, datetime(2026, 12, 31)) == "2026"


def test_access_review_lists_grants_and_subscriptions(client):
    a = asset(client, sensitivity="restricted")
    g = _grant(client, a["id"])
    revoked = _grant(client, a["id"], "bob")
    assert client.delete(f"{API}/assets/{a['id']}/access-grants/{revoked['id']}", headers=OWNER).status_code == 200
    ctx = setup_contract(client)
    purpose(client)
    published(client, ctx["contract"]["id"])
    mark_published(ctx["product"]["id"])
    sub = subscribe(client, "carol", ctx["port"]["id"])
    c = _campaign()
    assert c.kind == "access_review" and c.item_count == 2 and c.status == "open"
    items = {i["target_id"]: i for i in _items(client)}
    assert set(items) == {g["id"], sub["id"]}
    assert items[g["id"]]["target_type"] == "access_grant" and items[sub["id"]]["asset_id"] == ctx["asset"]["id"]
    # 同じ期間には作らない (冪等)
    assert _campaign() is None
    assert client.get(f"{REVIEWS}/campaigns", headers=COMMITTEE, params={"kind": "access_review", "status": "open"}).headers["X-Total-Count"] == "1"
    # 次の四半期には作る
    assert _campaign(now=utcnow() + timedelta(days=100)) is not None


def test_review_items_appear_in_owner_inbox(client):
    a = asset(client)
    _grant(client, a["id"])
    _campaign()
    item = _items(client)[0]
    assert item["approval_id"] in {i["id"] for i in inbox(client, OWNER)}
    assert inbox(client, U("stranger")) == []


def test_item_visibility(client):
    a = asset(client)
    b = asset(client, "sales.other", owner="other-owner")
    _grant(client, a["id"])
    assert client.post(f"{API}/assets/{b['id']}/access-grants", headers=U("other-owner"), json={"subject": "bob", "purpose": "p", "expires_at": future()}).status_code == 201
    _campaign()
    assert len(_items(client, COMMITTEE)) == 2 and len(_items(client, OWNER)) == 1 and _items(client, U("nobody")) == []
    assert client.get(f"{REVIEWS}/campaigns", headers=OWNER).status_code == 403


# ---------------------------------------------------------------- 判断
def test_owner_keeps_grant(client):
    a = asset(client, sensitivity="restricted")
    _grant(client, a["id"])
    _campaign()
    item = _items(client)[0]
    for who in (COMMITTEE, ADMIN, U("asset-custodian", "custodian", "*"), U("alice")):
        assert _decide(client, item["id"], who=who).status_code == 403
    r = _decide(client, item["id"], comment="継続")
    assert r.status_code == 200 and r.json()["status"] == "kept" and r.json()["decided_by"] == "asset-owner"
    assert client.get(f"{API}/assets/{a['id']}", headers=U("alice", clearance="confidential")).status_code == 200
    assert _decide(client, item["id"]).status_code == 409
    assert _decide(client, "none").status_code == 404
    assert client.get(f"{APPROVALS}/{item['approval_id']}", headers=OWNER).json()["status"] == "approved"


def test_owner_revokes_grant(client):
    a = asset(client, sensitivity="restricted")
    g = _grant(client, a["id"])
    _campaign()
    item = _items(client)[0]
    assert _decide(client, item["id"], keep=False).status_code == 422  # 理由は必須
    r = _decide(client, item["id"], keep=False, comment="異動した")
    assert r.json()["status"] == "revoked"
    assert client.get(f"{API}/assets/{a['id']}", headers=U("alice", clearance="confidential")).status_code == 403
    grants = client.get(f"{API}/assets/{a['id']}/access-grants", headers=OWNER, params={"include_inactive": True}).json()
    assert grants[0]["revoked_by"] == "asset-owner"
    events = [e for e in audit_actions(client) if e["action"] == "access.revoke" and e["payload"].get("target_id") == g["id"]]
    assert events[0]["payload"]["reason"] == "棚卸で取り消し"


def test_revoking_subscription_item_cancels_it(client):
    ctx = setup_contract(client, owner="asset-owner")
    purpose(client)
    published(client, ctx["contract"]["id"])
    mark_published(ctx["product"]["id"])
    sub = subscribe(client, "carol", ctx["port"]["id"])
    _campaign()
    item = _items(client)[0]
    assert _decide(client, item["id"], keep=False, comment="目的が終わった").status_code == 200
    got = client.get(f"{API}/mesh/subscriptions/{sub['id']}", headers=U("carol")).json()
    assert got["status"] == "cancelled"


def test_already_revoked_target_is_a_no_op(client):
    a = asset(client)
    g = _grant(client, a["id"])
    _campaign()
    assert client.delete(f"{API}/assets/{a['id']}/access-grants/{g['id']}", headers=OWNER).status_code == 200
    item = _items(client)[0]
    assert _decide(client, item["id"], keep=False, comment="r").json()["status"] == "revoked"
    assert len([e for e in audit_actions(client) if e["action"] == "access.revoke"]) == 1


def test_decision_through_approval_api_also_closes_item(client):
    a = asset(client)
    _grant(client, a["id"])
    _campaign()
    item = _items(client)[0]
    r = client.post(f"{APPROVALS}/{item['approval_id']}/decision", headers=OWNER, json={"approve": True})
    assert r.status_code == 200
    assert _items(client)[0]["status"] == "kept"
    # 取り消し
    other = asset(client, "sales.next")
    _grant(client, other["id"], "bob")
    _campaign(now=utcnow() + timedelta(days=100))
    pending = _items(client, status="pending")[0]
    assert client.post(f"{APPROVALS}/{pending['approval_id']}/cancel", headers=ADMIN).json()["status"] == "cancelled"
    assert next(i for i in _items(client) if i["id"] == pending["id"])["status"] == "cancelled"


# ---------------------------------------------------------------- 期限と猶予
def test_overdue_issue_then_grace_auto_revoke(client):
    a = asset(client, sensitivity="restricted")
    g = _grant(client, a["id"])
    c = _campaign()
    assert c.grace_until - c.due_at == timedelta(days=settings.access_review_grace_days)
    alice = U("alice", clearance="confidential")
    past_due, past_grace = _deadlines(c)

    # 期限内は何もしない
    assert _process()["issues_opened"] == 0 and issues_of(client, prefix="review:") == []

    # 期限を過ぎると Issue。権限はまだ残る
    first = _process(past_due)
    assert first["issues_opened"] == 1 and first["auto_revoked"] == 0
    issue = issues_of(client, prefix="review:")[0]
    assert issue["assignee"] == "asset-owner" and issue["escalation_level"] == "owner"
    assert client.get(f"{API}/assets/{a['id']}", headers=alice).status_code == 200
    # 重複起票しない
    assert _process(past_due)["issues_opened"] == 0

    # 猶予を過ぎると自動で取り消し、監査に記録し、エスカレーションする
    done = _process(past_grace)
    assert done["auto_revoked"] == 1 and done["issues_escalated"] == 1 and done["campaigns_closed"] == 1
    assert client.get(f"{API}/assets/{a['id']}", headers=alice).status_code == 403
    item = _items(client)[0]
    assert item["status"] == "auto_revoked"
    assert client.get(f"{APPROVALS}/{item['approval_id']}", headers=COMMITTEE).json()["status"] == "expired"
    assert issues_of(client, prefix="review:")[0]["escalation_level"] == "committee"
    audit = audit_actions(client)
    assert any(e["action"] == "review.auto_revoke" and e["payload"]["target_id"] == g["id"] for e in audit)
    assert any(e["action"] == "access.revoke" and e["actor"] == ACTOR for e in audit)
    # 2 回実行しても結果は変わらない
    assert _process(past_grace) == {"issues_opened": 0, "issues_escalated": 0, "auto_revoked": 0, "unattested": 0, "campaigns_closed": 0}
    assert len([e for e in audit_actions(client) if e["action"] == "review.auto_revoke"]) == 1


def test_grace_days_are_configurable(client):
    object.__setattr__(settings, "access_review_grace_days", 3)
    try:
        c = _campaign()
        assert c.grace_until - c.due_at == timedelta(days=3)
    finally:
        object.__setattr__(settings, "access_review_grace_days", 14)


def test_decision_resolves_overdue_issue(client):
    a = asset(client)
    _grant(client, a["id"])
    c = _campaign()
    _process(_deadlines(c)[0])
    assert issues_of(client, prefix="review:")[0]["status"] == "open"
    assert _decide(client, _items(client)[0]["id"]).status_code == 200
    assert issues_of(client, prefix="review:")[0]["status"] == "resolved"
    assert _process(_deadlines(c)[1])["auto_revoked"] == 0  # 判断済みは取り消さない


def test_generic_sla_job_leaves_review_items_to_campaign(client):
    a = asset(client)
    _grant(client, a["id"])
    _campaign()
    with engine.begin() as conn:
        conn.execute(text("UPDATE approval_requests SET due_at = :t"), {"t": datetime(2020, 1, 1)})
    with SessionLocal() as db:
        assert approvals.escalate_overdue(db, ACTOR)["overdue"] == []
        db.commit()


def test_empty_campaign_closes_after_due(client):
    c = _campaign()
    assert c.item_count == 0
    assert _process()["campaigns_closed"] == 0
    assert _process(_deadlines(c)[0])["campaigns_closed"] == 1


def test_campaign_closes_when_all_decided(client):
    a = asset(client)
    _grant(client, a["id"])
    _campaign()
    _decide(client, _items(client)[0]["id"])
    assert _process()["campaigns_closed"] == 1
    assert client.get(f"{REVIEWS}/campaigns", headers=COMMITTEE, params={"status": "closed"}).headers["X-Total-Count"] == "1"


# ---------------------------------------------------------------- Owner の証明
def test_ownership_attestation(client):
    a = asset(client)
    b = asset(client, "sales.b")
    deleted = asset(client, "sales.gone")
    assert client.post(f"{API}/assets/{deleted['id']}/lifecycle", headers=COMMITTEE, json={"target_state": "deleted"}).status_code == 200
    c = _campaign(reviews.OWNERSHIP)
    assert c.item_count == 2 and c.period == str(utcnow().year)
    req = client.get(f"{APPROVALS}/{_items(client)[0]['approval_id']}", headers=COMMITTEE).json()
    assert req["type"] == "ownership_attestation" and {"owner", "steward", "sensitivity", "retention_days"} == set(req["payload"])
    items = {i["asset_id"]: i for i in _items(client)}
    assert _decide(client, items[a["id"]]["id"]).json()["status"] == "attested"
    r = _decide(client, items[b["id"]]["id"], keep=False, comment="Steward が異動した")
    assert r.json()["status"] == "disputed"
    assert issues_of(client, f"review-dispute:{items[b['id']]['id']}")[0]["asset_id"] == b["id"]


def test_unattested_is_escalated_but_nothing_is_revoked(client):
    a = asset(client)
    g = _grant(client, a["id"])
    c = _campaign(reviews.OWNERSHIP)
    past_due, past_grace = _deadlines(c)
    assert _process(past_due)["issues_opened"] == 1
    done = _process(past_grace)
    assert done["unattested"] == 1 and done["auto_revoked"] == 0 and done["issues_escalated"] == 1
    assert _items(client)[0]["status"] == "unattested"
    grants = client.get(f"{API}/assets/{a['id']}/access-grants", headers=OWNER).json()
    assert [x["id"] for x in grants] == [g["id"]]


# ---------------------------------------------------------------- Policy の見直し
def _scan(now) -> list[str]:
    with SessionLocal() as db:
        out = reviews.policy_review_scan(db, ACTOR, now)
        db.commit()
    return out


def test_policy_review_issue_after_a_year(client):
    assert _scan(utcnow()) == []
    later = utcnow() + timedelta(days=366)
    opened = _scan(later)
    assert len(opened) >= 10 and _scan(later) == []  # 重複起票しない
    issue = issues_of(client, "policy-review:GOV-001")[0]
    assert issue["escalation_level"] == "committee" and issue["assignee"] == "data-governance-committee"


def test_policy_review_record_resolves_issue(client):
    later = utcnow() + timedelta(days=366)
    _scan(later)
    url = f"{API}/policies/GOV-001/review"
    assert client.post(url, headers=OWNER, json={}).status_code == 403
    assert client.post(url, headers=ADMIN, json={}).status_code == 403
    assert client.post(f"{API}/policies/NONE/review", headers=COMMITTEE, json={}).status_code == 404
    assert client.post(url, headers=COMMITTEE, json={"note": "変更なし"}).status_code == 201
    assert issues_of(client, "policy-review:GOV-001")[0]["status"] == "resolved"
    assert any(e["action"] == "policy.review" for e in audit_actions(client))
    # 記録の日時から 1 年は起票しない (ここでは記録を過去に戻して確認する)
    with engine.begin() as conn:
        conn.execute(text("UPDATE policy_reviews SET reviewed_at = :t"), {"t": later})
    assert "GOV-001" not in {i["source_ref"].split(":")[1] for i in issues_of(client, prefix="policy-review:") if i["status"] == "open"}
    assert _scan(later + timedelta(days=10)) == []


def test_changed_policy_resets_review_clock(client):
    import yaml
    files = client.get(f"{API}/policies/bundles/1", headers=ADMIN).json()["files"]["global.yaml"]
    doc = yaml.safe_load(files)
    doc["policies"][0]["severity"] = "high"
    p = client.post(f"{API}/policies/bundles", headers=U("proposer", "committee"), json={"changes": {"global.yaml": yaml.safe_dump(doc, allow_unicode=True)}, "reason": "r"}).json()
    client.post(f"{APPROVALS}/{p['approval_id']}/decision", headers=COMMITTEE, json={"approve": True})
    assert client.post(f"{API}/policies/bundles/{p['proposal_version']}/publish", headers=COMMITTEE).status_code == 200
    with engine.begin() as conn:
        conn.execute(text("UPDATE policy_bundles SET published_at = :t WHERE version = 1"), {"t": datetime(2020, 1, 1)})
    with SessionLocal() as db:
        reviewed = reviews.policy_reviewed_at(db)
    assert reviewed[doc["policies"][0]["id"]] > datetime(2025, 1, 1)  # 変更された Policy は新しい公開日
    assert reviewed[doc["policies"][1]["id"]] == datetime(2020, 1, 1)  # 変わっていない Policy は元のまま


def test_run_campaigns_is_idempotent(client):
    a = asset(client)
    _grant(client, a["id"])
    with SessionLocal() as db:
        first = reviews.run_campaigns(db, ACTOR)
        db.commit()
    assert set(first["campaigns_created"]) == {"access_review", "ownership_attestation", "policy_review"}
    before = client.get(f"{REVIEWS}/items", headers=COMMITTEE).headers["X-Total-Count"]
    with SessionLocal() as db:
        second = reviews.run_campaigns(db, ACTOR)
        db.commit()
    assert second["campaigns_created"] == [] and client.get(f"{REVIEWS}/items", headers=COMMITTEE).headers["X-Total-Count"] == before
