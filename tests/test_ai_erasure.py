"""Phase 10e (§12 / §14): 消去と汚染の AI 資産への伝播."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from edgf.db import engine
from tests.ai_helpers import (ADMIN, API, COMMITTEE, CUSTODIAN, HASH, OWNER, STEWARD, U, ai_asset, approve_dataset, asset,
                              dataset_version, edge, model_version, run_quality)

PRIV = f"{API}/privacy"
RAW = "Ai.Erasure.Person@Example.com"
COMMITTEE2 = U("vice-chair", "committee")
PII_SCHEMA = [{"name": "email", "identifier_type": "email", "pii_category": "contact",
               "processing_purposes": ["ai_training"], "legal_basis": "consent"}]


def _future(days=30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


@pytest.fixture()
def world(client):
    """PII の起点 → Embedding → ベクトル → RAG、および学習データセット → モデル。

    ベクトルと RAG は contains_pii を宣言していない (宣言に関わらずタスクを発行することの確認)。
    """
    src = asset(client, "crm.people", sensitivity="confidential", contains_pii=True, retention_days=30, schema=PII_SCHEMA,
                owner="src-owner", custodian="src-custodian")
    run_quality(client, src["id"])
    emb = ai_asset(client, "ai.emb", "embedding_set", sensitivity="confidential", privacy_technique="pseudonymized",
                   privacy_params={"key_vault_ref": "kv://1"}, retention_days=30)
    vec = ai_asset(client, "ai.vec", "vector_index", sensitivity="confidential")
    rag = ai_asset(client, "ai.rag", "rag_corpus", sensitivity="confidential")
    for s, t in (("crm.people", "ai.emb"), ("ai.emb", "ai.vec"), ("ai.vec", "ai.rag")):
        edge(client, s, t)
    train = ai_asset(client, "ai.train", "training_dataset", sensitivity="confidential")
    dv = dataset_version(client, train["id"], [src["id"]], version="v1")
    approve_dataset(client, dv["id"], ("src-owner",), committee=True)
    model = ai_asset(client, "ai.model", "model", sensitivity="confidential")
    mv = model_version(client, model["id"], [dv["id"]], version="m1")
    assert client.post(f"{API}/ai/model-versions/{mv['id']}/approve", headers=OWNER).status_code == 200
    assert client.post(f"{API}/ai/model-versions/{mv['id']}/deploy", headers=CUSTODIAN).status_code == 200
    return {"client": client, "src": src, "emb": emb, "vec": vec, "rag": rag, "train": train, "dv": dv, "model": model, "mv": mv}


def _receive(client) -> str:
    r = client.post(f"{PRIV}/erasure-requests", headers=ADMIN, json={"identifier_type": "email", "identifier": RAW})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _step(client, rid, action, who=COMMITTEE, code=200):
    r = client.post(f"{PRIV}/erasure-requests/{rid}/{action}", headers=who)
    assert r.status_code == code, r.text
    return r.json()


def _dispatch(client) -> tuple[str, dict]:
    rid = _receive(client)
    _step(client, rid, "scope")
    _step(client, rid, "approve")
    return rid, _step(client, rid, "dispatch")


def _remediations(client, rid, headers=COMMITTEE) -> list[dict]:
    return client.get(f"{API}/ai/remediations", headers=headers, params={"erasure_request_id": rid}).json()


def _answer(client, custodian, task_id) -> None:
    assert client.post(f"{PRIV}/erasure-tasks/{task_id}/ack", headers=U(custodian)).status_code == 200
    r = client.post(f"{PRIV}/erasure-tasks/{task_id}/complete", headers=U(custodian),
                    json={"records_deleted": 1, "evidence_hash": HASH})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- E2E
def test_erasure_e2e_propagates_to_ai_assets(world):
    client = world["client"]
    rid = _receive(client)

    # 特定の段階: タスクの計画と影響の予定が見える。版はまだ変わらない
    scoped = _step(client, rid, "scope")
    tasks = {t["qualified_name"]: t for t in scoped["tasks"]}
    assert set(tasks) == {"crm.people", "ai.emb", "ai.vec", "ai.rag", "ai.train"}  # モデルにはタスクを発行しない
    assert tasks["ai.vec"]["ai_kind"] == "vector_index" and tasks["ai.vec"]["origin"] == "lineage"
    assert tasks["ai.train"]["dataset_version_ids"] == [world["dv"]["id"]]
    assert scoped["ai_impact"]["model_version_ids"] == [world["mv"]["id"]]
    assert client.get(f"{API}/ai/dataset-versions/{world['dv']['id']}", headers=ADMIN).json()["status"] == "approved"
    assert _remediations(client, rid) == []

    # 発行: 版が superseded になり、モデル版に未決の Remediation ができる。モデル自体は変わらない
    _step(client, rid, "approve")
    dispatched = _step(client, rid, "dispatch")
    assert dispatched["status"] == "dispatched"
    v = client.get(f"{API}/ai/dataset-versions/{world['dv']['id']}", headers=ADMIN).json()
    assert v["status"] == "superseded" and v["superseded_reason"] == f"erasure:{rid}" and v["superseded_at"]
    rems = _remediations(client, rid)
    assert [(r["model_version_id"], r["status"]) for r in rems] == [(world["mv"]["id"], "pending")]
    assert client.get(f"{API}/ai/model-versions/{world['mv']['id']}", headers=ADMIN).json()["status"] == "deployed"

    # Data Plane 向けのタスクに主体の参照と種別が付く
    work = client.get(f"{PRIV}/erasure-tasks", headers=CUSTODIAN, params={"status": "pending"}).json()
    assert {w["qualified_name"] for w in work} == {"ai.emb", "ai.vec", "ai.rag", "ai.train"}
    assert all(w["subject_ref"] == dispatched["subject_ref"] for w in work)
    for w in work:
        _answer(client, "ai-custodian", w["id"])
    _answer(client, "src-custodian", client.get(f"{PRIV}/erasure-tasks", headers=U("src-custodian"), params={"status": "pending"}).json()[0]["id"])

    # 消去証明: AI 資産のタスクの結果と Remediation の状態
    cert = client.get(f"{PRIV}/erasure-requests/{rid}/certificate", headers=COMMITTEE).json()
    assert {a["qualified_name"]: a["status"] for a in cert["ai_assets"]} == {
        "ai.emb": "completed", "ai.vec": "completed", "ai.rag": "completed", "ai.train": "completed"}
    assert cert["dataset_versions"][0]["status"] == "superseded"
    assert [r["status"] for r in cert["model_remediations"]] == ["pending"]
    assert cert["model_remediation_open"] is True
    assert RAW.lower() not in str(cert).lower()

    # 判断すると証明の内容とハッシュも変わる
    rem = _remediations(client, rid)[0]
    r = client.post(f"{API}/ai/remediations/{rem['id']}/decision", headers=COMMITTEE, json={"decision": "unlearn"})
    assert r.json()["status"] == "unlearn"
    cert2 = client.get(f"{PRIV}/erasure-requests/{rid}/certificate", headers=COMMITTEE).json()
    assert cert2["model_remediations"][0]["status"] == "unlearn" and cert2["certificate_hash"] != cert["certificate_hash"]


def test_superseded_version_cannot_be_used_for_training(world):
    client = world["client"]
    _dispatch(client)
    # 新しいモデル版に使えない、再申請できない
    r = client.post(f"{API}/ai/assets/{world['model']['id']}/model-versions", headers=CUSTODIAN, json={
        "version": "m2", "dataset_version_ids": [world["dv"]["id"]]})
    assert r.status_code == 409
    r = client.post(f"{API}/ai/dataset-versions/{world['dv']['id']}/training-approvals", headers=OWNER, json={
        "license_status": "verified", "bias_evaluation_ref": "e"})
    assert r.status_code == 409
    # AI-001: 使える版が無くなる
    res = client.get(f"{API}/policies/evaluate/assets/{world['train']['id']}", headers=ADMIN).json()["results"]
    assert next(x for x in res if x["policy_id"] == "AI-001")["status"] == "fail"
    # 停止したモデルは学習データが superseded なので再開できない
    url = f"{API}/ai/model-versions/{world['mv']['id']}"
    assert client.post(f"{url}/suspend", headers=OWNER, json={"reason": "消去の影響"}).status_code == 200
    r = client.post(f"{url}/resume", headers=OWNER)
    assert r.status_code == 409 and r.json()["type"].endswith("model-approval-blocked")


def test_rejected_request_leaves_ai_assets_untouched(world):
    client = world["client"]
    rid = _receive(client)
    _step(client, rid, "scope")
    r = client.post(f"{PRIV}/erasure-requests/{rid}/reject", headers=COMMITTEE, json={"reason": "法的義務"})
    assert r.status_code == 200
    assert client.get(f"{API}/ai/dataset-versions/{world['dv']['id']}", headers=ADMIN).json()["status"] == "approved"
    assert _remediations(client, rid) == []


def test_legal_hold_withholds_task_but_still_supersedes_version(world):
    """Legal Hold のデータは削除しないが、学習には使わせない。"""
    client = world["client"]
    assert client.post(f"{API}/assets/{world['train']['id']}/legal-hold", headers=COMMITTEE, json={
        "legal_hold": True, "reason": "訴訟対応"}).status_code == 200
    _, req = _dispatch(client)
    task = next(t for t in req["tasks"] if t["qualified_name"] == "ai.train")
    assert task["status"] == "withheld"
    assert client.get(f"{API}/ai/dataset-versions/{world['dv']['id']}", headers=ADMIN).json()["status"] == "superseded"


def test_versions_registered_after_scope_are_included(world):
    client = world["client"]
    rid = _receive(client)
    _step(client, rid, "scope")
    late = dataset_version(client, world["train"]["id"], [world["src"]["id"]], version="v2")
    late_model = model_version(client, world["model"]["id"], [late["id"]], version="m2")
    _step(client, rid, "approve")
    req = _step(client, rid, "dispatch")
    assert client.get(f"{API}/ai/dataset-versions/{late['id']}", headers=ADMIN).json()["status"] == "superseded"
    assert {r["model_version_id"] for r in _remediations(client, rid)} == {world["mv"]["id"], late_model["id"]}
    task = next(t for t in req["tasks"] if t["qualified_name"] == "ai.train")
    assert set(task["dataset_version_ids"]) == {world["dv"]["id"], late["id"]}


def test_retired_model_gets_no_remediation_but_unrelated_version_is_kept(world):
    client = world["client"]
    other_src = asset(client, "crm.other")
    other_dv = dataset_version(client, world["train"]["id"], [other_src["id"]], version="other")
    unrelated = model_version(client, world["model"]["id"], [other_dv["id"]], version="unrelated")
    retired = model_version(client, world["model"]["id"], [world["dv"]["id"]], version="old")
    assert client.post(f"{API}/ai/model-versions/{retired['id']}/retire", headers=OWNER).status_code == 200
    rid, _ = _dispatch(client)
    assert [r["model_version_id"] for r in _remediations(client, rid)] == [world["mv"]["id"]]
    # 版の構成元が無関係なら superseded にしない
    assert client.get(f"{API}/ai/dataset-versions/{other_dv['id']}", headers=ADMIN).json()["status"] == "draft"
    assert client.get(f"{API}/ai/model-versions/{unrelated['id']}", headers=ADMIN).json()["status"] == "draft"


def test_model_with_identifier_column_has_no_task_and_unversioned_model_warns(client):
    ai_asset(client, "ai.direct", "model", sensitivity="confidential", contains_pii=True, retention_days=1, schema=PII_SCHEMA)
    src = asset(client, "crm.src", sensitivity="confidential", contains_pii=True, retention_days=1, schema=PII_SCHEMA)
    edge(client, src["qualified_name"], "ai.direct")
    rid = _receive(client)
    scoped = _step(client, rid, "scope")
    assert {t["qualified_name"] for t in scoped["tasks"]} == {"crm.src"}
    assert scoped["ai_impact"]["unversioned_models"] == ["ai.direct"]
    assert any("ai.direct" in w for w in scoped["warnings"])


def test_pending_training_approval_is_withdrawn(world):
    client = world["client"]
    dv2 = dataset_version(client, world["train"]["id"], [world["src"]["id"]], version="v2")
    r = client.post(f"{API}/ai/dataset-versions/{dv2['id']}/training-approvals", headers=OWNER, json={
        "license_status": "verified", "bias_evaluation_ref": "e"})
    assert r.status_code == 201
    _dispatch(client)
    assert client.get(f"{API}/ai/training-approvals/{r.json()['id']}", headers=ADMIN).json()["status"] == "withdrawn"


# ---------------------------------------------------------------- Remediation の判断
@pytest.fixture()
def remediation(world):
    rid, _ = _dispatch(world["client"])
    return {**world, "rid": rid, "rem": _remediations(world["client"], rid)[0]}


def _decide(client, rem_id, body, who=COMMITTEE):
    return client.post(f"{API}/ai/remediations/{rem_id}/decision", headers=who, json=body)


def test_accept_requires_reason_and_deadline(remediation):
    client, rem = remediation["client"], remediation["rem"]
    assert _decide(client, rem["id"], {"decision": "accept", "reason": "影響は軽微"}).status_code == 422  # 期限なし
    assert _decide(client, rem["id"], {"decision": "accept", "accept_until": _future()}).status_code == 422  # 理由なし
    assert _decide(client, rem["id"], {"decision": "accept", "reason": "r", "accept_until": _future(366)}).status_code == 422
    assert _decide(client, rem["id"], {"decision": "accept", "reason": "r", "accept_until": _future(-1)}).status_code == 422
    assert _decide(client, rem["id"], {"decision": "retrain", "accept_until": _future()}).status_code == 422
    assert _decide(client, rem["id"], {"decision": "ignore"}).status_code == 422
    r = _decide(client, rem["id"], {"decision": "accept", "reason": "次回の学習で解消", "accept_until": _future(365)})
    assert r.status_code == 200 and r.json()["status"] == "accepted" and r.json()["decided_by"] == "chair"


def test_only_committee_can_decide(remediation):
    client, rem = remediation["client"], remediation["rem"]
    for who in (OWNER, STEWARD, CUSTODIAN, ADMIN, U("x", "owner,steward,custodian", "*")):
        assert _decide(client, rem["id"], {"decision": "retrain"}, who).status_code == 403
    assert _decide(client, "none", {"decision": "retrain"}).status_code == 404
    assert client.get(f"{API}/ai/remediations/{rem['id']}", headers=OWNER).status_code == 200
    assert client.get(f"{API}/ai/remediations/none", headers=OWNER).status_code == 404


def test_retrain_flow(remediation):
    client, rem = remediation["client"], remediation["rem"]
    url = f"{API}/ai/remediations/{rem['id']}/complete"
    assert client.post(url, headers=OWNER, json={}).status_code == 409  # 判断前
    assert _decide(client, rem["id"], {"decision": "retrain", "reason": "主体が多い"}).json()["status"] == "retrain"
    assert client.post(url, headers=U("stranger"), json={}).status_code == 403
    assert client.post(url, headers=OWNER, json={}).status_code == 422
    assert client.post(url, headers=OWNER, json={"replacement_model_version_id": remediation["mv"]["id"]}).status_code == 422
    # 新しい学習データで再学習した版だけが置き換えになる
    train = remediation["train"]
    dv3 = dataset_version(client, train["id"], [remediation["src"]["id"]], version="v3")
    m3 = model_version(client, remediation["model"]["id"], [dv3["id"]], version="m3")
    assert client.post(url, headers=OWNER, json={"replacement_model_version_id": m3["id"]}).status_code == 409  # 未承認
    approve_dataset(client, dv3["id"], ("src-owner",), committee=True)
    assert client.post(f"{API}/ai/model-versions/{m3['id']}/approve", headers=OWNER).status_code == 200
    r = client.post(url, headers=CUSTODIAN, json={"replacement_model_version_id": m3["id"]})
    assert r.status_code == 200 and r.json()["status"] == "completed" and r.json()["replacement_model_version_id"] == m3["id"]
    assert _decide(client, rem["id"], {"decision": "unlearn"}).status_code == 409  # 完了後は変更不可
    assert client.post(url, headers=OWNER, json={}).status_code == 409
    for custodian in ("ai-custodian", "src-custodian"):
        for work in client.get(f"{PRIV}/erasure-tasks", headers=U(custodian), params={"status": "pending"}).json():
            _answer(client, custodian, work["id"])
    cert = client.get(f"{PRIV}/erasure-requests/{remediation['rid']}/certificate", headers=COMMITTEE).json()
    assert cert["model_remediation_open"] is False
    assert cert["model_remediations"][0]["replacement_model_version_id"] == m3["id"]


def test_replacement_must_not_use_superseded_dataset(remediation):
    client, rem = remediation["client"], remediation["rem"]
    assert _decide(client, rem["id"], {"decision": "retrain"}).status_code == 200
    dv3 = dataset_version(client, remediation["train"]["id"], [remediation["src"]["id"]], version="v3")
    approve_dataset(client, dv3["id"], ("src-owner",), committee=True)
    m3 = model_version(client, remediation["model"]["id"], [dv3["id"]], version="m3")
    assert client.post(f"{API}/ai/model-versions/{m3['id']}/approve", headers=OWNER).status_code == 200
    with engine.begin() as conn:
        conn.execute(text("UPDATE dataset_versions SET status = 'superseded' WHERE id = :id"), {"id": dv3["id"]})
    r = client.post(f"{API}/ai/remediations/{rem['id']}/complete", headers=OWNER, json={"replacement_model_version_id": m3["id"]})
    assert r.status_code == 409 and "superseded" in r.json()["detail"]


def test_unlearn_flow(remediation):
    client, rem = remediation["client"], remediation["rem"]
    url = f"{API}/ai/remediations/{rem['id']}/complete"
    assert _decide(client, rem["id"], {"decision": "unlearn"}).status_code == 200
    assert client.post(url, headers=OWNER, json={}).status_code == 422
    assert client.post(url, headers=OWNER, json={"evidence_hash": "xyz"}).status_code == 422
    r = client.post(url, headers=OWNER, json={"evidence_hash": HASH})
    assert r.json()["status"] == "completed" and r.json()["evidence_hash"] == HASH


def _issues_of(client, ref):
    rows = client.get(f"{API}/issues", headers=ADMIN, params={"limit": 1000}).json()
    return [i for i in rows if i["source_ref"] == ref]


def test_overdue_pending_opens_issue_and_escalates(remediation):
    client, rem = remediation["client"], remediation["rem"]
    url = f"{API}/ai/remediations/escalate-overdue"
    assert client.post(url, headers=OWNER).status_code == 403
    assert client.post(url, headers=COMMITTEE).json()["issues_opened"] == []  # 期限内
    with engine.begin() as conn:
        conn.execute(text("UPDATE model_remediations SET due_at = :t"), {"t": datetime(2020, 1, 1)})
    result = client.post(url, headers=COMMITTEE).json()
    assert result["overdue"] == [rem["id"]] and len(result["issues_opened"]) == 1
    issue = _issues_of(client, f"ai-remediation:{rem['id']}")[0]
    assert issue["escalation_level"] == "committee" and issue["assignee"] == "data-governance-committee"
    assert client.post(url, headers=COMMITTEE).json()["issues_opened"] == []  # 重複起票しない


def test_overdue_decided_remediation_goes_to_owner_then_escalates(remediation):
    client, rem = remediation["client"], remediation["rem"]
    assert _decide(client, rem["id"], {"decision": "retrain"}).status_code == 200
    with engine.begin() as conn:
        conn.execute(text("UPDATE model_remediations SET due_at = :t"), {"t": datetime(2020, 1, 1)})
    url = f"{API}/ai/remediations/escalate-overdue"
    assert len(client.post(url, headers=COMMITTEE).json()["issues_opened"]) == 1
    issue = _issues_of(client, f"ai-remediation:{rem['id']}")[0]
    assert issue["escalation_level"] == "owner" and issue["assignee"] == "ai-owner"
    with engine.begin() as conn:
        conn.execute(text("UPDATE issues SET due_at = :t WHERE id = :id"), {"t": datetime(2020, 1, 1), "id": issue["id"]})
    assert client.post(url, headers=COMMITTEE).json()["issues_escalated"] == [issue["id"]]


def test_accept_expires_back_to_pending(remediation):
    """恒久的な許容は不可: 期限が切れると未決に戻り、Issue が起票される。"""
    client, rem = remediation["client"], remediation["rem"]
    assert _decide(client, rem["id"], {"decision": "accept", "reason": "r", "accept_until": _future()}).status_code == 200
    url = f"{API}/ai/remediations/escalate-overdue"
    assert client.post(url, headers=COMMITTEE).json()["accept_expired"] == []
    with engine.begin() as conn:
        conn.execute(text("UPDATE model_remediations SET accept_until = :t, due_at = :t"), {"t": datetime(2020, 1, 1)})
    result = client.post(url, headers=COMMITTEE).json()
    assert result["accept_expired"] == [rem["id"]] and len(result["issues_opened"]) == 1
    assert client.get(f"{API}/ai/remediations/{rem['id']}", headers=COMMITTEE).json()["status"] == "pending"


def test_remediation_list_follows_abac(remediation):
    client, rem = remediation["client"], remediation["rem"]
    assert client.get(f"{API}/ai/remediations", headers=U("reader", clearance="internal")).json() == []
    assert client.get(f"{API}/ai/remediations/{rem['id']}", headers=U("reader", clearance="internal")).status_code == 403
    r = client.get(f"{API}/ai/remediations", headers=COMMITTEE, params={"status": "pending", "asset_id": rem["asset_id"]})
    assert r.headers["X-Total-Count"] == "1"
    events = [e["action"] for e in client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000}).json()]
    assert {"ai.remediation.open", "ai.dataset_version.supersede"} <= set(events)


# ---------------------------------------------------------------- インシデント
@pytest.fixture()
def incident_world(client):
    client.post(f"{API}/domains", headers=ADMIN, json={"name": "ml"})
    src = asset(client, "crm.src", domain="ml", owner="src-owner")
    run_quality(client, src["id"])
    train = ai_asset(client, "ai.train", "training_dataset", domain="ml")
    v1 = dataset_version(client, train["id"], [src["id"]], version="v1")
    v2 = dataset_version(client, train["id"], [src["id"]], version="v2")
    approve_dataset(client, v1["id"])
    model = ai_asset(client, "ai.model", "model", domain="ml", owner="model-owner")
    m1 = model_version(client, model["id"], [v1["id"]], version="m1")
    m2 = model_version(client, model["id"], [v2["id"]], version="m2")
    assert client.post(f"{API}/ai/model-versions/{m1['id']}/approve", headers=U("model-owner")).status_code == 200
    return {"client": client, "src": src, "train": train, "v1": v1, "v2": v2, "model": model, "m1": m1, "m2": m2}


def _incident(client, who, **body):
    return client.post(f"{API}/ai/incidents", headers=who, json={"kind": "data_poisoning", "title": "汚染の疑い", **body})


def test_incident_from_dataset_version_identifies_and_suspends(incident_world):
    w = incident_world
    client = w["client"]
    # 起点の版から影響するモデル版を特定する (別の版のモデルは含めない)
    r = _incident(client, COMMITTEE, origin_dataset_version_id=w["v1"]["id"], suspend_model_version_ids=[w["m1"]["id"]],
                  description="ラベルの改ざん")
    assert r.status_code == 201, r.text
    inc = r.json()
    assert inc["affected_model_version_ids"] == [w["m1"]["id"]] and inc["suspended_model_version_ids"] == [w["m1"]["id"]]
    assert inc["origin_asset_id"] == w["train"]["id"]
    assert client.get(f"{API}/ai/model-versions/{w['m1']['id']}", headers=ADMIN).json()["status"] == "suspended"
    # Owner と Committee 宛ての Issue
    rows = client.get(f"{API}/issues", headers=ADMIN, params={"limit": 1000}).json()
    mine = [i for i in rows if i["id"] in inc["issue_ids"]]
    assert {"model-owner", "ai-owner", "data-governance-committee"} == {i["assignee"] for i in mine}
    assert all("ラベルの改ざん" not in i["description"] for i in mine)
    assert client.get(f"{API}/ai/incidents/{inc['id']}", headers=ADMIN).json()["description"] == "ラベルの改ざん"
    assert client.get(f"{API}/ai/incidents", headers=ADMIN).headers["X-Total-Count"] == "1"
    assert client.get(f"{API}/ai/incidents/none", headers=ADMIN).status_code == 404


def test_incident_from_asset_covers_all_versions(incident_world):
    w = incident_world
    r = _incident(w["client"], U("st", "steward", "ml"), origin_asset_id=w["src"]["id"])
    assert r.status_code == 201, r.text
    assert set(r.json()["affected_model_version_ids"]) == {w["m1"]["id"], w["m2"]["id"]}
    assert r.json()["suspended_model_version_ids"] == []


def test_incident_nested_dataset_version(incident_world):
    w = incident_world
    client = w["client"]
    child = ai_asset(client, "ai.child", "training_dataset", domain="ml")
    r = client.post(f"{API}/ai/assets/{child['id']}/dataset-versions", headers=CUSTODIAN, json={
        "version": "c1", "manifest_hash": HASH, "record_count": 1,
        "sources": [{"asset_id": w["train"]["id"], "kind": "dataset_version", "version": "v1"}]})
    assert r.status_code == 201
    other = ai_asset(client, "ai.other_model", "model", domain="ml")
    m3 = model_version(client, other["id"], [r.json()["id"]], version="m3")
    inc = _incident(client, COMMITTEE, origin_dataset_version_id=w["v1"]["id"]).json()
    assert set(inc["affected_model_version_ids"]) == {w["m1"]["id"], m3["id"]}


def test_incident_permissions_and_validation(incident_world):
    w = incident_world
    client = w["client"]
    origin = {"origin_asset_id": w["src"]["id"]}
    assert _incident(client, U("x", "steward", "other"), **origin).status_code == 403
    assert _incident(client, U("x"), **origin).status_code == 403
    assert _incident(client, U("cust", "custodian", "ml"), **origin).status_code == 403
    assert _incident(client, COMMITTEE).status_code == 422  # 起点なし
    assert _incident(client, COMMITTEE, origin_asset_id=w["src"]["id"], origin_dataset_version_id=w["v1"]["id"]).status_code == 422
    assert _incident(client, COMMITTEE, origin_dataset_version_id="none").status_code == 404
    assert _incident(client, COMMITTEE, origin_asset_id="none").status_code == 404
    # 影響を受けないモデル版は停止できない
    assert _incident(client, COMMITTEE, origin_dataset_version_id=w["v1"]["id"], suspend_model_version_ids=[w["m2"]["id"]]).status_code == 422
    # 起票はできても、モデルの停止権限が無ければ全体を拒否する (一部だけ適用しない)
    r = _incident(client, U("st", "steward", "ml"), origin_asset_id=w["src"]["id"], suspend_model_version_ids=[w["m1"]["id"]])
    assert r.status_code == 403
    assert client.get(f"{API}/ai/model-versions/{w['m1']['id']}", headers=ADMIN).json()["status"] == "approved"
    assert client.get(f"{API}/ai/incidents", headers=ADMIN).json() == []
    # draft の版は停止できない
    assert _incident(client, COMMITTEE, origin_asset_id=w["src"]["id"], suspend_model_version_ids=[w["m2"]["id"]]).status_code == 409
    # 起票者でも読めない起点のインシデントは見えない
    assert client.get(f"{API}/ai/incidents", headers=U("reader", clearance="public")).json() == []


# ---------------------------------------------------------------- 同意の撤回
def test_withdrawing_ai_training_notifies_ai_asset_owners(world):
    client = world["client"]
    extra = ai_asset(client, "ai.card_only", "training_dataset", owner="card-owner")
    assert client.put(f"{API}/ai/assets/{extra['id']}/dataset-card", headers=U("card-owner"), json={
        "purpose": "p", "consent_purpose_id": "ai_training"}).status_code == 200
    unrelated = ai_asset(client, "ai.unrelated", "embedding_set", owner="other-owner")
    r = client.post(f"{PRIV}/consents", headers=COMMITTEE, json={
        "identifier_type": "email", "identifier": RAW, "purpose_id": "ai_training", "status": "withdrawn"})
    assert r.status_code == 201, r.text
    rows = {i["asset_id"]: i for i in client.get(f"{API}/issues", headers=ADMIN, params={"limit": 1000}).json()
            if i["source_ref"] == "consent:ai_training"}
    for key in ("emb", "vec", "rag", "train", "model"):
        assert rows[world[key]["id"]]["assignee"] == "ai-owner"
    assert rows[extra["id"]]["assignee"] == "card-owner"
    assert unrelated["id"] not in rows
    assert set(r.json()["issues_opened"]) == {i["id"] for i in rows.values()}
    assert RAW.lower() not in str(rows).lower()
    # 2 回目は重複起票しない。他の目的の撤回では AI 資産に起票しない
    again = client.post(f"{PRIV}/consents", headers=COMMITTEE, json={
        "identifier_type": "email", "identifier": "other@example.com", "purpose_id": "ai_training", "status": "withdrawn"})
    assert again.json()["issues_opened"] == []
    client.post(f"{PRIV}/purposes", headers=COMMITTEE, json={"id": "billing", "description": "d", "default_legal_basis": "consent"})
    other = client.post(f"{PRIV}/consents", headers=COMMITTEE, json={
        "identifier_type": "email", "identifier": RAW, "purpose_id": "billing", "status": "withdrawn"})
    assert other.json()["issues_opened"] == []
