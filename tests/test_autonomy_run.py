"""Phase 19b: プレイブックの実行、dry_run、取り消し、安全装置 (docs/adr/0001)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from edgf import authz
from edgf.auth import Principal
from edgf.db import SessionLocal
from edgf.errors import ProblemError
from edgf.jobs import run_job
from edgf.models import ApprovalRequest, AutonomyControl, AutonomyPlaybookState, DataAsset, Issue, RemediationRun, utcnow
from edgf.services import approvals, autonomy, autonomy_catalog
from tests.autonomy_helpers import (AUT, CHAIR, audit_count, audits, domain, grant, grant_row, owners, recovered,
                                    governance_state, run_now, runs, stale, use_playbooks)
from tests.process_helpers import ADMIN, API, U, asset

OWNER = U("asset-owner")


def owned_asset(client, qn="sales.orders", **kw) -> dict:
    return asset(client, qn, **kw)


# ---------------------------------------------------------------- 有効・無効
def test_disabled_does_nothing(client):
    """EDGF_AUTONOMY_ENABLED=false (既定) なら、dry_run も含めて何も記録しない。"""
    a = owned_asset(client)
    gid = grant(a["id"])
    use_playbooks(stale(mode="dry_run"), stale(mode="active") | {"id": "PB-STALE-2"})
    before, audit_before = governance_state(), audit_count()
    assert run_now() == {"status": "disabled"}
    assert run_job("autonomy_run", force=True)["result"] == {"status": "disabled"}
    assert runs() == [] and grant_row(gid).revoked_at is None and audit_count() == audit_before
    assert governance_state() == before
    with SessionLocal() as db:
        assert db.get(AutonomyControl, 1) is None and db.scalars(select(AutonomyPlaybookState)).all() == []


def test_no_playbooks_until_bundle_has_them(client, autonomy_on):
    a = owned_asset(client)
    grant(a["id"])
    use_playbooks()
    result = run_now()
    assert result["status"] == "ran" and result["playbooks"] == {} and runs() == []


# ---------------------------------------------------------------- dry_run
def test_dry_run_changes_nothing_and_records_the_intended_operation(client, autonomy_on):
    a = owned_asset(client)
    gid = grant(a["id"])
    use_playbooks(stale(mode="dry_run"))
    audit_before, snapshot = audit_count(), governance_state()
    result = run_now()
    assert result["playbooks"]["PB-STALE"] == {"candidates": 1, "new": 1, "over_limit": 0, "dry_run": 1}
    [row] = runs()
    assert row.status == "dry_run" and row.mode == "dry_run" and row.action == "access.revoke_stale"
    assert row.before == {"revoked_at": None, "revoked_by": None}
    assert row.after["revoked_by"] == "system:autonomy:PB-STALE" and row.after["revoked_at"]
    assert row.evidence["reasons"] == ["expired"] and row.actor == "system:autonomy:PB-STALE"
    assert grant_row(gid).revoked_at is None  # 状態は変わらない
    assert audit_count() == audit_before  # 監査にも状態の変更は無い
    assert governance_state() == snapshot  # 監査を含め、自律統治の記録以外は一切変わらない
    run_now()
    assert len(runs()) == 1  # 同じ対象を繰り返し記録しない


def test_switching_to_active_acts_on_targets_seen_in_dry_run(client, autonomy_on):
    a = owned_asset(client)
    gid = grant(a["id"])
    use_playbooks(stale(mode="dry_run"))
    run_now()
    use_playbooks(stale(mode="active"))
    run_now()
    assert [r.status for r in runs()] == ["dry_run", "executed"] and grant_row(gid).revoked_at is not None


# ---------------------------------------------------------------- 区分 A の実行と取り消し
def test_execute_and_undo_restores_the_original_state(client, autonomy_on):
    a = owned_asset(client)
    gid = grant(a["id"])
    use_playbooks(stale())
    run_now()
    [row] = runs()
    assert row.status == "executed" and row.undo_until is not None
    revoked = grant_row(gid)
    assert revoked.revoked_by == "system:autonomy:PB-STALE"
    [event] = audits("autonomy.execute")
    assert event.actor == "system:autonomy:PB-STALE" and event.payload["before"] == {"revoked_at": None, "revoked_by": None}
    assert audits("access.revoke")[-1].actor == "system:autonomy:PB-STALE"
    # 取り消し (対象資産の Owner)
    r = client.post(f"{AUT}/runs/{row.id}/undo", headers=OWNER, json={"reason": "まだ利用中"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "undone" and r.json()["undone_by"] == "dev:asset-owner" and r.json()["undoable"] is False
    restored = grant_row(gid)
    assert restored.revoked_at is None and restored.revoked_by is None
    [undo] = audits("autonomy.undo")
    assert undo.actor == "dev:asset-owner" and undo.payload["reason"] == "まだ利用中" and undo.payload["restored"] == row.before
    assert client.post(f"{AUT}/runs/{row.id}/undo", headers=OWNER, json={"reason": "x"}).status_code == 409
    run_now()
    assert len(runs()) == 1  # 取り消された対象は、同じプレイブックで繰り返さない


def test_undo_after_the_period_is_409(client, autonomy_on):
    a = owned_asset(client)
    grant(a["id"])
    use_playbooks(stale(undo_days=1))
    run_now()
    [row] = runs()
    with SessionLocal() as db:
        db.execute(update(RemediationRun).values(undo_until=utcnow() - timedelta(seconds=1)))
        db.commit()
    r = client.post(f"{AUT}/runs/{row.id}/undo", headers=CHAIR, json={"reason": "遅い"})
    assert r.status_code == 409 and "期限" in r.json()["detail"]
    assert client.get(f"{AUT}/runs/{row.id}", headers=CHAIR).json()["undoable"] is False


def test_undo_permissions(client, autonomy_on):
    a = owned_asset(client)
    grant(a["id"])
    grant(a["id"], "bob")
    use_playbooks(stale(fp=1.0))
    run_now()
    first, second = runs()
    for who in (U("someone", "owner", "*"), U("asset-steward"), U("asset-custodian"), ADMIN):
        assert client.post(f"{AUT}/runs/{first.id}/undo", headers=who, json={"reason": "x"}).status_code == 403
    assert client.post(f"{AUT}/runs/{first.id}/undo", headers=CHAIR, json={"reason": "Committee"}).status_code == 200
    assert client.post(f"{AUT}/runs/{second.id}/undo", headers=OWNER, json={"reason": ""}).status_code == 422
    assert client.post(f"{AUT}/runs/nope/undo", headers=CHAIR, json={"reason": "x"}).status_code == 404


def test_undo_refuses_when_the_state_changed_afterwards(client, autonomy_on):
    a = owned_asset(client)
    gid = grant(a["id"])
    use_playbooks(stale())
    run_now()
    [row] = runs()
    with SessionLocal() as db:
        g = db.get(type(grant_row(gid)), gid)
        g.revoked_by = "asset-owner"  # 人が改めて取り消した
        db.commit()
    r = client.post(f"{AUT}/runs/{row.id}/undo", headers=OWNER, json={"reason": "x"})
    assert r.status_code == 409 and "変わっています" in r.json()["detail"]


def _quality_issue(client, asset_id) -> str:
    qa = U("qa", "admin,owner,custodian", "*")
    r = client.post(f"{API}/quality/assets/{asset_id}/rules", headers=qa, json={
        "name": "not-null", "dimension": "completeness", "rule_type": "not_null", "column": "id", "threshold": 1.0})
    assert r.status_code == 201, r.text
    r = client.post(f"{API}/quality/assets/{asset_id}/runs", headers=qa, json={"records": [{"id": None}]})
    [issue_id] = r.json()["issues_opened"]
    return issue_id


def _pass(client, asset_id, n):
    qa = U("qa", "admin,owner,custodian", "*")
    for _ in range(n):
        assert client.post(f"{API}/quality/assets/{asset_id}/runs", headers=qa, json={"records": [{"id": 1}]}).status_code == 201


def _issue(issue_id) -> Issue:
    with SessionLocal() as db:
        return db.get(Issue, issue_id)


def test_close_recovered_issue_and_undo(client, autonomy_on):
    a = owned_asset(client)
    issue_id = _quality_issue(client, a["id"])
    use_playbooks(recovered())
    _pass(client, a["id"], 2)
    run_now()
    assert runs() == [] and _issue(issue_id).status == "open"  # まだ 2 回
    _pass(client, a["id"], 1)
    run_now()
    [row] = runs()
    assert row.status == "executed" and len(row.evidence["runs"]) == 3 and all(r["pass_rate"] == 1.0 for r in row.evidence["runs"])
    assert _issue(issue_id).status == "resolved" and _issue(issue_id).resolved_at is not None
    assert client.post(f"{AUT}/runs/{row.id}/undo", headers=OWNER, json={"reason": "まだ不安定"}).status_code == 200
    assert _issue(issue_id).status == "open" and _issue(issue_id).resolved_at is None


def test_recovered_requires_consecutive_passes(client, autonomy_on):
    a = owned_asset(client)
    issue_id = _quality_issue(client, a["id"])
    use_playbooks(recovered())
    _pass(client, a["id"], 2)
    qa = U("qa", "admin,owner,custodian", "*")
    client.post(f"{API}/quality/assets/{a['id']}/runs", headers=qa, json={"records": [{"id": None}]})  # 途中で不合格
    _pass(client, a["id"], 2)
    run_now()
    assert runs() == [] and _issue(issue_id).status == "open"


# ---------------------------------------------------------------- 安全装置
def test_limit_per_run_and_issue_for_the_rest(client, autonomy_on):
    a = owned_asset(client)
    ids = [grant(a["id"], f"user{i}") for i in range(5)]
    use_playbooks(stale(limit=2, fp=1.0))
    result = run_now()
    assert result["playbooks"]["PB-STALE"]["executed"] == 2 and result["playbooks"]["PB-STALE"]["over_limit"] == 3
    assert sum(1 for g in ids if grant_row(g).revoked_at is not None) == 2
    with SessionLocal() as db:
        [issue] = db.scalars(select(Issue).where(Issue.source_ref == "autonomy-limit:PB-STALE")).all()
    assert "3 件" in issue.title and issue.assignee == "data-governance-committee" and issue.escalation_level == "committee"
    run_now()
    assert sum(1 for g in ids if grant_row(g).revoked_at is not None) == 4  # 残りは次回以降


def test_stop_is_immediate_and_resume_needs_committee(client, autonomy_on):
    a = owned_asset(client)
    gid = grant(a["id"])
    use_playbooks(stale())
    for who in (U("someone", "owner", "*"), U("x", "consumer")):
        assert client.post(f"{AUT}/stop", headers=who, json={"reason": "x"}).status_code == 403
    r = client.post(f"{AUT}/stop", headers=ADMIN, json={"reason": "調査のため"})
    assert r.status_code == 200 and r.json()["stopped"] is True
    assert client.post(f"{AUT}/stop", headers=CHAIR, json={"reason": "重ねて"}).status_code == 200  # 停止中も 200
    assert run_now() == {"status": "stopped"} and runs() == [] and grant_row(gid).revoked_at is None
    assert [e.actor for e in audits("autonomy.stop")] == ["dev:root"]
    assert client.post(f"{AUT}/resume", headers=ADMIN, json={"reason": "x"}).status_code == 403  # 再開は Committee だけ
    r = client.post(f"{AUT}/resume", headers=CHAIR, json={"reason": "原因を取り除いた"})
    assert r.status_code == 200 and r.json()["stopped"] is False
    assert client.post(f"{AUT}/resume", headers=CHAIR, json={"reason": "x"}).status_code == 409
    run_now()
    assert grant_row(gid).revoked_at is not None


def test_resume_is_an_approval_action_with_step_up():
    assert authz.PERMISSIONS["autonomy.resume"].approval is True
    assert authz.PERMISSIONS["autonomy.stop"].approval is False  # 安全側の操作は ✓ 不要
    old = utcnow() - timedelta(hours=2)
    ui = Principal("chair", frozenset({"committee"}), "restricted", frozenset(), channel="ui", auth_time=old, write_request=True)
    with SessionLocal() as db:
        with pytest.raises(ProblemError) as e:
            authz.require(db, ui, "autonomy.resume", entity_type="autonomy", entity_id="control")
        assert e.value.status_code == 401 and e.value.type_ == "step-up-required"
        authz.require(db, ui, "autonomy.stop", entity_type="autonomy", entity_id="control")  # 停止はステップアップ不要


def test_stop_between_operations(client, autonomy_on, monkeypatch):
    a = owned_asset(client)
    ids = [grant(a["id"], f"user{i}") for i in range(3)]
    use_playbooks(stale(fp=1.0))
    calls = iter([False, False, True, True, True])
    monkeypatch.setattr(autonomy, "is_stopped", lambda db: next(calls))
    result = run_now()
    assert result["playbooks"]["PB-STALE"]["executed"] == 1 and result["playbooks"]["PB-STALE"]["stopped"] == 1
    assert sum(1 for g in ids if grant_row(g).revoked_at is not None) == 1


def test_false_positive_rate_suspends_the_playbook(client, autonomy_on):
    a = owned_asset(client)
    grant(a["id"], "u1")
    grant(a["id"], "u2")
    use_playbooks(stale(fp=0.4))
    run_now()
    first, _ = runs()
    assert client.post(f"{AUT}/runs/{first.id}/undo", headers=OWNER, json={"reason": "誤り"}).status_code == 200  # 1 / 2 = 50% > 40%
    status = client.get(f"{AUT}/status", headers=CHAIR).json()
    [pb] = status["playbooks"]
    assert pb["suspended"] is True and "50.00%" in pb["suspended_reason"] and pb["false_positive"]["rate"] == 0.5
    with SessionLocal() as db:
        [issue] = db.scalars(select(Issue).where(Issue.source_ref == "autonomy-suspended:PB-STALE")).all()
    assert issue.severity == "high" and issue.assignee == "data-governance-committee"
    assert audits("autonomy.playbook_suspend")[-1].actor == "system:autonomy:PB-STALE"
    third = grant(a["id"], "u3")
    assert run_now()["playbooks"]["PB-STALE"] == {"skipped": "suspended"}
    assert grant_row(third).revoked_at is None
    # 再開は Committee の承認行為。再開の後の実行から数え直す
    assert client.post(f"{AUT}/resume", headers=ADMIN, json={"reason": "x", "playbook": "PB-STALE"}).status_code == 403
    assert client.post(f"{AUT}/resume", headers=CHAIR, json={"reason": "x", "playbook": "PB-NONE"}).status_code == 409
    r = client.post(f"{AUT}/resume", headers=CHAIR, json={"reason": "閾値を見直した", "playbook": "PB-STALE"})
    assert r.status_code == 200 and r.json()["playbooks"][0]["suspended"] is False
    assert r.json()["playbooks"][0]["false_positive"]["executed"] == 0
    run_now()
    assert grant_row(third).revoked_at is not None


def test_failed_operation_is_recorded_and_others_continue(client, autonomy_on, monkeypatch):
    a = owned_asset(client)
    ids = [grant(a["id"], f"user{i}") for i in range(2)]
    use_playbooks(stale(fp=1.0))
    original = autonomy.EXECUTORS["access.revoke_stale"]
    seen = []

    def flaky(db, actor, c, pb, now):
        seen.append(c.target_id)
        if len(seen) == 1:
            raise RuntimeError("boom")
        return original.apply(db, actor, c, pb, now)

    monkeypatch.setitem(autonomy.EXECUTORS, "access.revoke_stale", autonomy.Executor(original.snapshot, original.preview, flaky, original.undo))
    run_now()
    statuses = sorted(r.status for r in runs())
    assert statuses == ["executed", "failed"]
    failed = next(r for r in runs() if r.status == "failed")
    assert failed.result["error"] == "RuntimeError" and grant_row(failed.target_id).revoked_at is None
    assert sum(1 for g in ids if grant_row(g).revoked_at is not None) == 1


def test_asset_gone_grants_are_revoked(client, autonomy_on):
    a = owned_asset(client)
    gid = grant(a["id"], expired=False)
    use_playbooks(stale(condition={"expired": True, "asset_gone": True}))
    run_now()
    assert runs() == []
    with SessionLocal() as db:
        db.execute(update(DataAsset).values(lifecycle_state="deleted"))
        db.commit()
    run_now()
    [row] = runs()
    assert row.evidence["reasons"] == ["asset_gone"] and grant_row(gid).revoked_at is not None


# ---------------------------------------------------------------- 区分 B
def test_owner_assignment_is_only_a_proposal(client, autonomy_on):
    domain(client)
    a = owned_asset(client, owner=None, domain="sales")
    use_playbooks(owners())
    run_now()
    [row] = runs()
    assert row.status == "proposed" and row.category == "B" and row.after["proposal"] == {"owner": "dom-owner"}
    with SessionLocal() as db:
        assert db.get(DataAsset, a["id"]).owner is None  # 承認されるまで状態は変わらない
        req = db.get(ApprovalRequest, row.approval_id)
        assert req.type == "owner_assignment" and req.requested_by == "system:autonomy:PB-OWNER" and req.status == "pending"
        # 自動化の主体は承認できない (権限表の外からの経路も塞ぐ)
        with pytest.raises(ProblemError) as e:
            approvals.decide(db, autonomy_catalog.principal_for("PB-OWNER"), req, approve=True, comment="")
        assert e.value.status_code == 403
    r = client.post(f"{API}/approvals/{row.approval_id}/decision", headers=U("system:autonomy:PB-X", "committee"),
                    json={"approve": True, "comment": ""})
    assert r.status_code == 403 and "system:autonomy" in r.json()["detail"]
    assert client.get(f"{AUT}/runs/{row.id}", headers=CHAIR).json()["approval_status"] == "pending"
    assert client.post(f"{API}/approvals/{row.approval_id}/decision", headers=CHAIR, json={"approve": True, "comment": "OK"}).status_code == 200
    with SessionLocal() as db:
        assert db.get(DataAsset, a["id"]).owner == "dom-owner"
    assert audits("asset.ownership_change")[-1].payload["after"] == {"owner": "dom-owner"}
    assert client.post(f"{AUT}/runs/{row.id}/undo", headers=CHAIR, json={"reason": "x"}).status_code == 409  # 区分 B は取り消しの対象外


def test_owner_assignment_conflict_when_owner_was_set_meanwhile(client, autonomy_on):
    domain(client)
    a = owned_asset(client, owner=None, domain="sales")
    use_playbooks(owners())
    run_now()
    [row] = runs()
    with SessionLocal() as db:
        db.get(DataAsset, a["id"]).owner = "someone"
        db.commit()
    r = client.post(f"{API}/approvals/{row.approval_id}/decision", headers=CHAIR, json={"approve": True, "comment": ""})
    assert r.status_code == 409


def test_autonomy_principal_is_not_offered_approvals(client, autonomy_on):
    domain(client)
    owned_asset(client, owner=None, domain="sales")
    use_playbooks(owners())
    run_now()
    with SessionLocal() as db:
        assert approvals.decidable_pending(db, autonomy_catalog.principal_for("PB-OWNER")) == []


# ---------------------------------------------------------------- 参照 (ABAC)
def test_runs_are_scoped_by_domain_and_read_permission(client, autonomy_on):
    domain(client)
    domain(client, "hr", owner="hr-owner")
    sales = owned_asset(client, "sales.orders", domain="sales")
    hr = owned_asset(client, "hr.people", domain="hr", sensitivity="restricted", owner="hr-asset-owner")
    grant(sales["id"])
    grant(hr["id"])
    use_playbooks(stale(mode="dry_run"))
    run_now()
    assert len(client.get(f"{AUT}/runs", headers=CHAIR).json()) == 2
    sales_steward = U("s-steward", "steward", "sales")
    assert [r["asset_id"] for r in client.get(f"{AUT}/runs", headers=sales_steward).json()] == [sales["id"]]
    hr_steward = U("h-steward", "steward", "hr")
    assert client.get(f"{AUT}/runs", headers=hr_steward).json() == []  # restricted の資産は読めない
    hr_run = next(r for r in runs() if r.asset_id == hr["id"])
    assert client.get(f"{AUT}/runs/{hr_run.id}", headers=sales_steward).status_code == 404
    assert client.get(f"{AUT}/runs/{hr_run.id}", headers=CHAIR).status_code == 200
    assert client.get(f"{AUT}/runs", headers=U("x", "consumer")).status_code == 403
    assert client.get(f"{AUT}/runs", headers=CHAIR, params={"status": "executed"}).json() == []
    assert client.get(f"{AUT}/runs/nope", headers=CHAIR).status_code == 404


def test_status_and_catalog(client, autonomy_on):
    use_playbooks(stale(mode="dry_run"), owners(mode="dry_run"))
    status = client.get(f"{AUT}/status", headers=CHAIR).json()
    assert status["enabled"] is True and status["stopped"] is False
    assert [p["id"] for p in status["playbooks"]] == ["PB-STALE", "PB-OWNER"]
    catalog = client.get(f"{AUT}/catalog", headers=U("s", "steward", "sales")).json()
    assert {o["id"]: o["category"] for o in catalog["operations"]}["access.revoke_stale"] == "A"
    assert "asset.delete" in {f["id"] for f in catalog["forbidden"]}
    assert client.get(f"{AUT}/status", headers=U("x", "consumer")).status_code == 403


def test_job_runs_as_the_autonomy_principal(client, autonomy_on):
    a = owned_asset(client)
    grant(a["id"])
    use_playbooks(stale())
    result = run_job("autonomy_run")
    assert result["status"] == "succeeded" and result["result"]["playbooks"]["PB-STALE"]["executed"] == 1
    assert audits("autonomy.execute")[-1].actor == "system:autonomy:PB-STALE"


def test_unimplemented_kinds_are_skipped(client, autonomy_on, monkeypatch):
    monkeypatch.delitem(autonomy.DETECTORS, "stale_grant")
    use_playbooks(stale())
    assert run_now()["playbooks"]["PB-STALE"] == {"skipped": "not_implemented"}
