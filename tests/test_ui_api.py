"""Phase 13d: UI のために追加した API (KPI の推移、Data Product の概要) の認可と ABAC."""
from __future__ import annotations

from datetime import date, timedelta

from edgf.db import SessionLocal
from edgf.jobs import JOBS, run_job
from edgf.services import kpi
from tests.mesh_helpers import add_port, contract, domain, mark_published, port_asset, product, published, purpose, setup_contract, subscribe
from tests.process_helpers import ADMIN, API, COMMITTEE, U

HISTORY = f"{API}/kpi/history"


def _snapshot(day=None):
    with SessionLocal() as db:
        result = kpi.snapshot(db, day)
        db.commit()
    return result


def test_snapshot_is_idempotent_per_day(client):
    domain(client)
    assert _snapshot(date(2026, 1, 1))["created"] == 2
    assert _snapshot(date(2026, 1, 1))["created"] == 0
    rows = client.get(HISTORY, headers=ADMIN).json()
    assert len(rows) == 2 and {r["scope"] for r in rows} == {"enterprise", "sales"}
    assert "compliance_rate" in rows[0]["metrics"] and "maturity_level" in rows[0]["metrics"]
    # 新しいドメインの分だけ後から作られる
    domain(client, "bi", owner="o")
    assert _snapshot(date(2026, 1, 1))["created"] == 1
    assert _snapshot(date(2026, 1, 2))["created"] == 3


def test_snapshot_job_twice_creates_one_day(client):
    assert "kpi_snapshot" in JOBS
    first = run_job("kpi_snapshot")
    assert first["status"] == "succeeded" and first["result"]["created"] == 1
    assert run_job("kpi_snapshot")["result"]["created"] == 0
    assert client.get(HISTORY, headers=ADMIN).headers["X-Total-Count"] == "1"


def test_history_period_and_paging(client):
    start = date(2026, 3, 1)
    for i in range(5):
        _snapshot(start + timedelta(days=i))
    r = client.get(HISTORY, headers=ADMIN, params={"from": "2026-03-02", "to": "2026-03-04"})
    assert [x["day"] for x in r.json()] == ["2026-03-02", "2026-03-03", "2026-03-04"] and r.headers["X-Total-Count"] == "3"
    page = client.get(HISTORY, headers=ADMIN, params={"limit": 2, "offset": 2})
    assert [x["day"] for x in page.json()] == ["2026-03-03", "2026-03-04"] and page.headers["X-Total-Count"] == "5"
    assert 'rel="next"' in page.headers["Link"] and 'rel="prev"' in page.headers["Link"]
    assert client.get(HISTORY, headers=ADMIN, params={"from": "bad"}).status_code == 422
    assert client.get(HISTORY, headers=ADMIN, params={"limit": 0}).status_code == 422


def test_history_requires_authentication(client):
    assert client.get(HISTORY, headers={"X-Actor": "anyone"}).status_code == 200
    from fastapi.testclient import TestClient

    from edgf.main import create_app
    with TestClient(create_app()) as anonymous:
        assert anonymous.get(HISTORY).status_code == 401


def test_history_domain_breakdown_follows_abac(client):
    domain(client)
    domain(client, "secret", owner="so")
    port_asset(client, "sales.open")
    port_asset(client, "secret.only", sensitivity="restricted", domain_name="secret", owner="so")
    _snapshot(date(2026, 1, 1))

    def scopes(headers, **params):
        return {r["scope"] for r in client.get(HISTORY, headers=headers, params=params).json()}

    # 閲覧できる資産が無いドメインの内訳は返さない (全社の値は返す)
    assert scopes(U("reader", clearance="internal")) == {"enterprise", "sales"}
    # 所属するドメイン、当事者、Committee は見える
    assert scopes(U("m", "steward", "secret", clearance="internal")) == {"enterprise", "sales", "secret"}
    assert scopes(U("so")) == {"enterprise", "sales", "secret"}
    assert scopes(COMMITTEE) == scopes(ADMIN) == {"enterprise", "sales", "secret"}
    assert scopes(U("w", "owner", "*")) == {"enterprise", "sales", "secret"}
    # 絞り込み
    assert scopes(COMMITTEE, scope="enterprise") == {"enterprise"}
    assert scopes(COMMITTEE, scope="domains") == {"sales", "secret"}
    assert scopes(COMMITTEE, domain="secret") == {"secret"}
    assert scopes(U("reader", clearance="internal"), domain="secret") == set()
    assert client.get(HISTORY, headers=ADMIN, params={"scope": "bad"}).status_code == 422


def test_kpi_has_privacy_metrics_by_domain(client):
    domain(client)
    port_asset(client, "sales.people", sensitivity="confidential", contains_pii=True, retention_days=1, schema=[{"name": "email", "identifier_type": "email"}])
    r = client.post(f"{API}/privacy/erasure-requests", headers=ADMIN, json={"identifier_type": "email", "identifier": "a@example.com"}).json()
    assert client.post(f"{API}/privacy/erasure-requests/{r['id']}/scope", headers=COMMITTEE).status_code == 200
    k = client.get(f"{API}/kpi", headers=ADMIN).json()
    assert k["privacy_pii_asset_count"] == 1 and k["privacy_erasure_open_count"] == 1 and k["privacy_erasure_overdue_count"] == 0
    domain(client, "other", owner="o")
    by_domain = {d["scope"]: d for d in client.get(f"{API}/kpi/domains", headers=ADMIN).json()}
    assert by_domain["sales"]["privacy_erasure_open_count"] == 1 and by_domain["other"]["privacy_erasure_open_count"] == 0


def test_maturity_reports_next_level_conditions():
    low = kpi.maturity_indicator({"owner_assignment_rate": 0.9})
    assert low["indicative_level"] == 1 and low["level_name"] == "Basic Policy"
    nxt = low["next_level"]
    assert nxt["level"] == 2 and nxt["name"] == "Managed"
    assert nxt["conditions"] == [{"metric": "owner_assignment_rate", "minimum": 0.8, "value": 0.9, "met": True},
                                 {"metric": "quality_score_avg", "minimum": None, "value": None, "met": False}]
    top = kpi.maturity_indicator({"owner_assignment_rate": 1, "quality_score_avg": 1, "metadata_completeness": 1, "compliance_rate": 1, "certified_rate": 1})
    assert top["indicative_level"] == 4 and top["next_level"] is None
    assert kpi.maturity_indicator({})["indicative_level"] == 0


def test_products_overview(client):
    ctx = setup_contract(client)
    purpose(client)
    published(client, ctx["contract"]["id"])
    mark_published(ctx["product"]["id"])
    subscribe(client, "alice", ctx["port"]["id"])
    secret = port_asset(client, "sales.secret", sensitivity="restricted", owner="so")
    port = add_port(client, ctx["product"]["id"], secret["id"], name="secret")
    published(client, contract(client, port["id"])["id"], sensitivity="restricted")
    product(client, "empty")
    url = f"{API}/mesh/products/overview"
    r = client.get(url, headers=U("reader", clearance="internal"))
    assert r.status_code == 200 and r.headers["X-Total-Count"] == "2"
    row = next(x for x in r.json() if x["id"] == ctx["product"]["id"])
    assert row["subscription_count"] == 1 and row["datsis_passed"] is False and "MESH-003" in row["datsis_failed"]
    # 閲覧できない Output Port の Contract は出さない
    assert [c["port"] for c in row["contracts"]] == ["main"] and row["contracts"][0]["version"] == "1.0.0"
    seen = next(x for x in client.get(url, headers=COMMITTEE).json() if x["id"] == ctx["product"]["id"])
    assert sorted(c["port"] for c in seen["contracts"]) == ["main", "secret"]
    empty = next(x for x in r.json() if x["name"] == "empty")
    assert empty["contracts"] == [] and empty["subscription_count"] == 0
