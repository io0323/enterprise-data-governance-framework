"""Phase 10a (§14 AI Metadata): AI 資産、Card、版の情報."""
from __future__ import annotations

import sqlite3

import pytest

from tests.ai_helpers import (ADMIN, API, COMMITTEE, CUSTODIAN, HASH, OWNER, STEWARD, U, ai_asset, asset, dataset_version,
                              model_version)
from tests.test_mdm_integration import _alembic

MDM_ASSET = "mdm.party"  # policies/mdm/party.yaml の対応資産


@pytest.fixture()
def world(client):
    src = asset(client, "crm.events")
    ds = ai_asset(client, "ai.train", "training_dataset")
    model = ai_asset(client, "ai.churn", "model")
    return client, src, ds, model


def test_ai_kind_only_for_ai_class(client):
    r = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "x", "name": "x", "data_class": "master",
                                                          "sensitivity": "internal", "ai_kind": "model"})
    assert r.status_code == 422
    a = ai_asset(client, "ai.x", "model")
    assert a["ai_kind"] == "model"
    assert client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"data_class": "master"}).status_code == 422
    assert client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"ai_kind": "vector_index"}).status_code == 200


def test_kind_cannot_change_once_versions_exist(world):
    client, src, ds, _ = world
    dataset_version(client, ds["id"], [src["id"]])
    assert client.patch(f"{API}/assets/{ds['id']}", headers=ADMIN, json={"ai_kind": "model"}).status_code == 409
    assert client.patch(f"{API}/assets/{ds['id']}", headers=ADMIN, json={"description": "new"}).status_code == 200


def test_ai_training_purpose_is_standard(client):
    r = client.get(f"{API}/privacy/purposes/ai_training", headers=ADMIN)
    assert r.status_code == 200 and r.json()["default_legal_basis"] == "consent"


def test_dataset_card(world):
    client, _, ds, model = world
    url = f"{API}/ai/assets/{ds['id']}/dataset-card"
    assert client.get(url, headers=ADMIN).status_code == 404
    body = {"purpose": "離反予測", "collection_source": "CRM", "license": "internal", "consent_purpose_id": "ai_training",
            "known_biases": "地域の偏り", "constraints": "EU を除く"}
    for headers, code in ((OWNER, 200), (STEWARD, 200), (CUSTODIAN, 403), (U("nobody"), 403)):
        assert client.put(url, headers=headers, json=body).status_code == code
    assert client.get(url, headers=ADMIN).json()["consent_purpose_id"] == "ai_training"
    assert client.put(url, headers=OWNER, json={"consent_purpose_id": "unknown"}).status_code == 422
    # Model の資産には Dataset Card を登録できない
    assert client.put(f"{API}/ai/assets/{model['id']}/dataset-card", headers=OWNER, json=body).status_code == 422
    # ドメイン内の Steward
    client.post(f"{API}/domains", headers=ADMIN, json={"name": "ml"})
    in_domain = ai_asset(client, "ai.domain", "embedding_set", domain="ml", owner="o", steward="s")
    r = client.put(f"{API}/ai/assets/{in_domain['id']}/dataset-card", headers=U("x", "steward", "ml"), json=body)
    assert r.status_code == 200
    assert client.put(f"{API}/ai/assets/{in_domain['id']}/dataset-card", headers=U("x", "steward", "other"), json=body).status_code == 403


def test_model_card(world):
    client, _, ds, model = world
    url = f"{API}/ai/assets/{model['id']}/model-card"
    assert client.get(url, headers=ADMIN).status_code == 404
    body = {"intended_use": "離反予測", "out_of_scope_use": "与信", "metrics": {"auc": 0.9, "note": "holdout"},
            "bias_evaluation_ref": "eval://bias/1", "constraints": "日本のみ"}
    assert client.put(url, headers=OWNER, json=body).status_code == 200
    assert client.get(url, headers=ADMIN).json()["metrics"]["auc"] == 0.9
    assert client.put(url, headers=OWNER, json={"metrics": {"embedding": [0.1, 0.2]}}).status_code == 422
    assert client.put(f"{API}/ai/assets/{ds['id']}/model-card", headers=OWNER, json=body).status_code == 422


def test_dataset_version_registration(world):
    client, src, ds, model = world
    v = dataset_version(client, ds["id"], [src["id"]], record_count=42, period_start="2026-01-01T09:00:00+09:00",
                        period_end="2026-02-01T00:00:00Z", external_uri="s3://bucket/manifest.json")
    assert v["status"] == "draft" and v["record_count"] == 42 and v["registered_by"] == "ai-custodian"
    assert v["period_start"].startswith("2026-01-01T00:00:00")
    assert v["sources"] == [{"asset_id": src["id"], "qualified_name": "crm.events", "kind": "asset", "version": "2026-01"}]
    assert client.get(f"{API}/ai/dataset-versions/{v['id']}", headers=ADMIN).json()["id"] == v["id"]
    assert client.get(f"{API}/ai/dataset-versions/none", headers=ADMIN).status_code == 404
    r = client.get(f"{API}/ai/assets/{ds['id']}/dataset-versions", headers=ADMIN)
    assert r.headers["X-Total-Count"] == "1" and r.json()[0]["id"] == v["id"]

    url = f"{API}/ai/assets/{ds['id']}/dataset-versions"
    base = {"manifest_hash": HASH, "record_count": 1, "sources": [{"asset_id": src["id"], "version": "1"}]}
    assert client.post(url, headers=CUSTODIAN, json={**base, "version": "v1"}).status_code == 409  # 版は不変
    assert client.post(url, headers=U("nobody"), json={**base, "version": "v2"}).status_code == 403
    assert client.post(url, headers=CUSTODIAN, json={**base, "version": "v2", "sources": []}).status_code == 422
    assert client.post(url, headers=CUSTODIAN, json={**base, "version": "v2", "manifest_hash": "xyz"}).status_code == 422
    assert client.post(url, headers=CUSTODIAN, json={**base, "version": "v2", "period_start": "2026-02-01T00:00:00",
                                                     "period_end": "2026-01-01T00:00:00"}).status_code == 422
    for bad_uri in ("data:text/plain;base64,AAAA", "https://user:secret@host/x", "not-a-uri"):
        assert client.post(url, headers=CUSTODIAN, json={**base, "version": "v2", "external_uri": bad_uri}).status_code == 422
    # 未登録の構成元、自分自身
    assert client.post(url, headers=CUSTODIAN, json={**base, "version": "v2", "sources": [{"asset_id": "none", "version": "1"}]}).status_code == 422
    assert client.post(url, headers=CUSTODIAN, json={**base, "version": "v2", "sources": [{"asset_id": ds["id"], "version": "1"}]}).status_code == 422
    # Model の資産には Dataset Version を登録できない
    assert client.post(f"{API}/ai/assets/{model['id']}/dataset-versions", headers=CUSTODIAN, json={**base, "version": "v2"}).status_code == 422


def test_source_versions_are_resolved(world):
    """MDM、Reference Data、別の Dataset Version の版を構成元として参照できる。"""
    client, _, ds, _ = world
    url = f"{API}/ai/assets/{ds['id']}/dataset-versions"
    base = {"manifest_hash": HASH, "record_count": 1}

    ref = asset(client, "ref.country", data_class="reference", sensitivity="public")
    assert client.post(f"{API}/reference-data/sets", headers=U("x", "owner", "*"), json={
        "id": "country", "asset_id": ref["id"], "name": "c"}).status_code == 201
    assert client.post(f"{API}/reference-data/sets/country/versions", headers=U("x", "owner", "*"), json={
        "codes": [{"code": "JP", "label": "日本"}]}).status_code == 201
    src = {"asset_id": ref["id"], "kind": "reference_data", "version": "1"}
    assert client.post(url, headers=CUSTODIAN, json={**base, "version": "r0", "sources": [src]}).status_code == 422  # 未公開
    assert client.post(f"{API}/reference-data/sets/country/versions/1/publish", headers=COMMITTEE).status_code == 200
    assert client.post(url, headers=CUSTODIAN, json={**base, "version": "r1", "sources": [src]}).status_code == 201
    assert client.post(url, headers=CUSTODIAN, json={**base, "version": "r2", "sources": [{**src, "version": "abc"}]}).status_code == 422

    master = asset(client, MDM_ASSET, data_class="master")
    src = {"asset_id": master["id"], "kind": "mdm", "version": "rule-1"}
    assert client.post(url, headers=CUSTODIAN, json={**base, "version": "m0", "sources": [src]}).status_code == 201
    other = asset(client, "other.master", data_class="master")
    assert client.post(url, headers=CUSTODIAN, json={**base, "version": "m1", "sources": [
        {"asset_id": other["id"], "kind": "mdm", "version": "1"}]}).status_code == 422

    parent = ai_asset(client, "ai.parent", "training_dataset")
    dataset_version(client, parent["id"], [ref["id"]], version="p1")
    src = {"asset_id": parent["id"], "kind": "dataset_version"}
    assert client.post(url, headers=CUSTODIAN, json={**base, "version": "d1", "sources": [{**src, "version": "p1"}]}).status_code == 201
    assert client.post(url, headers=CUSTODIAN, json={**base, "version": "d2", "sources": [{**src, "version": "none"}]}).status_code == 422


def test_model_version_registration(world):
    client, src, ds, model = world
    dv = dataset_version(client, ds["id"], [src["id"]])
    mv = model_version(client, model["id"], [dv["id"], dv["id"]], external_uri="models:/churn/1")
    assert mv["status"] == "draft" and mv["dataset_version_ids"] == [dv["id"]] and mv["external_uri"] == "models:/churn/1"
    assert client.get(f"{API}/ai/model-versions/{mv['id']}", headers=ADMIN).json()["version"] == "m1"
    assert client.get(f"{API}/ai/model-versions/none", headers=ADMIN).status_code == 404
    assert client.get(f"{API}/ai/assets/{model['id']}/model-versions", headers=ADMIN).headers["X-Total-Count"] == "1"

    url = f"{API}/ai/assets/{model['id']}/model-versions"
    assert client.post(url, headers=CUSTODIAN, json={"version": "m1", "dataset_version_ids": [dv["id"]]}).status_code == 409
    assert client.post(url, headers=CUSTODIAN, json={"version": "m2", "dataset_version_ids": []}).status_code == 422  # 紐付けは必須
    assert client.post(url, headers=CUSTODIAN, json={"version": "m2", "dataset_version_ids": ["none"]}).status_code == 422
    assert client.post(url, headers=U("nobody"), json={"version": "m2", "dataset_version_ids": [dv["id"]]}).status_code == 403
    assert client.post(url, headers=CUSTODIAN, json={"version": "m2", "dataset_version_ids": [dv["id"]],
                                                     "weights": [0.1]}).status_code == 422
    assert client.post(f"{API}/ai/assets/{ds['id']}/model-versions", headers=CUSTODIAN, json={
        "version": "m2", "dataset_version_ids": [dv["id"]]}).status_code == 422
    # training_dataset 以外の版は学習データとして紐付けられない
    emb = ai_asset(client, "ai.emb", "embedding_set")
    ev = dataset_version(client, emb["id"], [src["id"]])
    assert client.post(url, headers=CUSTODIAN, json={"version": "m2", "dataset_version_ids": [ev["id"]]}).status_code == 422


def test_ai_reads_follow_abac(world):
    client, src, _, _ = world
    secret = ai_asset(client, "ai.secret", "training_dataset", sensitivity="restricted")
    dv = dataset_version(client, secret["id"], [src["id"]])
    reader = U("reader", clearance="internal")
    assert client.get(f"{API}/ai/assets/{secret['id']}/dataset-versions", headers=reader).status_code == 403
    assert client.get(f"{API}/ai/dataset-versions/{dv['id']}", headers=reader).status_code == 403
    assert client.get(f"{API}/ai/assets/{secret['id']}/dataset-card", headers=reader).status_code == 403
    assert client.get(f"{API}/ai/dataset-versions/{dv['id']}", headers=CUSTODIAN).status_code == 200


def test_state_changes_are_audited(world):
    client, src, ds, model = world
    client.put(f"{API}/ai/assets/{ds['id']}/dataset-card", headers=OWNER, json={"purpose": "p"})
    client.put(f"{API}/ai/assets/{model['id']}/model-card", headers=OWNER, json={"intended_use": "u"})
    dv = dataset_version(client, ds["id"], [src["id"]])
    model_version(client, model["id"], [dv["id"]])
    actions = [e["action"] for e in client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000}).json()]
    assert {"ai.card.write", "ai.dataset_version.register", "ai.model_version.register"} <= set(actions)


def test_seed_keeps_existing_purpose_and_consent_history(tmp_path):
    """同じ ID の Purpose が既にある環境では定義を尊重する。同意の履歴があれば downgrade でも消さない。"""
    db = tmp_path / "seed.db"
    url = f"sqlite:///{db}"
    _alembic(["upgrade", "0011"], url)
    now = "2026-01-01 00:00:00"
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO privacy_purposes (id, description, default_legal_basis, created_at, updated_at) "
                     "VALUES ('ai_training', '自社の定義', 'contract', ?, ?)", (now, now))
        conn.execute("INSERT INTO consents (subject_ref, purpose_id, status, source, recorded_by, recorded_at) "
                     "VALUES ('ab', 'ai_training', 'granted', '', 'x', ?)", (now,))
    _alembic(["upgrade", "head"], url)
    query = "SELECT description, default_legal_basis FROM privacy_purposes WHERE id = 'ai_training'"
    with sqlite3.connect(db) as conn:
        assert conn.execute(query).fetchone() == ("自社の定義", "contract")
    _alembic(["downgrade", "0011"], url)
    with sqlite3.connect(db) as conn:
        assert conn.execute(query).fetchone() == ("自社の定義", "contract")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert not tables & {"dataset_versions", "model_versions", "training_approvals", "model_remediations", "ai_incidents"}
        assert "ai_kind" not in [r[1] for r in conn.execute("PRAGMA table_info(data_assets)")]
    # 参照が無ければ downgrade で標準の行を消す
    fresh = tmp_path / "fresh.db"
    _alembic(["upgrade", "head"], f"sqlite:///{fresh}")
    with sqlite3.connect(fresh) as conn:
        assert conn.execute(query).fetchone()[1] == "consent"
    _alembic(["downgrade", "0011"], f"sqlite:///{fresh}")
    with sqlite3.connect(fresh) as conn:
        assert conn.execute(query).fetchone() is None
