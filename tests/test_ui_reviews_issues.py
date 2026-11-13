"""Phase 17d: 棚卸と証明、Issue の更新を UI の経路で行う (確定事項 6 の ②③)."""
from __future__ import annotations

from sqlalchemy import select

from edgf.db import SessionLocal
from edgf.models import AuditEvent
from edgf.services import reviews
from tests.process_helpers import ADMIN, API, U, asset
from tests.test_reviews import _campaign, _grant, _items
from tests.ui_helpers import as_ui

REVIEWS = f"{API}/reviews"
OWNER_UI = ("asset-owner", "owner", "sales")


def payloads(action):
    with SessionLocal() as db:
        return [e.payload for e in db.scalars(select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.seq))]


def pending(ui) -> list[dict]:
    r = ui.get(f"{REVIEWS}/items", params={"status": "pending"})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 権限の棚卸
def test_access_review_keep_on_ui_path(client):
    a = asset(client, sensitivity="restricted")
    _grant(client, a["id"])
    _campaign()
    ui = as_ui(client.app, *OWNER_UI)
    [item] = pending(ui)
    r = ui.post(f"{REVIEWS}/items/{item['id']}/decision", json={"keep": True, "comment": "継続"})
    assert r.status_code == 200 and r.json()["status"] == "kept"
    assert pending(ui) == []
    decision = payloads("approval.decision")[-1]
    assert decision["channel"] == "ui" and decision["type"] == "access_review"


def test_access_review_revoke_on_ui_path_requires_reason(client):
    a = asset(client, sensitivity="restricted")
    _grant(client, a["id"])
    _campaign()
    ui = as_ui(client.app, *OWNER_UI)
    [item] = pending(ui)
    assert ui.post(f"{REVIEWS}/items/{item['id']}/decision", json={"keep": False}).status_code == 422
    r = ui.post(f"{REVIEWS}/items/{item['id']}/decision", json={"keep": False, "comment": "異動した"})
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert client.get(f"{API}/assets/{a['id']}", headers=U("alice", clearance="confidential")).status_code == 403
    revoke = payloads("access.revoke")[-1]
    assert revoke["channel"] == "ui"


def test_access_review_authorization_is_the_same_as_api(client):
    a = asset(client, sensitivity="restricted")
    _grant(client, a["id"])
    _campaign()
    [item] = _items(client)
    for who in (("chair", "committee", ""), ("asset-custodian", "custodian", "*"), ("alice", "", "")):
        via_ui = as_ui(client.app, *who).post(f"{REVIEWS}/items/{item['id']}/decision", json={"keep": True})
        via_api = client.post(f"{REVIEWS}/items/{item['id']}/decision", headers=U(*who), json={"keep": True})
        assert via_ui.status_code == via_api.status_code == 403


def test_review_decision_needs_step_up(client):
    a = asset(client, sensitivity="restricted")
    _grant(client, a["id"])
    _campaign()
    stale = as_ui(client.app, *OWNER_UI, auth_age_minutes=60)
    [item] = pending(stale)
    r = stale.post(f"{REVIEWS}/items/{item['id']}/decision", json={"keep": True})
    assert r.status_code == 401 and r.json()["type"].endswith("step-up-required")


# ---------------------------------------------------------------- Owner の証明
def test_ownership_attestation_on_ui_path(client):
    a = asset(client)
    b = asset(client, "sales.b")
    _campaign(reviews.OWNERSHIP)
    ui = as_ui(client.app, *OWNER_UI)
    items = {i["asset_id"]: i for i in pending(ui)}
    assert {i["target_type"] for i in items.values()} == {"asset"}
    r = ui.post(f"{REVIEWS}/items/{items[a['id']]['id']}/decision", json={"keep": True})
    assert r.status_code == 200 and r.json()["status"] == "attested"
    assert ui.post(f"{REVIEWS}/items/{items[b['id']]['id']}/decision", json={"keep": False}).status_code == 422
    r = ui.post(f"{REVIEWS}/items/{items[b['id']]['id']}/decision", json={"keep": False, "comment": "Steward が異動した"})
    assert r.status_code == 200 and r.json()["status"] == "disputed"
    assert all(p["channel"] == "ui" for p in payloads("approval.decision")[-2:])


# ---------------------------------------------------------------- Issue の状態と担当者
def test_issue_status_and_assignee_on_ui_path(client):
    a = asset(client)
    issue = client.post(f"{API}/issues", headers=ADMIN, json={"kind": "other", "title": "UI で更新", "asset_id": a["id"]}).json()
    ui = as_ui(client.app, *OWNER_UI)
    r = ui.patch(f"{API}/issues/{issue['id']}", json={"status": "in_progress", "assignee": "asset-steward", "comment": "対応します"})
    assert r.status_code == 200 and r.json()["status"] == "in_progress" and r.json()["assignee"] == "asset-steward"
    payload = payloads("issue.update")[-1]
    assert payload["channel"] == "ui" and payload["status"] == "in_progress" and payload["assignee"] == "asset-steward"
    # 担当者になった人は更新できる。無関係の人は API と同じく 403
    steward = as_ui(client.app, "asset-steward")
    assert steward.patch(f"{API}/issues/{issue['id']}", json={"status": "resolved"}).json()["resolved_at"]
    stranger = as_ui(client.app, "stranger")
    via_ui = stranger.patch(f"{API}/issues/{issue['id']}", json={"status": "closed"})
    via_api = client.patch(f"{API}/issues/{issue['id']}", headers=U("stranger"), json={"status": "closed"})
    assert via_ui.status_code == via_api.status_code == 403
