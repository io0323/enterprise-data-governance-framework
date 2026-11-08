"""Phase 10b (§14 Training Data): 学習利用の承認、AI-001、Model Version の状態遷移."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from edgf.db import engine
from tests.ai_helpers import (ADMIN, API, COMMITTEE, CUSTODIAN, OWNER, STEWARD, U, ai_asset, approve_dataset, asset,
                              dataset_version, edge, model_version, request_approval, run_quality)


def _decide(client, approval_id, headers, approve=True, comment=""):
    return client.post(f"{API}/ai/training-approvals/{approval_id}/decision", headers=headers,
                       json={"approve": approve, "comment": comment})


def _ai001(client, asset_id, headers=ADMIN):
    res = client.get(f"{API}/policies/evaluate/assets/{asset_id}", headers=headers).json()["results"]
    return next((r for r in res if r["policy_id"] == "AI-001"), None)


@pytest.fixture()
def two_owners(client):
    """Owner の異なる 2 つの構成元 (PII なし、internal) → 学習データ。"""
    a = asset(client, "crm.a", owner="owner-a")
    b = asset(client, "crm.b", owner="owner-b")
    for s in (a, b):
        run_quality(client, s["id"])
    ds = ai_asset(client, "ai.train", "training_dataset")
    dv = dataset_version(client, ds["id"], [a["id"], b["id"]])
    return client, ds, dv


@pytest.fixture()
def pii_source(client):
    """PII を含み ai_training が目的に入っている構成元 → Committee の承認も必要。"""
    src = asset(client, "crm.people", owner="owner-a", sensitivity="confidential", contains_pii=True, retention_days=30,
                schema=[{"name": "email", "pii": True, "processing_purposes": ["ai_training"], "legal_basis": "consent"}])
    run_quality(client, src["id"])
    ds = ai_asset(client, "ai.people", "training_dataset")
    dv = dataset_version(client, ds["id"], [src["id"]])
    return client, src, ds, dv


# ---------------------------------------------------------------- AI-001
def test_ai001_self_declared_controls_do_not_pass(client):
    """controls に自己申告しただけでは fail になる。"""
    ds = ai_asset(client, "ai.selfdeclared", "training_dataset")
    r = client.patch(f"{API}/assets/{ds['id']}", headers=ADMIN, json={
        "controls": ["encryption_at_rest", "encryption_in_transit", "masking", "training_approval", "license_verified"]})
    assert r.status_code == 200
    assert _ai001(client, ds["id"])["status"] == "fail"


def test_ai001_passes_only_when_all_owners_and_committee_approve(pii_source):
    client, src, ds, dv = pii_source
    # 2 人目の Owner がいる構成にする
    other = asset(client, "crm.other", owner="owner-b")
    run_quality(client, other["id"])
    dv2 = dataset_version(client, ds["id"], [src["id"], other["id"]], version="v2")
    approval = request_approval(client, dv2["id"]).json()
    assert [s["slot"] for s in approval["decisions"]] == ["committee", "owner:owner-a", "owner:owner-b"]

    assert _decide(client, approval["id"], U("owner-a")).json()["status"] == "pending"
    assert _ai001(client, ds["id"])["status"] == "fail"
    assert _decide(client, approval["id"], U("owner-b")).json()["status"] == "pending"
    assert client.get(f"{API}/ai/dataset-versions/{dv2['id']}", headers=ADMIN).json()["status"] == "draft"
    assert _ai001(client, ds["id"])["status"] == "fail"  # Committee が欠けている

    done = _decide(client, approval["id"], COMMITTEE).json()
    assert done["status"] == "approved"
    v = client.get(f"{API}/ai/dataset-versions/{dv2['id']}", headers=ADMIN).json()
    assert v["status"] == "approved" and v["approved_at"]
    assert _ai001(client, ds["id"])["status"] == "pass"


def test_ai001_out_of_scope_and_unset_kind(client):
    emb = ai_asset(client, "ai.emb", "embedding_set")
    assert _ai001(client, emb["id"]) is None
    legacy = asset(client, "ai.legacy", data_class="ai")
    assert _ai001(client, legacy["id"])["status"] == "fail"


def test_ai001_for_model_uses_approval_records(two_owners):
    client, ds, dv = two_owners
    model = ai_asset(client, "ai.m", "model")
    assert _ai001(client, model["id"])["status"] == "fail"  # 版が無い
    mv = model_version(client, model["id"], [dv["id"]])
    assert mv["status"] == "draft"
    assert _ai001(client, model["id"])["status"] == "fail"  # 学習データが未承認
    approve_dataset(client, dv["id"], ("owner-a", "owner-b"))
    assert _ai001(client, model["id"])["status"] == "pass"
    assert client.post(f"{API}/ai/model-versions/{mv['id']}/approve", headers=OWNER).status_code == 200


# ---------------------------------------------------------------- 承認ワークフロー
def test_missing_one_approver_keeps_draft(two_owners):
    client, ds, dv = two_owners
    approval = request_approval(client, dv["id"]).json()
    assert approval["status"] == "pending"
    assert [s["slot"] for s in approval["decisions"]] == ["owner:owner-a", "owner:owner-b"]  # PII なし: Committee は不要
    assert _decide(client, approval["id"], U("owner-a")).status_code == 200
    assert client.get(f"{API}/ai/dataset-versions/{dv['id']}", headers=ADMIN).json()["status"] == "draft"
    assert _ai001(client, ds["id"])["status"] == "fail"
    # 同じ Owner の 2 回目と、Committee の枠が無い申請への Committee の決裁は 409
    assert _decide(client, approval["id"], U("owner-a")).status_code == 403
    assert _decide(client, approval["id"], COMMITTEE).status_code == 409
    # 権限の無い利用者。Custodian や対象 AI 資産の Owner には承認権限が無い
    for headers in (U("stranger"), CUSTODIAN, OWNER, STEWARD):
        assert _decide(client, approval["id"], headers).status_code == 403
    assert _decide(client, approval["id"], U("owner-b")).json()["status"] == "approved"
    assert _decide(client, approval["id"], U("owner-b")).status_code == 403  # 枠が閉じた
    assert _decide(client, approval["id"], COMMITTEE).status_code == 409


def test_rejection_requires_reason_and_allows_new_request(two_owners):
    client, _, dv = two_owners
    approval = request_approval(client, dv["id"]).json()
    assert _decide(client, approval["id"], U("owner-a"), approve=False).status_code == 422
    r = _decide(client, approval["id"], U("owner-a"), approve=False, comment="利用規約に抵触")
    assert r.json()["status"] == "rejected"
    assert client.get(f"{API}/ai/dataset-versions/{dv['id']}", headers=ADMIN).json()["status"] == "draft"
    assert _decide(client, approval["id"], U("owner-b")).status_code == 409  # 却下で確定済み
    again = request_approval(client, dv["id"])
    assert again.status_code == 201
    assert request_approval(client, dv["id"]).status_code == 409  # 未決の申請は 1 件だけ
    assert client.get(f"{API}/ai/dataset-versions/{dv['id']}/training-approvals", headers=ADMIN).headers["X-Total-Count"] == "2"


def test_request_permissions(two_owners):
    client, _, dv = two_owners
    url = f"{API}/ai/dataset-versions/{dv['id']}/training-approvals"
    body = {"license_status": "verified", "bias_evaluation_ref": "eval://1"}
    assert client.post(url, headers=CUSTODIAN, json=body).status_code == 403
    assert client.post(url, headers=U("stranger"), json=body).status_code == 403
    assert client.post(url, headers=STEWARD, json=body).status_code == 201


def test_prerequisites_are_required(client):
    """ライセンス、同意、品質、バイアス評価は §14.1 で必須。結果は Control Plane が判定する。"""
    src = asset(client, "crm.gap", owner="owner-a", sensitivity="confidential", contains_pii=True, retention_days=30,
                schema=[{"name": "email", "pii": True, "processing_purposes": ["billing"], "legal_basis": "consent"}])
    ds = ai_asset(client, "ai.gap", "training_dataset")
    dv = dataset_version(client, ds["id"], [src["id"]])
    r = request_approval(client, dv["id"], license_status="unverified", bias_evaluation_ref="")
    assert r.status_code == 409 and r.json()["type"].endswith("training-prerequisites-failed")
    text = " ".join(r.json()["blockers"])
    assert "ライセンス" in text and "バイアス" in text and "crm.gap.email" in text and "品質" in text
    run_quality(client, src["id"])
    # 同意以外の法的根拠があれば同意の集計は通る (妥当性は承認者が確認する)
    assert client.patch(f"{API}/assets/{src['id']}", headers=ADMIN, json={"schema": [
        {"name": "email", "pii": True, "processing_purposes": ["billing"], "legal_basis": "legitimate_interest"}]}).status_code == 200
    ok = request_approval(client, dv["id"])
    assert ok.status_code == 201
    assert ok.json()["consent_summary"]["columns"][0]["covered_by"] == "legal_basis"
    assert ok.json()["quality_summary"][0]["score"] == 1.0

    # 申請後に状況が変わると、承認の時点で止まる
    assert client.patch(f"{API}/assets/{src['id']}", headers=ADMIN, json={"schema": [
        {"name": "email", "pii": True, "processing_purposes": ["billing"], "legal_basis": "consent"}]}).status_code == 200
    blocked = _decide(client, ok.json()["id"], U("owner-a"))
    assert blocked.status_code == 409 and "crm.gap.email" in " ".join(blocked.json()["blockers"])


def test_source_without_owner_blocks_request(client):
    src = asset(client, "crm.orphan", owner=None)
    run_quality(client, src["id"])
    ds = ai_asset(client, "ai.orphan", "training_dataset")
    dv = dataset_version(client, ds["id"], [src["id"]])
    r = request_approval(client, dv["id"])
    assert r.status_code == 409 and "Owner" in " ".join(r.json()["blockers"])


def test_committee_required_for_restricted_or_inherited(client):
    secret = asset(client, "crm.secret", owner="owner-a", sensitivity="restricted")
    run_quality(client, secret["id"])
    ds = ai_asset(client, "ai.r", "training_dataset")
    dv = dataset_version(client, ds["id"], [secret["id"]])
    assert "committee" in [s["slot"] for s in request_approval(client, dv["id"]).json()["decisions"]]

    # 構成元の宣言が internal でも、その上流が restricted なら Committee が必要
    upstream = asset(client, "crm.raw", sensitivity="restricted")
    view = asset(client, "crm.view", owner="owner-b")
    run_quality(client, view["id"])
    edge(client, upstream["qualified_name"], view["qualified_name"])
    ds2 = ai_asset(client, "ai.r2", "training_dataset")
    dv2 = dataset_version(client, ds2["id"], [view["id"]])
    assert [s["slot"] for s in request_approval(client, dv2["id"]).json()["decisions"]] == ["committee", "owner:owner-b"]


def test_separation_of_duties_for_committee(pii_source):
    client, _, _, dv = pii_source
    approval = request_approval(client, dv["id"]).json()
    requester_on_committee = U("ai-owner", "committee")
    assert _decide(client, approval["id"], requester_on_committee).status_code == 403
    # Owner の枠を持つ人は Owner として決裁し、同じ人が Committee の枠を埋めることはできない
    both = U("owner-a", "committee")
    assert _decide(client, approval["id"], both).json()["decisions"][1]["decided_by"] == "owner-a"
    r = _decide(client, approval["id"], both)
    assert r.status_code == 403
    assert _decide(client, approval["id"], COMMITTEE).json()["status"] == "approved"


def test_owner_change_after_request_closes_old_slot(two_owners):
    client, _, dv = two_owners
    approval = request_approval(client, dv["id"]).json()
    a_id = approval["decisions"][0]["asset_ids"][0]
    assert client.put(f"{API}/assets/{a_id}/ownership", headers=ADMIN, json={"owner": "owner-new"}).status_code == 200
    assert _decide(client, approval["id"], U("owner-a")).status_code == 403
    assert client.post(f"{API}/ai/training-approvals/{approval['id']}/withdraw", headers=U("stranger")).status_code == 403
    assert client.post(f"{API}/ai/training-approvals/{approval['id']}/withdraw", headers=OWNER).json()["status"] == "withdrawn"
    assert client.post(f"{API}/ai/training-approvals/{approval['id']}/withdraw", headers=OWNER).status_code == 409
    assert [s["slot"] for s in request_approval(client, dv["id"]).json()["decisions"]] == ["owner:owner-b", "owner:owner-new"]


def test_approval_lookup_and_state_guards(two_owners):
    client, _, dv = two_owners
    assert client.get(f"{API}/ai/training-approvals/none", headers=ADMIN).status_code == 404
    approve_dataset(client, dv["id"], ("owner-a", "owner-b"))
    assert request_approval(client, dv["id"]).status_code == 409  # approved の版は再申請できない
    approvals = client.get(f"{API}/ai/dataset-versions/{dv['id']}/training-approvals", headers=ADMIN).json()
    assert client.get(f"{API}/ai/training-approvals/{approvals[0]['id']}", headers=ADMIN).json()["status"] == "approved"
    assert client.get(f"{API}/ai/training-approvals/{approvals[0]['id']}", headers=U("x", clearance="public")).status_code == 403
    events = [e["action"] for e in client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000}).json()]
    assert {"ai.training.request", "ai.training.decision"} <= set(events)


def test_only_training_dataset_can_request(client):
    src = asset(client, "crm.s")
    emb = ai_asset(client, "ai.e", "embedding_set")
    dv = dataset_version(client, emb["id"], [src["id"]])
    assert request_approval(client, dv["id"]).status_code == 422


def test_nested_dataset_version_must_be_approved(client):
    src = asset(client, "crm.base", owner="owner-a")
    run_quality(client, src["id"])
    parent = ai_asset(client, "ai.parent", "training_dataset", owner="parent-owner")
    pv = dataset_version(client, parent["id"], [src["id"]], version="p1")
    child = ai_asset(client, "ai.child", "training_dataset")
    r = client.post(f"{API}/ai/assets/{child['id']}/dataset-versions", headers=U("ai-custodian"), json={
        "version": "c1", "manifest_hash": "ab" * 32, "record_count": 1,
        "sources": [{"asset_id": parent["id"], "kind": "dataset_version", "version": "p1"}]})
    assert r.status_code == 201, r.text
    cv = r.json()
    blocked = request_approval(client, cv["id"])
    assert blocked.status_code == 409 and "ai.parent@p1" in " ".join(blocked.json()["blockers"])
    pr = client.post(f"{API}/ai/dataset-versions/{pv['id']}/training-approvals", headers=U("parent-owner"),
                     json={"license_status": "verified", "bias_evaluation_ref": "e"})
    assert pr.status_code == 201, pr.text
    assert _decide(client, pr.json()["id"], U("owner-a")).json()["status"] == "approved"
    assert request_approval(client, cv["id"]).status_code == 201


# ---------------------------------------------------------------- Model Version の状態遷移
def test_model_cannot_be_approved_with_unapproved_dataset(two_owners):
    client, _, dv = two_owners
    model = ai_asset(client, "ai.model", "model")
    mv = model_version(client, model["id"], [dv["id"]])
    r = client.post(f"{API}/ai/model-versions/{mv['id']}/approve", headers=OWNER)
    assert r.status_code == 409 and r.json()["type"].endswith("model-approval-blocked")
    approve_dataset(client, dv["id"], ("owner-a", "owner-b"))
    assert client.post(f"{API}/ai/model-versions/{mv['id']}/approve", headers=CUSTODIAN).status_code == 403
    assert client.post(f"{API}/ai/model-versions/{mv['id']}/approve", headers=COMMITTEE).status_code == 403
    r = client.post(f"{API}/ai/model-versions/{mv['id']}/approve", headers=OWNER)
    assert r.json()["status"] == "approved" and r.json()["approved_by"] == "ai-owner"
    assert client.post(f"{API}/ai/model-versions/{mv['id']}/approve", headers=OWNER).status_code == 409


def test_model_lifecycle(two_owners):
    client, _, dv = two_owners
    approve_dataset(client, dv["id"], ("owner-a", "owner-b"))
    model = ai_asset(client, "ai.model", "model")
    mv = model_version(client, model["id"], [dv["id"]])
    url = f"{API}/ai/model-versions/{mv['id']}"
    assert client.post(f"{url}/deploy", headers=CUSTODIAN).status_code == 409  # 未承認
    assert client.post(f"{url}/suspend", headers=OWNER, json={"reason": "x"}).status_code == 409
    client.post(f"{url}/approve", headers=OWNER)
    assert client.post(f"{url}/deploy", headers=U("stranger")).status_code == 403
    assert client.post(f"{url}/deploy", headers=CUSTODIAN).json()["status"] == "deployed"
    assert client.post(f"{url}/suspend", headers=OWNER, json={}).status_code == 422
    assert client.post(f"{url}/suspend", headers=CUSTODIAN, json={"reason": "x"}).status_code == 403
    r = client.post(f"{url}/suspend", headers=COMMITTEE, json={"reason": "汚染の疑い"})
    assert r.json()["status"] == "suspended" and r.json()["suspended_reason"] == "汚染の疑い"
    assert client.post(f"{url}/resume", headers=COMMITTEE).status_code == 403
    r = client.post(f"{url}/resume", headers=OWNER)
    assert r.json()["status"] == "deployed" and r.json()["suspended_at"] is None
    assert client.post(f"{url}/retire", headers=CUSTODIAN).status_code == 403
    assert client.post(f"{url}/retire", headers=OWNER).json()["status"] == "retired"
    assert client.post(f"{url}/retire", headers=OWNER).status_code == 409
    assert client.post(f"{url}/resume", headers=OWNER).status_code == 409
    assert client.get(url, headers=ADMIN).json()["retired_at"]


# ---------------------------------------------------------------- 並行実行 (実 DB のみ)
needs_real_concurrency = pytest.mark.skipif(engine.dialect.name == "sqlite",
                                            reason="インメモリの SQLite は 1 接続を共有するため並行実行できない")


@needs_real_concurrency
def test_concurrent_final_approvals_always_finalize(client):
    """最後の承認が同時に来ても、互いの未決を読んで確定し損ねることがない。"""
    owners = [f"owner-{i}" for i in range(6)]
    for i, owner in enumerate(owners):
        run_quality(client, asset(client, f"crm.s{i}", owner=owner)["id"])
    rows = client.get(f"{API}/assets", headers=ADMIN, params={"q": "crm.s", "limit": 100}).json()
    ds = ai_asset(client, "ai.race", "training_dataset")
    for round_no in range(5):
        dv = dataset_version(client, ds["id"], [a["id"] for a in rows], version=f"r{round_no}")
        approval = request_approval(client, dv["id"]).json()
        barrier = threading.Barrier(len(owners))

        def decide(owner: str):
            barrier.wait()
            return _decide(client, approval["id"], U(owner)).status_code

        with ThreadPoolExecutor(len(owners)) as pool:
            codes = list(pool.map(decide, owners))
        assert codes == [200] * len(owners)
        assert client.get(f"{API}/ai/training-approvals/{approval['id']}", headers=ADMIN).json()["status"] == "approved"
        assert client.get(f"{API}/ai/dataset-versions/{dv['id']}", headers=ADMIN).json()["status"] == "approved"


@needs_real_concurrency
def test_concurrent_requests_leave_one_pending(two_owners):
    client, _, dv = two_owners
    barrier = threading.Barrier(4)

    def req(_):
        barrier.wait()
        return request_approval(client, dv["id"]).status_code

    with ThreadPoolExecutor(4) as pool:
        codes = sorted(pool.map(req, range(4)))
    assert codes == [201, 409, 409, 409]
