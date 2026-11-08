"""Phase 10 の置き場所の保証 (§18.2): 学習データ、Embedding、サンプルは Control Plane のどこにも残らない."""
from __future__ import annotations

import json

from tests.ai_helpers import (ADMIN, API, COMMITTEE, CUSTODIAN, HASH, OWNER, ai_asset, asset, dataset_version, edge,
                              model_version)
from tests.conftest import dump_database

SAMPLE = "SAMPLE-TEXT-3f9a1c-顧客の問い合わせ本文"
EMBEDDING = 0.7391826451
QUERY = "QUERY-TEXT-77ab21"
CHUNK = "CHUNK-TEXT-a1b2c3"
WEIGHTS = "WEIGHTS-BLOB-99ff00"
MARKERS = (SAMPLE, str(EMBEDDING), QUERY, CHUNK, WEIGHTS)


def _assert_nowhere(client, logs, responses=()):
    haystacks = {"db": dump_database(), "logs": logs.text(),
                 "audit": json.dumps(client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000}).json(), ensure_ascii=False)}
    haystacks.update({f"response{i}": r.text for i, r in enumerate(responses)})
    for where, text in haystacks.items():
        for marker in MARKERS:
            assert marker.lower() not in text.lower(), f"{marker} が {where} に残っています"


def test_values_never_reach_control_plane(client, edgf_logs):
    src = asset(client, "docs.src")
    train = ai_asset(client, "ai.train", "training_dataset")
    model = ai_asset(client, "ai.model", "model")
    rag = ai_asset(client, "ai.rag", "rag_corpus")
    edge(client, "docs.src", "ai.rag")

    forbidden = {"samples": [SAMPLE], "embedding": [EMBEDDING], "text": SAMPLE, "weights": WEIGHTS}
    source = {"asset_id": src["id"], "version": "1"}
    responses = []

    def attempt(method, url, headers, body):
        r = client.request(method, url, headers=headers, json=body)
        assert r.status_code == 422, r.text
        responses.append(r)

    # Card
    for key, value in forbidden.items():
        attempt("PUT", f"{API}/ai/assets/{train['id']}/dataset-card", OWNER, {"purpose": "p", key: value})
        attempt("PUT", f"{API}/ai/assets/{model['id']}/model-card", OWNER, {"intended_use": "u", key: value})
    attempt("PUT", f"{API}/ai/assets/{model['id']}/model-card", OWNER, {"metrics": {"vector": [EMBEDDING]}})
    # 版の登録
    version = {"version": "v1", "manifest_hash": HASH, "record_count": 1, "sources": [source]}
    for key, value in forbidden.items():
        attempt("POST", f"{API}/ai/assets/{train['id']}/dataset-versions", CUSTODIAN, {**version, key: value})
        attempt("POST", f"{API}/ai/assets/{train['id']}/dataset-versions", CUSTODIAN,
                {**version, "sources": [{**source, key: value}]})
    attempt("POST", f"{API}/ai/assets/{train['id']}/dataset-versions", CUSTODIAN,
            {**version, "external_uri": f"data:text/plain,{SAMPLE}"})
    # RAG の判定
    rag_body = {"corpus_asset_id": rag["id"], "user": {"subject": "alice", "roles": []}, "hit_asset_ids": [src["id"]]}
    attempt("POST", f"{API}/ai/rag/authorize", CUSTODIAN, {**rag_body, "query": QUERY})
    attempt("POST", f"{API}/ai/rag/authorize", CUSTODIAN, {**rag_body, "query_embedding": [EMBEDDING]})
    attempt("POST", f"{API}/ai/rag/authorize", CUSTODIAN, {**rag_body, "hit_asset_ids": [{"asset_id": src["id"], "chunk": CHUNK}]})

    # 検証エラーの応答、DB、ログ、監査のどこにも値が出ない
    _assert_nowhere(client, edgf_logs, responses)


def test_success_path_keeps_only_metadata(client, edgf_logs):
    """正常系の全 API を通しても、保存されるのはメタデータだけ。"""
    src = asset(client, "docs.src")
    train = ai_asset(client, "ai.train", "training_dataset")
    model = ai_asset(client, "ai.model", "model")
    rag = ai_asset(client, "ai.rag", "rag_corpus")
    edge(client, "docs.src", "ai.rag")
    assert client.put(f"{API}/ai/assets/{train['id']}/dataset-card", headers=OWNER, json={
        "purpose": "p", "license": "internal", "consent_purpose_id": "ai_training"}).status_code == 200
    dv = dataset_version(client, train["id"], [src["id"]])
    mv = model_version(client, model["id"], [dv["id"]], external_uri="models:/x/1")
    r = client.post(f"{API}/ai/rag/authorize", headers=CUSTODIAN, json={
        "corpus_asset_id": rag["id"], "user": {"subject": "rag-end-user-5512", "roles": []}, "hit_asset_ids": [src["id"]]})
    assert r.json()["allowed_asset_ids"] == [src["id"]]
    _assert_nowhere(client, edgf_logs)
    # 判定はステートレス: 検索の利用者も残らない
    assert "rag-end-user-5512" not in dump_database() and "rag-end-user-5512" not in edgf_logs.text()
    assert mv["external_uri"] == "models:/x/1"


def test_audit_guard_applies_to_ai_free_text(client):
    """是正の理由やインシデントのタイトルは監査に残るので、PII のパターンは拒否する (監査は消せない)。"""
    src = asset(client, "docs.src")
    r = client.post(f"{API}/ai/incidents", headers=COMMITTEE, json={
        "kind": "recall", "title": "person@example.com のデータが混入", "origin_asset_id": src["id"]})
    assert r.status_code == 422 and r.json()["type"].endswith("audit-payload-rejected")
    assert client.get(f"{API}/ai/incidents", headers=ADMIN).json() == []
