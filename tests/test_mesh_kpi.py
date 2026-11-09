"""Phase 11f (§17): Data Mesh の KPI. 成熟度の判定 (§20) は変えない."""
from __future__ import annotations

from edgf.services.kpi import maturity_indicator
from tests.mesh_helpers import ADMIN, API, COLUMNS, MESH, PO, domain, draft, mark_published, product, publish, published, purpose, setup_contract, shippable, subscribe


def _kpi(client):
    r = client.get(f"{API}/kpi", headers=ADMIN)
    assert r.status_code == 200
    return r.json()


def _by_domain(client):
    return {d["scope"]: d for d in client.get(f"{API}/kpi/domains", headers=ADMIN).json()}


def test_empty(client):
    k = _kpi(client)
    assert k["mesh_product_published_count"] == 0 and k["mesh_datsis_pass_rate"] is None and k["mesh_breaking_rejected_count"] == 0
    assert k["mesh_subscription_active_count"] == 0 and k["mesh_schema_drift_issue_count"] == 0


def test_mesh_kpis_with_domain_breakdown(client):
    w = shippable(client)
    domain(client, "bi", owner="bi-owner")
    product(client, "draft-only", domain_name="bi", owner="bi-owner")
    k = _kpi(client)
    assert k["mesh_datsis_pass_rate"] == 0.5 and k["mesh_product_published_count"] == 0
    assert client.post(f"{MESH}/products/{w['pid']}/publish", headers=PO).status_code == 200
    purpose(client)
    subscribe(client, "alice", w["port"]["id"])
    k = _kpi(client)
    assert k["mesh_product_published_count"] == 1 and k["mesh_subscription_active_count"] == 1
    # 破壊的な変更の拒否 (非破壊の版の不足は数えない)
    cid = w["contract"]["id"]
    rejected = draft(client, cid, "1.1.0", schema=[COLUMNS[0]], quality_sla={"min_score": 0.9})
    publish(client, rejected["id"], code=409)
    insufficient = draft(client, cid, "1.0.1", quality_sla={"min_score": 0.95})
    publish(client, insufficient["id"], code=409)
    assert _kpi(client)["mesh_breaking_rejected_count"] == 1
    # スキーマのずれ
    assert client.patch(f"{API}/assets/{w['aid']}", headers=ADMIN, json={"schema": []}).status_code == 200
    assert _kpi(client)["mesh_schema_drift_issue_count"] == 1
    sales, bi = _by_domain(client)["sales"], _by_domain(client)["bi"]
    assert sales["mesh_product_published_count"] == 1 and sales["mesh_breaking_rejected_count"] == 1
    assert sales["mesh_subscription_active_count"] == 1 and sales["mesh_schema_drift_issue_count"] == 1
    assert bi["mesh_product_published_count"] == 0 and bi["mesh_breaking_rejected_count"] == 0
    assert bi["mesh_subscription_active_count"] == 0 and bi["mesh_schema_drift_issue_count"] == 0 and bi["mesh_datsis_pass_rate"] == 0.0


def test_retired_products_are_excluded(client):
    ctx = setup_contract(client)
    published(client, ctx["contract"]["id"])
    mark_published(ctx["product"]["id"])
    url = f"{MESH}/products/{ctx['product']['id']}"
    assert _kpi(client)["mesh_product_published_count"] == 1
    client.post(f"{url}/deprecate", headers=PO)
    assert _kpi(client)["mesh_product_published_count"] == 0
    client.post(f"{url}/retire", headers=PO)
    assert _kpi(client)["mesh_datsis_pass_rate"] is None


def test_maturity_is_not_changed():
    base = {"owner_assignment_rate": 1.0, "quality_score_avg": 1.0, "metadata_completeness": 1.0, "compliance_rate": 1.0, "certified_rate": 1.0}
    worst = {"mesh_product_published_count": 0, "mesh_datsis_pass_rate": 0.0, "mesh_breaking_rejected_count": 99,
             "mesh_subscription_active_count": 0, "mesh_schema_drift_issue_count": 99}
    assert maturity_indicator(base) == maturity_indicator({**base, **worst})
