"""Phase 15d: 利用実績 (集計値) の取り込み、未使用の検出、Usage の KPI."""
from __future__ import annotations

from datetime import timedelta

import pytest

from edgf.db import SessionLocal
from edgf.jobs import run_job
from edgf.models import DataAsset, utcnow
from edgf.services import usage
from tests.process_helpers import ADMIN, API, U, asset, issues_of

PUSH = f"{API}/usage/daily"
DP = U("dp-sales", "custodian", "sales")


def today(days_ago=0):
    return (utcnow().date() - timedelta(days=days_ago)).isoformat()


def rec(name, days_ago=1, queries=10, users=3, **kw):
    return {"qualified_name": name, "day": today(days_ago), "query_count": queries, "user_count": users, **kw}


def push(client, records, who=DP, code=200):
    r = client.post(PUSH, headers=who, json={"records": records})
    assert r.status_code == code, r.text
    return r.json()


@pytest.fixture()
def sales(client):
    for name in ("sales", "hr"):
        client.post(f"{API}/domains", headers=ADMIN, json={"name": name})
    a = asset(client, "sales.orders", domain="sales")
    b = asset(client, "sales.items", domain="sales")
    return client, a, b


def _age(asset_id, days):
    with SessionLocal() as db:
        db.get(DataAsset, asset_id).created_at = utcnow() - timedelta(days=days)
        db.commit()


def test_push_and_resend_overwrites(sales):
    client, a, _ = sales
    assert push(client, [rec("sales.orders"), rec("sales.orders", 2)]) == {"created": 2, "updated": 0, "unregistered": []}
    assert push(client, [rec("sales.orders", queries=50, users=7)]) == {"created": 0, "updated": 1, "unregistered": []}
    k = client.get(f"{API}/kpi", headers=ADMIN).json()
    assert k["usage_query_count_30d"] == 60 and k["usage_active_assets_30d"] == 1
    # 同じ本文の中の重複は後勝ち
    push(client, [rec("sales.orders", 3, queries=1, users=1), rec("sales.orders", 3, queries=4, users=1)])
    assert client.get(f"{API}/kpi", headers=ADMIN).json()["usage_query_count_30d"] == 64


def test_unregistered_assets_are_reported_not_stored(sales):
    client, _, _ = sales
    assert push(client, [rec("sales.unknown")]) == {"created": 0, "updated": 0, "unregistered": ["sales.unknown"]}


@pytest.mark.parametrize("field", ["user_id", "users", "user_ids", "email", "subject", "client_ip", "query_text", "sql", "session_id"])
def test_user_identifiers_are_rejected(sales, field):
    client, _, _ = sales
    r = client.post(PUSH, headers=DP, json={"records": [rec("sales.orders", **{field: ["zzuser-1"]})]})
    assert r.status_code == 422 and "zzuser-1" not in r.text


@pytest.mark.parametrize("bad", [rec("sales.orders", queries=-1), rec("sales.orders", users=5, queries=2), rec("sales.orders", days_ago=-1),
                                 rec("sales.orders", days_ago=401), {**rec("sales.orders"), "extra": 1}])
def test_invalid_records(sales, bad):
    client, _, _ = sales
    assert client.post(PUSH, headers=DP, json={"records": [bad]}).status_code == 422
    assert client.post(PUSH, headers=DP, json={"records": []}).status_code == 422


def test_authorization(sales):
    client, _, _ = sales
    for who in (U("dp-hr", "custodian", "hr"), U("x", "custodian", ""), U("reader"), U("o", "owner", "sales")):
        assert client.post(PUSH, headers=who, json={"records": [rec("sales.orders")]}).status_code == 403
    assert push(client, [rec("sales.orders")], who=ADMIN)["created"] == 1


def test_kpi_is_none_until_usage_arrives(sales):
    client, _, _ = sales
    k = client.get(f"{API}/kpi", headers=ADMIN).json()
    assert k["usage_active_assets_30d"] is None and k["unused_asset_count"] is None and "governance_activity_assets_30d" in k


def test_unused_detection_and_job(sales):
    client, a, b = sales
    _age(a["id"], 200)
    _age(b["id"], 200)
    assert run_job("unused_assets")["result"] == {"candidates": 0, "opened": 0}  # 利用実績が無ければ判定しない
    push(client, [rec("sales.orders", 5), rec("sales.items", 95)])
    k = client.get(f"{API}/kpi", headers=ADMIN).json()
    assert k["unused_asset_count"] == 1 and k["usage_active_assets_30d"] == 1
    first = run_job("unused_assets")["result"]
    assert first == {"candidates": 1, "opened": 1}
    opened = issues_of(client, "usage-unused")
    assert len(opened) == 1 and opened[0]["asset_id"] == b["id"] and opened[0]["assignee"] == "asset-owner" and opened[0]["kind"] == "usage"
    assert run_job("unused_assets")["result"] == {"candidates": 1, "opened": 0}  # 重複起票しない
    # 問い合わせ 0 件の日は利用とみなさない
    push(client, [rec("sales.items", 1, queries=0, users=0)])
    assert client.get(f"{API}/kpi", headers=ADMIN).json()["unused_asset_count"] == 1


def test_new_and_inactive_assets_are_not_candidates(sales):
    client, a, b = sales
    push(client, [rec("sales.orders")])
    _age(b["id"], 30)  # 登録から日が浅い
    with SessionLocal() as db:
        assert [x.qualified_name for x in usage.unused_assets(db)] == []
        db.get(DataAsset, a["id"]).created_at = utcnow() - timedelta(days=200)
        db.get(DataAsset, a["id"]).lifecycle_state = "archived"
        db.commit()
        assert usage.unused_assets(db) == []


def test_domain_scoped_usage_kpi(sales):
    client, _, _ = sales
    hr = asset(client, "hr.people", domain="hr")
    push(client, [rec("sales.orders", queries=5)])
    push(client, [rec("hr.people", queries=7)], who=ADMIN)
    by_domain = {d["scope"]: d for d in client.get(f"{API}/kpi/domains", headers=ADMIN).json()}
    assert by_domain["sales"]["usage_query_count_30d"] == 5 and by_domain["hr"]["usage_query_count_30d"] == 7
    assert hr["id"]
