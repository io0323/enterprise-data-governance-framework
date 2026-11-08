"""Phase 10d (§14 Embedding / Vector / RAG): 統制属性の継承、AI-002〜004、RAG の判定."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.ai_helpers import (ADMIN, API, COMMITTEE, CUSTODIAN, U, ai_asset, asset, certify, dataset_version, edge,
                              policy_result)

ANON = {"privacy_technique": "anonymized", "privacy_params": {"k": 10}}


def _get(client, asset_id):
    return client.get(f"{API}/assets/{asset_id}", headers=ADMIN).json()


def _inh(client, asset_id):
    r = client.get(f"{API}/ai/assets/{asset_id}/inheritance", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def _issues(client, asset_id, source_ref="policy:AI-002"):
    rows = client.get(f"{API}/issues", headers=ADMIN, params={"limit": 1000}).json()
    return [i for i in rows if i["asset_id"] == asset_id and i["source_ref"] == source_ref]


def _three_tier(client, *, top=None, middle=None):
    """up1 → up2 → up3 → emb。"""
    up1 = asset(client, "up.one", **(top or {}))
    up2 = asset(client, "up.two", **(middle or {}))
    up3 = asset(client, "up.three")
    emb = ai_asset(client, "ai.emb", "embedding_set")
    edge(client, "up.one", "up.two")
    edge(client, "up.two", "up.three")
    edge(client, "up.three", "ai.emb")
    return up1, up2, up3, emb


# ---------------------------------------------------------------- 継承
def test_one_restricted_in_three_tiers_makes_effective_restricted(client):
    up1, *_, emb = _three_tier(client, top={"sensitivity": "restricted"})
    got = _get(client, emb["id"])
    assert got["sensitivity"] == "internal" and got["effective_sensitivity"] == "restricted"
    assert got["inheritance"]["sensitivity_from"] == ["up.one"]
    i = _inh(client, emb["id"])
    assert i["effective_sensitivity"] == "restricted" and i["violation"] is True
    assert policy_result(client, emb["id"], "AI-002")["status"] == "fail"
    assert len(_issues(client, emb["id"])) == 1


def test_pii_is_logical_or_of_upstream(client):
    *_, emb = _three_tier(client, middle={"sensitivity": "confidential", "contains_pii": True, "retention_days": 1,
                                          "schema": [{"name": "email", "pii": True}]})
    got = _get(client, emb["id"])
    assert got["contains_pii"] is False and got["effective_contains_pii"] is True
    assert _inh(client, emb["id"])["pii_from"] == ["up.two"]
    assert policy_result(client, emb["id"], "AI-002")["status"] == "fail"


def test_anonymized_releases_inheritance_only_when_k_is_met(client):
    # k が基準を満たす anonymized を経由すると、その先の restricted / PII は継承しない
    _, _, up3, emb = _three_tier(client, top={"sensitivity": "restricted", "contains_pii": True}, middle=ANON)
    got = _get(client, emb["id"])
    assert got["effective_sensitivity"] == "internal" and got["effective_contains_pii"] is False
    assert got["inheritance"]["released_by"] == ["up.two"]
    assert policy_result(client, emb["id"], "AI-002")["status"] == "pass"
    assert _issues(client, emb["id"]) == []


def test_insufficient_k_does_not_release(client):
    *_, emb = _three_tier(client, top={"sensitivity": "restricted"},
                          middle={"privacy_technique": "anonymized", "privacy_params": {"k": 2}})
    got = _get(client, emb["id"])
    assert got["effective_sensitivity"] == "restricted" and got["inheritance"]["released_by"] == []


def test_pseudonymized_does_not_release(client):
    *_, emb = _three_tier(client, top={"sensitivity": "restricted"}, middle={
        "privacy_technique": "pseudonymized", "privacy_params": {"key_vault_ref": "kv://1"},
        "sensitivity": "confidential", "retention_days": 1})
    got = _get(client, emb["id"])
    assert got["effective_sensitivity"] == "restricted" and got["effective_contains_pii"] is True


def test_release_needs_every_path_anonymized(client):
    asset(client, "up.secret", sensitivity="restricted")
    asset(client, "up.anon", **ANON)
    asset(client, "up.bypass")
    emb = ai_asset(client, "ai.emb", "embedding_set")
    for s, t in (("up.secret", "up.anon"), ("up.anon", "ai.emb")):
        edge(client, s, t)
    assert _get(client, emb["id"])["effective_sensitivity"] == "internal"
    edge(client, "up.secret", "up.bypass")  # 匿名化を経ない経路が追加された
    edge(client, "up.bypass", "ai.emb")
    got = _get(client, emb["id"])
    assert got["effective_sensitivity"] == "restricted" and got["inheritance"]["sensitivity_from"] == ["up.secret"]


def test_raising_upstream_sensitivity_opens_issue(client):
    up1, _, _, emb = _three_tier(client)
    assert _get(client, emb["id"])["effective_sensitivity"] == "internal" and _issues(client, emb["id"]) == []
    assert client.patch(f"{API}/assets/{up1['id']}", headers=ADMIN, json={"sensitivity": "restricted"}).status_code == 200
    assert _get(client, emb["id"])["effective_sensitivity"] == "restricted"
    issues = _issues(client, emb["id"])
    assert len(issues) == 1 and issues[0]["assignee"] == "ai-owner" and issues[0]["severity"] == "critical"
    # 再度の再計算で重複起票しない
    assert client.post(f"{API}/ai/inheritance/recompute", headers=COMMITTEE).json()["violations"] == ["ai.emb"]
    assert len(_issues(client, emb["id"])) == 1
    # 宣言値を上げると違反は解消する
    assert client.patch(f"{API}/assets/{emb['id']}", headers=ADMIN, json={"sensitivity": "restricted"}).status_code == 200
    assert _inh(client, emb["id"])["violation"] is False


def test_lowering_anonymization_restores_inheritance(client):
    _, up2, _, emb = _three_tier(client, top={"sensitivity": "restricted"}, middle=ANON)
    assert _get(client, emb["id"])["effective_sensitivity"] == "internal"
    assert client.patch(f"{API}/assets/{up2['id']}", headers=ADMIN, json={"privacy_params": {"k": 1}}).status_code == 200
    assert _get(client, emb["id"])["effective_sensitivity"] == "restricted"


def test_lineage_and_registration_trigger_recompute(client):
    emb = ai_asset(client, "ai.emb", "embedding_set")
    # OpenLineage の取込でも再計算する
    asset(client, "up.secret", sensitivity="confidential")
    ev = {"eventType": "COMPLETE", "run": {"runId": "r1"}, "job": {"namespace": "etl", "name": "j"},
          "inputs": [{"name": "up.secret"}], "outputs": [{"name": "ai.emb"}]}
    assert client.post(f"{API}/lineage/openlineage", headers=ADMIN, json=ev).status_code == 202
    assert _get(client, emb["id"])["effective_sensitivity"] == "confidential"
    # 未登録の上流が、後から restricted で登録された
    edge(client, "up.unknown", "ai.emb")
    assert _inh(client, emb["id"])["unregistered_upstream"] == ["up.unknown"]
    asset(client, "up.unknown", sensitivity="restricted")
    assert _get(client, emb["id"])["effective_sensitivity"] == "restricted"
    # 版の登録でもエッジが増えるので再計算する
    ds = ai_asset(client, "ai.ds", "training_dataset")
    dataset_version(client, ds["id"], [emb["id"]])
    assert _get(client, ds["id"])["effective_sensitivity"] == "restricted"


def test_certified_ai_asset_loses_certification_on_violation(client):
    client.post(f"{API}/domains", headers=ADMIN, json={"name": "ml"})
    up = asset(client, "up.src")
    vec = ai_asset(client, "ai.vec", "vector_index", domain="ml")
    edge(client, "up.src", "ai.vec")
    certify(client, vec["id"], owner="ai-owner")
    assert _get(client, vec["id"])["certification"] == "certified"
    assert client.patch(f"{API}/assets/{up['id']}", headers=ADMIN, json={"sensitivity": "restricted"}).status_code == 200
    assert _get(client, vec["id"])["certification"] == "draft"
    events = client.get(f"{API}/audit/events", headers=ADMIN, params={"entity_id": vec["id"], "limit": 1000}).json()
    assert any(e["action"] == "ai.inheritance.recompute" and e["payload"].get("certification_reset") for e in events)


def test_changing_data_class_clears_effective_values(client):
    a = ai_asset(client, "ai.x", "embedding_set")
    assert _get(client, a["id"])["effective_sensitivity"] == "internal"
    assert client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"ai_kind": None, "data_class": "analytical"}).status_code == 200
    got = _get(client, a["id"])
    assert got["effective_sensitivity"] is None and got["effective_contains_pii"] is None
    assert client.get(f"{API}/ai/assets/{a['id']}/inheritance", headers=ADMIN).status_code == 422


def test_abac_uses_effective_sensitivity(client):
    """宣言が internal でも、実効が restricted の AI 資産は閲覧できない (継承を ABAC に反映する)。"""
    asset(client, "up.secret", sensitivity="restricted")
    emb = ai_asset(client, "ai.emb", "embedding_set")
    edge(client, "up.secret", "ai.emb")
    reader = U("reader", clearance="confidential")
    assert client.get(f"{API}/assets/{emb['id']}", headers=reader).status_code == 403
    assert client.get(f"{API}/assets", headers=reader, params={"data_class": "ai"}).json() == []
    assert client.get(f"{API}/assets/{emb['id']}", headers=U("ai-owner")).status_code == 200
    exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    r = client.post(f"{API}/assets/{emb['id']}/access-grants", headers=U("ai-owner"), json={
        "subject": "reader", "purpose": "監査", "expires_at": exp})
    assert r.status_code == 201, r.text
    assert client.get(f"{API}/assets/{emb['id']}", headers=reader).status_code == 200
    assert [a["id"] for a in client.get(f"{API}/assets", headers=reader, params={"data_class": "ai"}).json()] == [emb["id"]]


def test_inheritance_permissions(client):
    assert client.post(f"{API}/ai/inheritance/recompute", headers=U("x", "owner")).status_code == 403
    assert client.post(f"{API}/ai/inheritance/recompute", headers=ADMIN).json() == {"assets": 0, "violations": []}


def test_inventory_lists_inherited_pii(client):
    asset(client, "up.pii", sensitivity="confidential", contains_pii=True, retention_days=1,
          schema=[{"name": "email", "pii": True}])
    ai_asset(client, "ai.emb", "embedding_set", sensitivity="confidential")
    edge(client, "up.pii", "ai.emb")
    names = [e["qualified_name"] for e in client.get(f"{API}/privacy/inventory", headers=COMMITTEE).json()]
    assert "ai.emb" in names


# ---------------------------------------------------------------- AI-003 / AI-004
def test_ai003_rag_sources_must_be_certified(client):
    rag = ai_asset(client, "ai.rag", "rag_corpus")
    assert "構成元" in policy_result(client, rag["id"], "AI-003")["message"]
    assert policy_result(client, rag["id"], "AI-003")["status"] == "fail"
    client.post(f"{API}/domains", headers=ADMIN, json={"name": "docs"})
    doc = asset(client, "docs.manual", data_class="transactional", domain="docs")
    vec = ai_asset(client, "ai.vec", "vector_index")
    edge(client, "docs.manual", "ai.vec")
    edge(client, "ai.vec", "ai.rag")
    r = policy_result(client, rag["id"], "AI-003")
    assert r["status"] == "fail" and "docs.manual" in r["message"]  # ベクトルを透過して元の資産を見る
    certify(client, doc["id"])
    assert policy_result(client, rag["id"], "AI-003")["status"] == "pass"
    edge(client, "docs.unregistered", "ai.vec")
    r = policy_result(client, rag["id"], "AI-003")
    assert r["status"] == "fail" and "docs.unregistered" in r["message"]
    assert policy_result(client, vec["id"], "AI-003") is None  # rag_corpus 以外は対象外


def test_ai004_embedding_from_pii_requires_pseudonymization(client):
    asset(client, "up.pii", sensitivity="confidential", contains_pii=True, retention_days=1,
          schema=[{"name": "email", "pii": True}])
    vec = ai_asset(client, "ai.vec", "vector_index", sensitivity="confidential", contains_pii=True, retention_days=1)
    emb = ai_asset(client, "ai.emb", "embedding_set", sensitivity="confidential", contains_pii=True, retention_days=1)
    plain = ai_asset(client, "ai.plain", "embedding_set")
    for name in ("ai.vec", "ai.emb"):
        edge(client, "up.pii", name)
    assert policy_result(client, vec["id"], "AI-004")["status"] == "fail"
    assert client.patch(f"{API}/assets/{emb['id']}", headers=ADMIN, json={
        "privacy_technique": "pseudonymized", "privacy_params": {"key_vault_ref": "kv://1"}}).status_code == 200
    assert policy_result(client, emb["id"], "AI-004")["status"] == "pass"
    assert policy_result(client, plain["id"], "AI-004") is None  # 上流に PII が無ければ対象外
    # 種別の違う資産には適用されない
    corpus = ai_asset(client, "ai.rag2", "rag_corpus", sensitivity="confidential", contains_pii=True, retention_days=1)
    edge(client, "up.pii", "ai.rag2")
    assert policy_result(client, corpus["id"], "AI-004") is None


def test_ai_policies_are_global_and_cannot_be_overridden(client):
    ids = {p["id"]: p for p in client.get(f"{API}/policies", headers=ADMIN).json()}
    assert {"AI-001", "AI-002", "AI-003", "AI-004"} <= set(ids)
    assert ids["AI-001"]["check"] == {"type": "ai_training_approved"}
    assert all(ids[p]["scope"] == "global" for p in ("AI-001", "AI-002", "AI-003", "AI-004"))


# ---------------------------------------------------------------- RAG の判定
@pytest.fixture()
def rag(client):
    pub = asset(client, "docs.public", sensitivity="public")
    internal = asset(client, "docs.internal")
    conf = asset(client, "docs.conf", sensitivity="confidential")
    secret = asset(client, "docs.secret", sensitivity="restricted", owner="secret-owner")
    corpus = ai_asset(client, "ai.rag", "rag_corpus", sensitivity="restricted")
    for a in (pub, internal, conf, secret):
        edge(client, a["qualified_name"], "ai.rag")
    outside = asset(client, "docs.outside", sensitivity="public")
    return {"client": client, "pub": pub, "internal": internal, "conf": conf, "secret": secret, "corpus": corpus,
            "outside": outside}


def _authorize(rag, hits, user, headers=CUSTODIAN):
    return rag["client"].post(f"{API}/ai/rag/authorize", headers=headers, json={
        "corpus_asset_id": rag["corpus"]["id"], "user": user, "hit_asset_ids": hits})


def test_rag_filters_by_clearance_and_restricted(rag):
    hits = [rag[k]["id"] for k in ("pub", "internal", "conf", "secret")]
    r = _authorize(rag, hits, {"subject": "alice", "roles": ["consumer"]})
    assert r.status_code == 200, r.text
    assert r.json()["allowed_asset_ids"] == [rag["pub"]["id"], rag["internal"]["id"]]
    assert {d["asset_id"]: d["reason"] for d in r.json()["denied"]} == {
        rag["conf"]["id"]: "clearance", rag["secret"]["id"]: "restricted_not_granted"}
    # clearance を指定しても restricted は承認が無ければ通らない
    r = _authorize(rag, hits, {"subject": "alice", "roles": [], "clearance": "restricted"})
    assert rag["conf"]["id"] in r.json()["allowed_asset_ids"] and rag["secret"]["id"] not in r.json()["allowed_asset_ids"]
    # 資産の当事者と Committee は通る
    r = _authorize(rag, hits, {"subject": "secret-owner", "roles": []})
    assert rag["secret"]["id"] in r.json()["allowed_asset_ids"]
    r = _authorize(rag, hits, {"subject": "chair", "roles": ["committee"]})
    assert r.json()["allowed_asset_ids"] == hits and r.json()["denied"] == []
    # 未知のロールで権限は上がらない
    r = _authorize(rag, hits, {"subject": "x", "roles": ["superuser"]})
    assert rag["conf"]["id"] not in r.json()["allowed_asset_ids"]


def test_rag_reflects_owner_grant(rag):
    client = rag["client"]
    hits = [rag["secret"]["id"]]
    assert _authorize(rag, hits, {"subject": "alice", "roles": []}).json()["allowed_asset_ids"] == []
    exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    r = client.post(f"{API}/assets/{hits[0]}/access-grants", headers=U("secret-owner"), json={
        "subject": "alice", "purpose": "調査", "expires_at": exp})
    assert r.status_code == 201, r.text
    assert _authorize(rag, hits, {"subject": "alice", "roles": []}).json()["allowed_asset_ids"] == hits
    assert _authorize(rag, hits, {"subject": "bob", "roles": []}).json()["allowed_asset_ids"] == []


def test_rag_fails_closed_for_assets_outside_corpus(rag):
    r = _authorize(rag, [rag["outside"]["id"], "none", rag["pub"]["id"], rag["pub"]["id"]], {"subject": "chair", "roles": ["committee"]})
    assert r.json()["allowed_asset_ids"] == [rag["pub"]["id"]]
    assert {d["reason"] for d in r.json()["denied"]} == {"not_in_corpus", "unknown_asset"}


def test_rag_excludes_deleted_assets(rag):
    client, doc = rag["client"], rag["internal"]
    r = client.post(f"{API}/assets/{doc['id']}/lifecycle", headers=COMMITTEE, json={"target_state": "deleted"})
    assert r.status_code == 200, r.text
    r = _authorize(rag, [doc["id"]], {"subject": "chair", "roles": ["committee"]})
    assert r.json() == {"corpus_asset_id": rag["corpus"]["id"], "allowed_asset_ids": [],
                        "denied": [{"asset_id": doc["id"], "reason": "deleted"}]}


def test_rag_permissions_and_validation(rag):
    client, corpus = rag["client"], rag["corpus"]
    user = {"subject": "alice", "roles": []}
    body = {"corpus_asset_id": corpus["id"], "user": user, "hit_asset_ids": []}
    # 実行主体 (Custodian) だけが呼べる
    for headers in (U("ai-owner"), U("ai-steward"), COMMITTEE, ADMIN, U("stranger")):
        assert client.post(f"{API}/ai/rag/authorize", headers=headers, json=body).status_code == 403
    assert client.post(f"{API}/ai/rag/authorize", headers=CUSTODIAN, json=body).json()["allowed_asset_ids"] == []
    # コーパス以外の資産は 422
    other = ai_asset(client, "ai.other", "vector_index")
    assert client.post(f"{API}/ai/rag/authorize", headers=CUSTODIAN, json={**body, "corpus_asset_id": other["id"]}).status_code == 422
    # 本文や Embedding は受け取らない
    assert client.post(f"{API}/ai/rag/authorize", headers=CUSTODIAN, json={**body, "query": "x"}).status_code == 422
    assert client.post(f"{API}/ai/rag/authorize", headers=CUSTODIAN, json={**body, "hit_asset_ids": [{"id": "a", "text": "x"}]}).status_code == 422
    assert client.post(f"{API}/ai/rag/authorize", headers=CUSTODIAN, json={**body, "corpus_asset_id": "none"}).status_code == 404


def test_rag_authorize_is_not_audited(rag):
    client = rag["client"]
    before = client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1}).headers["X-Total-Count"]
    assert _authorize(rag, [rag["pub"]["id"]], {"subject": "alice", "roles": []}).status_code == 200
    assert client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1}).headers["X-Total-Count"] == before
