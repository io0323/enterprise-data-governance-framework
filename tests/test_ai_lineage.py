"""Phase 10c (§14 AI Lineage): 版単位の系譜と lineage_edges への反映."""
from __future__ import annotations

import pytest

from tests.ai_helpers import ADMIN, API, CUSTODIAN, U, ai_asset, asset, dataset_version, edge, model_version


@pytest.fixture()
def chain(client):
    """raw → features → train(v1: features / v2: other) → model(m1: v1 / m2: v2)。"""
    raw = asset(client, "crm.raw")
    features = asset(client, "crm.features")
    other = asset(client, "crm.other")
    edge(client, "crm.raw", "crm.features")
    train = ai_asset(client, "ai.train", "training_dataset")
    v1 = dataset_version(client, train["id"], [features["id"]], version="v1")
    v2 = dataset_version(client, train["id"], [other["id"]], version="v2")
    model = ai_asset(client, "ai.model", "model")
    m1 = model_version(client, model["id"], [v1["id"]], version="m1")
    m2 = model_version(client, model["id"], [v2["id"]], version="m2")
    return {"client": client, "raw": raw, "features": features, "other": other, "train": train, "model": model,
            "v1": v1, "v2": v2, "m1": m1, "m2": m2}


def _impacted(client, asset_id, headers=ADMIN):
    r = client.get(f"{API}/ai/lineage/assets/{asset_id}/models", headers=headers)
    assert r.status_code == 200, r.text
    return r


def test_dataset_level_edges_feed_existing_impact_and_lin001(chain):
    client = chain["client"]
    impact = client.get(f"{API}/lineage/crm.raw/impact", headers=ADMIN).json()
    assert {"crm.features", "ai.train", "ai.model"} == {i["qualified_name"] for i in impact["impacted"]}
    assert "ai-owner" in impact["owners_to_notify"]
    for key in ("train", "model"):
        results = client.get(f"{API}/policies/evaluate/assets/{chain[key]['id']}", headers=ADMIN).json()["results"]
        assert next(r for r in results if r["policy_id"] == "LIN-001")["status"] == "pass"


def test_trace_model_back_to_origin(chain):
    client = chain["client"]
    t = client.get(f"{API}/ai/lineage/models/{chain['m1']['id']}", headers=ADMIN).json()
    assert t["model_version"]["version"] == "m1"
    assert [d["version"] for d in t["datasets"]] == ["v1"]
    assert [s["qualified_name"] for s in t["sources"]] == ["crm.features"]
    assert t["origins"] == ["crm.raw"]
    assert {"from": "crm.raw", "to": "crm.features", "job": "manual"} in t["upstream_edges"]
    assert client.get(f"{API}/ai/lineage/models/none", headers=ADMIN).status_code == 404


def test_trace_follows_nested_dataset_versions(chain):
    client = chain["client"]
    child = ai_asset(client, "ai.child", "training_dataset")
    r = client.post(f"{API}/ai/assets/{child['id']}/dataset-versions", headers=CUSTODIAN, json={
        "version": "c1", "manifest_hash": "ab" * 32, "record_count": 1,
        "sources": [{"asset_id": chain["train"]["id"], "kind": "dataset_version", "version": "v1"}]})
    assert r.status_code == 201, r.text
    model = ai_asset(client, "ai.child_model", "model")
    mv = model_version(client, model["id"], [r.json()["id"]])
    t = client.get(f"{API}/ai/lineage/models/{mv['id']}", headers=ADMIN).json()
    assert [d["qualified_name"] for d in t["datasets"]] == ["ai.child", "ai.train"]
    assert "crm.raw" in t["origins"]


def test_impacted_models_are_version_precise(chain):
    """版の構成元で判定する: crm.other を使うのは v2 → m2 だけ。"""
    client = chain["client"]
    rows = _impacted(client, chain["other"]["id"]).json()
    assert [r["version"] for r in rows] == ["m2"]
    assert rows[0]["via_dataset_version_ids"] == [chain["v2"]["id"]]
    # raw は features を経由するので v1 → m1 だけ
    assert [r["version"] for r in _impacted(client, chain["raw"]["id"]).json()] == ["m1"]
    # データセット自身を起点にすると全版
    r = _impacted(client, chain["train"]["id"])
    assert sorted(x["version"] for x in r.json()) == ["m1", "m2"] and r.headers["X-Total-Count"] == "2"
    assert sorted(x["version"] for x in _impacted(client, chain["model"]["id"]).json()) == ["m1", "m2"]


def test_unversioned_edge_makes_all_versions_affected(chain):
    """手動の Lineage で繋がるときは、どの版か分からないので全版を対象にする (影響範囲から漏らさない)。"""
    client = chain["client"]
    manual = asset(client, "crm.manual")
    edge(client, "crm.manual", "ai.train")
    assert sorted(r["version"] for r in _impacted(client, manual["id"]).json()) == ["m1", "m2"]
    direct = asset(client, "crm.direct")
    edge(client, "crm.direct", "ai.model")
    assert sorted(r["version"] for r in _impacted(client, direct["id"]).json()) == ["m1", "m2"]


def test_impacted_models_redact_unreadable(chain):
    client = chain["client"]
    secret_model = ai_asset(client, "ai.secret_model", "model", sensitivity="restricted")
    model_version(client, secret_model["id"], [chain["v2"]["id"]], version="s1")
    rows = _impacted(client, chain["other"]["id"], U("reader", clearance="internal")).json()
    hidden = next(r for r in rows if r.get("redacted"))
    assert hidden == {"qualified_name": "redacted-1", "status": "draft", "redacted": True}
    assert "ai.secret_model" not in str(rows)
    assert next(r for r in rows if r["qualified_name"] == "ai.model")["version"] == "m2"
    # 起点が読めなければ 403
    secret = asset(client, "crm.secret", sensitivity="restricted")
    assert client.get(f"{API}/ai/lineage/assets/{secret['id']}/models", headers=U("reader", clearance="internal")).status_code == 403


def test_trace_redacts_unreadable_sources(client):
    secret = asset(client, "crm.secret", sensitivity="restricted")
    train = ai_asset(client, "ai.t", "training_dataset")
    dv = dataset_version(client, train["id"], [secret["id"]])
    model = ai_asset(client, "ai.m", "model")
    mv = model_version(client, model["id"], [dv["id"]])
    # モデルは restricted を継承するので、clearance だけでは閲覧できない
    assert client.get(f"{API}/ai/lineage/models/{mv['id']}", headers=U("reader", clearance="internal")).status_code == 403
    # モデルの当事者には系譜を返すが、閲覧できない構成元の詳細は伏せる
    t = client.get(f"{API}/ai/lineage/models/{mv['id']}", headers=U("ai-owner")).json()
    assert t["sources"] == [{"qualified_name": "redacted-1", "redacted": True}]
    assert "crm.secret" not in str(t) and t["origins"] == ["redacted-1"]
    assert t["datasets"][0]["version"] == "v1"
    full = client.get(f"{API}/ai/lineage/models/{mv['id']}", headers=ADMIN).json()
    assert full["sources"][0]["kind"] == "asset"


def test_usages_extend_lineage(chain):
    client = chain["client"]
    url = f"{API}/ai/model-versions/{chain['m1']['id']}/usages"
    r = client.post(url, headers=CUSTODIAN, json={"kind": "deployment", "target": "prod-eu"})
    assert r.status_code == 201 and r.json()["kind"] == "deployment"
    rag = ai_asset(client, "ai.rag", "rag_corpus")
    assert client.post(url, headers=CUSTODIAN, json={"kind": "rag", "target": "support", "target_asset_id": rag["id"]}).status_code == 201
    assert client.post(url, headers=U("stranger"), json={"kind": "rag", "target": "x"}).status_code == 403
    assert client.post(url, headers=CUSTODIAN, json={"kind": "rag", "target": "x", "target_asset_id": "none"}).status_code == 422
    assert client.post(url, headers=CUSTODIAN, json={"kind": "rag", "target": "x", "target_asset_id": chain["model"]["id"]}).status_code == 422
    assert client.post(url, headers=CUSTODIAN, json={"kind": "other", "target": "x"}).status_code == 422
    assert client.post(url, headers=CUSTODIAN, json={"kind": "rag", "target": "x", "output": "生成物"}).status_code == 422
    assert client.get(url, headers=ADMIN).headers["X-Total-Count"] == "2"
    t = client.get(f"{API}/ai/lineage/models/{chain['m1']['id']}", headers=ADMIN).json()
    assert {u["target"] for u in t["usages"]} == {"prod-eu", "support"}
    assert "ai.rag" in {i["qualified_name"] for i in client.get(f"{API}/lineage/ai.model/impact", headers=ADMIN).json()["impacted"]}
