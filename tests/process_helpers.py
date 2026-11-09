"""Phase 12 のテストの共通部品。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.ai_helpers import ADMIN, API, COMMITTEE, CONTROLS, U  # noqa: F401

APPROVALS = f"{API}/approvals"
OWNER = U("asset-owner")
COMMITTEE2 = U("vice-chair", "committee")


def future(days=30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def asset(client, qn="sales.orders", *, sensitivity="internal", owner="asset-owner", domain=None, **kw) -> dict:
    r = client.post(f"{API}/assets", headers=ADMIN, json={
        "qualified_name": qn, "name": qn, "description": "d", "data_class": "transactional", "sensitivity": sensitivity,
        "owner": owner, "steward": "asset-steward", "custodian": "asset-custodian", "controls": CONTROLS, "domain": domain, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def request_access(client, asset_id, who="alice", code=201, type_name="access_request", **kw):
    body = {"type": type_name, "asset_id": asset_id, "reason": "調査のため", "purpose": "分析", "expires_at": future(), **kw}
    r = client.post(APPROVALS, headers=U(who), json=body)
    assert r.status_code == code, r.text
    return r.json()


def decide(client, approval_id, who, approve=True, comment="", code=200):
    r = client.post(f"{APPROVALS}/{approval_id}/decision", headers=who, json={"approve": approve, "comment": comment})
    assert r.status_code == code, r.text
    return r.json()


def inbox(client, who, **params) -> list[dict]:
    r = client.get(f"{APPROVALS}/inbox", headers=who, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def issues_of(client, source_ref=None, prefix=None) -> list[dict]:
    rows = client.get(f"{API}/issues", headers=ADMIN, params={"limit": 1000}).json()
    return [i for i in rows if (source_ref is None or i["source_ref"] == source_ref)
            and (prefix is None or (i["source_ref"] or "").startswith(prefix))]


def audit_actions(client, **params) -> list[dict]:
    return client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000, **params}).json()
