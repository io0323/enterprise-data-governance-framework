"""Phase 12a (§16 Approval): 汎用の承認エンジンと共通の承認箱."""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text

from edgf.authz import rule
from edgf.auth import CUSTODIAN
from edgf.db import SessionLocal, engine
from edgf.services import approval_types, approvals, lifecycle
from edgf.services.approval_types import ApprovalType, Step, add_business_days
from tests.ai_helpers import ai_asset, dataset_version, model_version, run_quality
from tests.mesh_helpers import add_port, cdm_asset, contract, domain, draft, import_cdm, mark_published, port_asset, product, published, purpose, subscribe
from tests.process_helpers import (ADMIN, API, APPROVALS, COMMITTEE, COMMITTEE2, OWNER, U, asset, audit_actions, decide, future, inbox, issues_of, request_access)


# ---------------------------------------------------------------- access_request
def test_access_request_approved_grants_read(client):
    a = asset(client, sensitivity="restricted")
    alice = U("alice", clearance="confidential")
    assert client.get(f"{API}/assets/{a['id']}", headers=alice).status_code == 403
    req = request_access(client, a["id"])
    assert req["status"] == "pending" and req["steps"] == [{"name": "owner", "rule": "asset_owner", "quorum": 1, "domain": None}]
    assert client.get(f"{API}/assets/{a['id']}", headers=alice).status_code == 403  # 申請だけでは開かない
    done = decide(client, req["id"], OWNER, comment="業務に必要")
    assert done["status"] == "approved" and done["decided_at"] and done["decisions"][0]["decided_by"] == "asset-owner"
    assert client.get(f"{API}/assets/{a['id']}", headers=alice).status_code == 200
    grants = client.get(f"{API}/assets/{a['id']}/access-grants", headers=OWNER).json()
    assert [(g["subject"], g["granted_by"]) for g in grants] == [("alice", "asset-owner")]
    actions = [e["action"] for e in audit_actions(client)]
    assert {"approval.request", "approval.decision", "approval.approved", "access.grant"} <= set(actions)
    # 既存の Owner による直接の付与はそのまま使える
    assert client.post(f"{API}/assets/{a['id']}/access-grants", headers=OWNER, json={"subject": "bob", "purpose": "p", "expires_at": future()}).status_code == 201


def test_access_request_rejected_does_not_grant(client):
    a = asset(client, sensitivity="restricted")
    req = request_access(client, a["id"])
    decide(client, req["id"], OWNER, approve=False, code=422)  # 却下は理由が必須
    done = decide(client, req["id"], OWNER, approve=False, comment="目的が不明")
    assert done["status"] == "rejected"
    assert client.get(f"{API}/assets/{a['id']}", headers=U("alice", clearance="confidential")).status_code == 403
    assert client.get(f"{API}/assets/{a['id']}/access-grants", headers=OWNER).json() == []
    decide(client, req["id"], OWNER, code=409)
    assert request_access(client, a["id"])["status"] == "pending"  # 却下後は再申請できる


def test_request_validation(client):
    a = asset(client)
    request_access(client, a["id"], type_name="retention_delete", code=422)  # 申請できない種別
    request_access(client, a["id"], type_name="unknown", code=422)
    request_access(client, a["id"], expires_at=future(366), code=422)
    request_access(client, a["id"], expires_at=future(-1), code=422)
    request_access(client, a["id"], secret="x", code=422)
    request_access(client, "none", code=404)
    request_access(client, a["id"])
    request_access(client, a["id"], code=409)  # 未決の重複
    no_owner = asset(client, "sales.orphan", owner=None)
    request_access(client, no_owner["id"], code=409)
    r = client.post(f"{API}/assets/{a['id']}/lifecycle", headers=COMMITTEE, json={"target_state": "deleted"})
    assert r.status_code == 200
    request_access(client, a["id"], who="bob", code=409)  # 削除済み
    # 監査ガード: 理由に PII があれば申請ごと拒否する
    b = asset(client, "sales.other")
    r = client.post(APPROVALS, headers=U("alice"), json={"type": "access_request", "asset_id": b["id"], "reason": "a@example.com の調査", "purpose": "p", "expires_at": future()})
    assert r.status_code == 422 and r.json()["type"].endswith("audit-payload-rejected")
    assert client.get(APPROVALS, headers=ADMIN, params={"type": "access_request", "status": "pending"}).headers["X-Total-Count"] == "1"


# ---------------------------------------------------------------- 職務分離
def test_requester_cannot_approve(client):
    a = asset(client, owner="alice")
    req = request_access(client, a["id"], who="alice")
    r = client.post(f"{APPROVALS}/{req['id']}/decision", headers=U("alice"), json={"approve": True})
    assert r.status_code == 403 and "職務分離" in r.json()["detail"]
    assert any(e["action"] == "authz.denied" for e in audit_actions(client))


def test_custodian_cannot_approve(client):
    # ロールでも当事者でも Custodian は承認できない
    a = asset(client)
    req = request_access(client, a["id"])
    decide(client, req["id"], U("asset-custodian", "custodian", "*"), code=403)
    both = asset(client, "sales.both", owner="same-person")
    assert client.put(f"{API}/assets/{both['id']}/ownership", headers=ADMIN, json={"custodian": "same-person"}).status_code == 200
    req = request_access(client, both["id"])
    r = client.post(f"{APPROVALS}/{req['id']}/decision", headers=U("same-person"), json={"approve": True})
    assert r.status_code == 403 and "Custodian" in r.json()["detail"]


def test_custodian_rule_is_rejected_at_load():
    with pytest.raises(ValueError, match="Custodian"):
        approval_types.add_rule("bad", rule(roles=(CUSTODIAN,), approval=True))
    with pytest.raises(ValueError, match="Custodian"):
        approval_types.add_rule("bad", rule(relations=("custodian",), approval=True))
    with pytest.raises(ValueError, match="unknown approver rule"):
        approval_types.register(ApprovalType("x", "d", steps=(Step("s", "none"),)))
    with pytest.raises(ValueError, match="quorum"):
        approval_types.register(ApprovalType("x", "d", steps=(Step("s", "committee", quorum=0),)))


def test_wrong_approver_is_denied(client):
    a = asset(client)
    req = request_access(client, a["id"])
    for who in (U("stranger"), U("x", "owner", "*"), COMMITTEE, ADMIN, U("asset-steward")):
        decide(client, req["id"], who, code=403)
    decide(client, "none", OWNER, code=404)


# ---------------------------------------------------------------- 定足数と複数ステップ
@pytest.fixture()
def quorum_type():
    approval_types.register(ApprovalType("test_quorum", "定足数のテスト", steps=(Step("committee", "committee", quorum=2),)))
    yield
    del approval_types.TYPES["test_quorum"]


def test_quorum(client, quorum_type):
    with SessionLocal() as db:
        req = approvals.create(db, "dev:req", type_name="test_quorum", requested_by="req", target_type="x", target_id="1")
        db.commit()
    assert decide(client, req.id, COMMITTEE)["status"] == "pending"
    decide(client, req.id, COMMITTEE, code=403)  # 同じ人の 2 回目は数えない
    assert decide(client, req.id, COMMITTEE2)["status"] == "approved"
    with SessionLocal() as db:
        with pytest.raises(approvals.ApprovalValidationError):
            approvals.create(db, "dev:req", type_name="test_quorum", requested_by="req", target_type="x", target_id="1", steps=())


def test_reject_within_quorum_closes(client, quorum_type):
    with SessionLocal() as db:
        req = approvals.create(db, "dev:req", type_name="test_quorum", requested_by="req", target_type="x", target_id="1")
        db.commit()
    decide(client, req.id, COMMITTEE)
    assert decide(client, req.id, COMMITTEE2, approve=False, comment="no")["status"] == "rejected"


def test_external_share_needs_owner_and_committee(client):
    a = asset(client)
    req = request_access(client, a["id"], type_name="external_share", recipient="取引先A")
    assert [s["name"] for s in req["steps"]] == ["owner", "committee"]
    decide(client, req["id"], COMMITTEE, code=403)  # 順番: まず Owner
    after = decide(client, req["id"], OWNER)
    assert after["status"] == "pending" and after["current_step"] == 1
    decide(client, req["id"], OWNER, code=403)
    done = decide(client, req["id"], COMMITTEE)
    assert done["status"] == "approved"
    events = [e for e in audit_actions(client) if e["action"] == "share.approved"]
    assert events[0]["payload"]["recipient"] == "取引先A"
    request_access(client, a["id"], type_name="external_share", code=422)  # 提供先は必須


def test_same_person_cannot_fill_both_steps(client):
    a = asset(client, owner="both")
    req = request_access(client, a["id"], type_name="external_share", recipient="取引先A")
    assert decide(client, req["id"], U("both", "committee"))["current_step"] == 1
    decide(client, req["id"], U("both", "committee"), code=403)


def test_external_share_requires_read(client):
    a = asset(client, sensitivity="restricted")
    request_access(client, a["id"], type_name="external_share", recipient="取引先A", code=403)


def test_cancel(client):
    a = asset(client)
    req = request_access(client, a["id"])
    url = f"{APPROVALS}/{req['id']}/cancel"
    assert client.post(url, headers=U("bob")).status_code == 403
    assert client.post(url, headers=COMMITTEE).status_code == 403
    assert client.post(url, headers=U("alice")).json()["status"] == "cancelled"
    assert client.post(url, headers=U("alice")).status_code == 409
    other = request_access(client, a["id"])
    assert client.post(f"{APPROVALS}/{other['id']}/cancel", headers=ADMIN).json()["status"] == "cancelled"
    assert client.post(f"{APPROVALS}/none/cancel", headers=ADMIN).status_code == 404


def test_reads(client):
    a = asset(client, sensitivity="restricted")
    req = request_access(client, a["id"])
    url = f"{APPROVALS}/{req['id']}"
    for who, code in ((U("alice"), 200), (OWNER, 200), (COMMITTEE, 200), (ADMIN, 200), (U("stranger"), 403)):
        assert client.get(url, headers=who).status_code == code
    assert client.get(f"{APPROVALS}/none", headers=ADMIN).status_code == 404
    assert client.get(APPROVALS, headers=U("alice")).headers["X-Total-Count"] == "1"
    assert client.get(APPROVALS, headers=U("stranger")).headers["X-Total-Count"] == "0"
    assert client.get(APPROVALS, headers=COMMITTEE, params={"status": "pending"}).headers["X-Total-Count"] == "1"
    decide(client, req["id"], OWNER)
    assert client.get(url, headers=OWNER).status_code == 200  # 判断した人は決裁後も読める


# ---------------------------------------------------------------- SLA
def test_business_days_skip_weekends():
    friday = datetime(2026, 11, 6, 12)
    assert add_business_days(friday, 3) == datetime(2026, 11, 11, 12)
    assert add_business_days(friday, 0) == friday


def _overdue():
    with engine.begin() as conn:
        conn.execute(text("UPDATE approval_requests SET due_at = :t"), {"t": datetime(2020, 1, 1)})


def test_sla_overdue_opens_issue_and_escalates(client):
    a = asset(client)
    req = request_access(client, a["id"])
    assert req["due_at"] > req["created_at"]
    with SessionLocal() as db:
        assert approvals.escalate_overdue(db, "system:jobs")["overdue"] == []
        db.commit()
    _overdue()
    with SessionLocal() as db:
        result = approvals.escalate_overdue(db, "system:jobs")
        db.commit()
    assert result["overdue"] == [req["id"]] and len(result["issues_opened"]) == 1
    issue = issues_of(client, f"approval:{req['id']}")[0]
    assert issue["assignee"] == "asset-owner" and issue["escalation_level"] == "owner"
    # 重複起票せず、Issue の期限も過ぎるとエスカレーションする
    with SessionLocal() as db:
        assert approvals.escalate_overdue(db, "system:jobs")["issues_opened"] == []
        db.commit()
    with engine.begin() as conn:
        conn.execute(text("UPDATE issues SET due_at = :t WHERE id = :i"), {"t": datetime(2020, 1, 1), "i": issue["id"]})
    with SessionLocal() as db:
        assert approvals.escalate_overdue(db, "system:jobs")["issues_escalated"] == [issue["id"]]
        db.commit()
    assert issues_of(client, f"approval:{req['id']}")[0]["escalation_level"] == "committee"


def test_sla_overdue_for_committee_step_goes_to_committee(client):
    a = asset(client)
    req = request_access(client, a["id"], type_name="external_share", recipient="取引先A")
    decide(client, req["id"], OWNER)
    _overdue()
    with SessionLocal() as db:
        approvals.escalate_overdue(db, "system:jobs")
        db.commit()
    issue = issues_of(client, f"approval:{req['id']}")[0]
    assert issue["escalation_level"] == "committee" and issue["assignee"] == "data-governance-committee"


# ---------------------------------------------------------------- 保持期限の削除
def _retention_request(asset_row):
    with SessionLocal() as db:
        from edgf.models import DataAsset
        req = approvals.create(db, "system:jobs", type_name="retention_delete", requested_by="system:jobs", target_type="asset",
                               target_id=asset_row["id"], asset=db.get(DataAsset, asset_row["id"]), reason="保持期限")
        db.commit()
    return req


@pytest.mark.parametrize("state,events", [
    ("draft", ["lifecycle.delete_requested"]),
    ("active", ["lifecycle.change", "lifecycle.delete_requested"]),
])
def test_retention_delete_uses_lifecycle(client, state, events):
    a = asset(client, retention_days=1)
    if state == "active":
        assert client.post(f"{API}/assets/{a['id']}/lifecycle", headers=ADMIN, json={"target_state": "active"}).status_code == 200
    req = _retention_request(a)
    assert decide(client, req.id, OWNER)["status"] == "approved"
    assert client.get(f"{API}/assets/{a['id']}", headers=ADMIN).json()["lifecycle_state"] == "deleted"
    actions = [e["action"] for e in audit_actions(client, entity_id=a["id"]) if e["action"].startswith("lifecycle.")]
    assert actions[::-1][-len(events):] == events


def test_legal_hold_blocks_retention_delete(client):
    a = asset(client, retention_days=1)
    req = _retention_request(a)
    assert client.post(f"{API}/assets/{a['id']}/legal-hold", headers=COMMITTEE, json={"legal_hold": True, "reason": "訴訟"}).status_code == 200
    r = decide(client, req.id, OWNER, code=409)
    assert "Legal Hold" in r["detail"]
    got = client.get(f"{APPROVALS}/{req.id}", headers=COMMITTEE).json()
    assert got["status"] == "pending" and got["decisions"] == []  # 全体を巻き戻す
    assert client.get(f"{API}/assets/{a['id']}", headers=ADMIN).json()["lifecycle_state"] != "deleted"


def test_request_delete_is_idempotent(client):
    a = asset(client)
    assert client.post(f"{API}/assets/{a['id']}/lifecycle", headers=COMMITTEE, json={"target_state": "deleted"}).status_code == 200
    from edgf.models import DataAsset
    with SessionLocal() as db:
        lifecycle.request_delete(db, "x", db.get(DataAsset, a["id"]), "r")
        db.commit()
    assert len([e for e in audit_actions(client, entity_id=a["id"]) if e["action"] == "lifecycle.delete_requested"]) == 1


# ---------------------------------------------------------------- inbox
@pytest.fixture()
def full_inbox(client):
    """新しいエンジンと既存の 8 種類の案件を 1 人の Owner / Committee に集める。"""
    domain(client)
    a = port_asset(client, "sales.main", owner="po")
    new = request_access(client, a["id"])
    # 1. ポリシー例外
    ex = client.post(f"{API}/policies/exceptions", headers=U("x", "owner", "sales"), json={
        "policy_id": "GOV-001", "asset_id": a["id"], "reason": "移行中", "expires_at": future()})
    assert ex.status_code == 201, ex.text
    # 2. MDM ルール (未承認の生成規則)
    m = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "mdm.party", "name": "p", "data_class": "master",
                                                          "sensitivity": "internal", "owner": "po", "domain": "sales"})
    assert m.status_code == 201, m.text
    # 3. Reference Data (未公開の版)
    ref = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "ref.c", "name": "c", "data_class": "reference",
                                                            "sensitivity": "public", "owner": "po"}).json()
    writer = U("w", "owner", "*")
    assert client.post(f"{API}/reference-data/sets", headers=writer, json={"id": "country", "asset_id": ref["id"], "name": "c"}).status_code == 201
    assert client.post(f"{API}/reference-data/sets/country/versions", headers=writer, json={"codes": [{"code": "JP", "label": "日本"}]}).status_code == 201
    # 4. 学習データの承認 (構成元の Owner が po)
    src = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "crm.src", "name": "s", "data_class": "analytical", "sensitivity": "internal",
                                                            "owner": "po", "contains_pii": True, "retention_days": 1,
                                                            "schema": [{"name": "e", "pii": True, "processing_purposes": ["ai_training"], "legal_basis": "consent"}]}).json()
    assert client.patch(f"{API}/assets/{src['id']}", headers=ADMIN, json={"sensitivity": "confidential"}).status_code == 200
    run_quality(client, src["id"])
    train = ai_asset(client, "ai.train", "training_dataset", sensitivity="confidential")
    dv = dataset_version(client, train["id"], [src["id"]])
    assert client.post(f"{API}/ai/dataset-versions/{dv['id']}/training-approvals", headers=U("ai-owner"), json={
        "license_status": "verified", "bias_evaluation_ref": "e"}).status_code == 201
    # 5. Contract の下書き、6. 利用登録 (confidential なので承認待ち)
    p = product(client)
    secret = port_asset(client, "sales.secret", sensitivity="confidential", owner="po")
    port = add_port(client, p["id"], secret["id"])
    c = contract(client, port["id"])
    purpose(client)
    published(client, c["id"], sensitivity="confidential")
    mark_published(p["id"])
    subscribe(client, "bob", port["id"])
    draft(client, c["id"], "1.0.1", sensitivity="confidential")
    # 7. CDM: 新しい版の再検証で不合格になった Contract
    cdm = cdm_asset(client)
    import_cdm(client, cdm["id"], schema={"type": "object", "properties": {"order_id": {"type": "integer"}}})
    port2 = add_port(client, p["id"], port_asset(client, "sales.third", owner="po")["id"], name="third")
    c2 = contract(client, port2["id"])
    published(client, c2["id"], conforms_to="party@1")
    import_cdm(client, cdm["id"], version="2", schema={"type": "object", "properties": {"need": {"type": "string"}}, "required": ["need"]})
    # 8. ModelRemediation
    pii = asset(client, "crm.people", sensitivity="confidential", contains_pii=True, retention_days=1, schema=[
        {"name": "email", "identifier_type": "email", "processing_purposes": ["ai_training"], "legal_basis": "consent"}])
    t2 = ai_asset(client, "ai.t2", "training_dataset", sensitivity="confidential")
    d2 = dataset_version(client, t2["id"], [pii["id"]])
    model = ai_asset(client, "ai.model", "model", sensitivity="confidential")
    model_version(client, model["id"], [d2["id"]])
    r = client.post(f"{API}/privacy/erasure-requests", headers=ADMIN, json={"identifier_type": "email", "identifier": "a@example.com"})
    for action in ("scope", "approve", "dispatch"):
        assert client.post(f"{API}/privacy/erasure-requests/{r.json()['id']}/{action}", headers=COMMITTEE).status_code == 200
    return {"client": client, "new": new, "main": a}


def test_inbox_has_engine_and_all_eight_sources(full_inbox):
    client = full_inbox["client"]
    owner_items = inbox(client, U("po", "owner", "sales"))
    assert {i["source"] for i in owner_items} == {
        "approval", "mdm_rule", "reference_data", "training_approval", "contract", "subscription", "cdm"}
    committee_items = inbox(client, COMMITTEE)
    assert {"policy_exception", "reference_data", "training_approval", "cdm", "model_remediation"} <= {i["source"] for i in committee_items}
    # 8 種類すべてが出る (Owner と Committee の合計)
    assert {i["source"] for i in owner_items + committee_items} == {
        "approval", "policy_exception", "mdm_rule", "reference_data", "training_approval", "contract", "subscription", "cdm", "model_remediation"}
    assert all(i["action"].startswith(("POST /api/v1/", "PATCH /api/v1/")) for i in owner_items + committee_items)
    engine_item = next(i for i in owner_items if i["source"] == "approval")
    assert engine_item["id"] == full_inbox["new"]["id"] and engine_item["requested_by"] == "alice" and engine_item["due_at"]
    r = client.get(f"{APPROVALS}/inbox", headers=U("po", "owner", "sales"), params={"source": "contract", "limit": 1})
    assert [i["source"] for i in r.json()] == ["contract"] and r.headers["X-Total-Count"] == "1"
    assert inbox(client, U("stranger")) == []


def test_inbox_items_leave_when_decided(full_inbox):
    client = full_inbox["client"]
    who = U("po", "owner", "sales")
    decide(client, full_inbox["new"]["id"], who)
    assert "approval" not in {i["source"] for i in inbox(client, who)}
    # 申請者本人の inbox には出ない
    asset_row = full_inbox["main"]
    own = request_access(client, asset_row["id"], who="po")
    assert own["id"] not in {i["id"] for i in inbox(client, who)}


def test_inbox_is_filtered_by_abac(client):
    # Committee の判断ステップでも、Domain Owner でも、読めない資産の案件は出さない
    domain(client)
    secret = port_asset(client, "sales.secret", sensitivity="restricted", owner="so")
    # Owner 自身には出る
    req = request_access(client, secret["id"])
    assert [i["id"] for i in inbox(client, U("so"))] == [req["id"]]
    # ポリシー例外の判断権限 (admin) があっても、ここでは全資産を読めるので出る
    assert client.post(f"{API}/policies/exceptions", headers=U("x", "owner", "sales"), json={
        "policy_id": "GOV-001", "asset_id": port_asset(client, "sales.pub", owner="po")["id"], "reason": "r", "expires_at": future()}).status_code == 201
    assert "policy_exception" in {i["source"] for i in inbox(client, ADMIN)}
    # 権限はあるが読めない: Product の Owner と Output Port の資産
    p = product(client)
    port = add_port(client, p["id"], secret["id"], name="secret")
    c = contract(client, port["id"])
    draft(client, c["id"], sensitivity="restricted")
    assert inbox(client, U("po", "owner", "sales", clearance="confidential")) == []
    assert client.post(f"{API}/assets/{secret['id']}/access-grants", headers=U("so"), json={"subject": "po", "purpose": "p", "expires_at": future()}).status_code == 201
    assert {i["source"] for i in inbox(client, U("po", "owner", "sales", clearance="confidential"))} == {"contract"}
