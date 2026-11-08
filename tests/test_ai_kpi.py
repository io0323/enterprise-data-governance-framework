"""Phase 10f (§17): AI 統制の KPI. 成熟度の判定 (§20) は変えない."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from edgf.db import engine
from edgf.services.kpi import maturity_indicator
from tests.ai_helpers import (ADMIN, API, COMMITTEE, OWNER, ai_asset, approve_dataset, asset, dataset_version, edge,
                              model_version, run_quality)


def _kpi(client, **params):
    r = client.get(f"{API}/kpi", headers=ADMIN, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_empty_ai_kpis(client):
    k = _kpi(client)
    assert k["ai_asset_count"] == 0 and k["ai_training_approved_rate"] is None and k["ai_card_complete_rate"] is None
    assert k["ai_remediation_pending_count"] == 0 and k["ai_inheritance_violation_count"] == 0


def test_training_approved_rate_ignores_superseded(client):
    src = asset(client, "crm.src")
    run_quality(client, src["id"])
    train = ai_asset(client, "ai.train", "training_dataset")
    v1 = dataset_version(client, train["id"], [src["id"]], version="v1")
    dataset_version(client, train["id"], [src["id"]], version="v2")
    v3 = dataset_version(client, train["id"], [src["id"]], version="v3")
    assert _kpi(client)["ai_training_approved_rate"] == 0.0
    approve_dataset(client, v1["id"])
    assert _kpi(client)["ai_training_approved_rate"] == round(1 / 3, 4)
    with engine.begin() as conn:
        conn.execute(text("UPDATE dataset_versions SET status = 'superseded' WHERE id = :id"), {"id": v3["id"]})
    assert _kpi(client)["ai_training_approved_rate"] == 0.5


def test_card_complete_rate(client):
    train = ai_asset(client, "ai.train", "training_dataset")
    model = ai_asset(client, "ai.model", "model")
    pii = ai_asset(client, "ai.pii", "embedding_set", sensitivity="confidential", contains_pii=True, retention_days=1)
    ai_asset(client, "ai.none", "vector_index")
    assert _kpi(client)["ai_card_complete_rate"] == 0.0
    card = {"purpose": "p", "collection_source": "c", "license": "l", "known_biases": "b", "constraints": "x"}
    assert client.put(f"{API}/ai/assets/{train['id']}/dataset-card", headers=OWNER, json=card).status_code == 200
    assert _kpi(client)["ai_card_complete_rate"] == 0.25
    # PII を含む資産は同意の根拠も必要
    assert client.put(f"{API}/ai/assets/{pii['id']}/dataset-card", headers=OWNER, json=card).status_code == 200
    assert _kpi(client)["ai_card_complete_rate"] == 0.25
    assert client.put(f"{API}/ai/assets/{pii['id']}/dataset-card", headers=OWNER, json={**card, "consent_purpose_id": "ai_training"}).status_code == 200
    assert _kpi(client)["ai_card_complete_rate"] == 0.5
    # Model Card は全項目が必要
    assert client.put(f"{API}/ai/assets/{model['id']}/model-card", headers=OWNER, json={"intended_use": "u"}).status_code == 200
    assert _kpi(client)["ai_card_complete_rate"] == 0.5
    assert client.put(f"{API}/ai/assets/{model['id']}/model-card", headers=OWNER, json={
        "intended_use": "u", "out_of_scope_use": "o", "metrics": {"auc": 0.9}, "bias_evaluation_ref": "e", "constraints": "x"}).status_code == 200
    assert _kpi(client)["ai_card_complete_rate"] == 0.75


def test_inheritance_violation_count_and_domain_scope(client):
    client.post(f"{API}/domains", headers=ADMIN, json={"name": "ml"})
    asset(client, "up.secret", sensitivity="restricted")
    ai_asset(client, "ai.bad", "embedding_set", domain="ml")
    ai_asset(client, "ai.good", "embedding_set")
    edge(client, "up.secret", "ai.bad")
    assert _kpi(client)["ai_inheritance_violation_count"] == 1
    assert _kpi(client)["ai_asset_count"] == 2
    domains = {d["scope"]: d for d in client.get(f"{API}/kpi/domains", headers=ADMIN).json()}
    assert domains["ml"]["ai_inheritance_violation_count"] == 1 and domains["ml"]["ai_asset_count"] == 1


def _pending_remediation(client):
    src = asset(client, "crm.people", sensitivity="confidential", contains_pii=True, retention_days=1, schema=[
        {"name": "email", "identifier_type": "email", "processing_purposes": ["ai_training"], "legal_basis": "consent"}])
    train = ai_asset(client, "ai.train", "training_dataset", sensitivity="confidential")
    dv = dataset_version(client, train["id"], [src["id"]])
    model = ai_asset(client, "ai.model", "model", sensitivity="confidential")
    model_version(client, model["id"], [dv["id"]])
    r = client.post(f"{API}/privacy/erasure-requests", headers=ADMIN, json={"identifier_type": "email", "identifier": "a@example.com"})
    rid = r.json()["id"]
    for action in ("scope", "approve", "dispatch"):
        assert client.post(f"{API}/privacy/erasure-requests/{rid}/{action}", headers=COMMITTEE).status_code == 200
    return client.get(f"{API}/ai/remediations", headers=COMMITTEE).json()[0]


def test_remediation_pending_count_includes_expired_accept(client):
    rem = _pending_remediation(client)
    assert _kpi(client)["ai_remediation_pending_count"] == 1
    until = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    assert client.post(f"{API}/ai/remediations/{rem['id']}/decision", headers=COMMITTEE, json={
        "decision": "accept", "reason": "r", "accept_until": until}).status_code == 200
    assert _kpi(client)["ai_remediation_pending_count"] == 0
    with engine.begin() as conn:
        conn.execute(text("UPDATE model_remediations SET accept_until = :t"), {"t": datetime(2020, 1, 1)})
    assert _kpi(client)["ai_remediation_pending_count"] == 1


def test_maturity_is_not_changed_by_ai_kpis():
    base = {"owner_assignment_rate": 1.0, "quality_score_avg": 1.0, "metadata_completeness": 1.0, "compliance_rate": 1.0,
            "certified_rate": 1.0}
    worst = {"ai_training_approved_rate": 0.0, "ai_card_complete_rate": 0.0, "ai_remediation_pending_count": 99,
             "ai_inheritance_violation_count": 99, "ai_asset_count": 99}
    assert maturity_indicator(base) == maturity_indicator({**base, **worst})


def test_asset_search_by_ai_kind(client):
    ai_asset(client, "ai.a", "model")
    ai_asset(client, "ai.b", "rag_corpus")
    rows = client.get(f"{API}/assets", headers=ADMIN, params={"ai_kind": "rag_corpus"}).json()
    assert [r["qualified_name"] for r in rows] == ["ai.b"]
