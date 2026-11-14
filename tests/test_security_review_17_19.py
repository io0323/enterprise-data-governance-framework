"""Phase 17〜19 のセキュリティレビューの指摘の回帰テスト (fix/security-review-17-19)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from edgf.auth import Principal
from edgf.config import settings
from edgf.db import SessionLocal, engine
from edgf.errors import ProblemError
from edgf.models import ErasureRequest, Issue, RemediationRun, utcnow
from edgf.services import autonomy
from tests.autonomy_helpers import AUT, CHAIR, grant, grant_row, run_now, runs, stale, use_playbooks
from tests.process_helpers import API, U, asset
from tests.ui_helpers import as_ui

STALE = settings.step_up_minutes + 5
PRIV = f"{API}/privacy"


# ---------------------------------------------------------------- M1: 消去の承認と完了確認のステップアップ
def _erasure(client) -> str:
    client.post(f"{API}/assets", headers=U("root", "admin"), json={
        "qualified_name": "crm.customers", "name": "c", "data_class": "master", "sensitivity": "confidential", "owner": "o1",
        "steward": "st", "custodian": "c1", "schema": [{"name": "email", "pii": True, "identifier_type": "email"}]})
    r = client.post(f"{PRIV}/erasure-requests", headers=U("root", "admin"), json={"identifier_type": "email", "identifier": "p@example.com"})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert client.post(f"{PRIV}/erasure-requests/{rid}/scope", headers=CHAIR).status_code == 200
    return rid


def test_erasure_approve_requires_step_up_on_ui_path(client):
    rid = _erasure(client)
    for who in (("chair", "committee"), ("o1", "")):  # 全社ロールと、対象資産の Owner (is_allowed で許可される経路) の両方
        r = as_ui(client.app, who[0], who[1], auth_age_minutes=STALE).post(f"{PRIV}/erasure-requests/{rid}/approve")
        assert r.status_code == 401 and r.json()["type"].endswith("step-up-required") and r.json()["action"] == "privacy.erasure.approve"
    assert as_ui(client.app, "chair", "committee").post(f"{PRIV}/erasure-requests/{rid}/approve").status_code == 200
    # Bearer の経路には適用しない
    assert client.post(f"{PRIV}/erasure-requests/{_erasure_again(client)}/approve", headers=U("vice", "committee")).status_code == 200


def _erasure_again(client) -> str:
    r = client.post(f"{PRIV}/erasure-requests", headers=U("root", "admin"), json={"identifier_type": "email", "identifier": "q@example.com"})
    rid = r.json()["id"]
    assert client.post(f"{PRIV}/erasure-requests/{rid}/scope", headers=CHAIR).status_code == 200
    return rid


def test_erasure_verify_helper_requires_step_up(client):
    from edgf.api.privacy import _require_on_any_target
    rid = _erasure(client)
    old = Principal("chair", frozenset({"committee"}), "restricted", frozenset(), channel="ui", auth_time=utcnow() - timedelta(minutes=STALE),
                    write_request=True)
    with SessionLocal() as db:
        req = db.get(ErasureRequest, rid)
        with pytest.raises(ProblemError) as e:
            _require_on_any_target(db, old, req, "privacy.erasure.verify")
        assert e.value.status_code == 401 and e.value.type_ == "step-up-required"
        _require_on_any_target(db, old, req, "privacy.erasure.read")  # 読み取りは止めない


# ---------------------------------------------------------------- M2: 停止スイッチは実行中のジョブにも効く
@pytest.mark.skipif(engine.dialect.name == "sqlite", reason="インメモリの SQLite は 1 接続を共有するため並行実行できない")
def test_stop_during_a_running_job_takes_effect_before_the_next_operation(client, autonomy_on, monkeypatch):
    """操作ごとに確定するので、別の要求の停止 (監査を書く) がジョブの終わりを待たずに確定する (PostgreSQL では advisory lock)。"""
    a = asset(client, "sales.orders")
    ids = [grant(a["id"], f"user{i}") for i in range(3)]
    use_playbooks(stale(fp=1.0))
    original = autonomy._execute
    calls = []

    def execute(db, pb, digest, candidate, batch, now):
        calls.append(candidate.target_id)
        if len(calls) == 2:
            with SessionLocal() as other:  # 別の要求: 1 件目は確定済みで見え、停止もすぐに確定する
                assert other.scalars(select(RemediationRun).where(RemediationRun.status == "executed")).all() != []
                autonomy.stop(other, "dev:chair", "実行中に停止")
                other.commit()
        return original(db, pb, digest, candidate, batch, now)

    monkeypatch.setattr(autonomy, "_execute", execute)
    result = run_now()
    assert result["playbooks"]["PB-STALE"]["executed"] == 2 and result["playbooks"]["PB-STALE"]["stopped"] == 1
    assert sum(1 for g in ids if grant_row(g).revoked_at is not None) == 2


# ---------------------------------------------------------------- M3: dry_run は Issue を起票しない
def test_dry_run_over_limit_opens_no_issue(client, autonomy_on):
    a = asset(client, "sales.orders")
    for i in range(3):
        grant(a["id"], f"user{i}")
    use_playbooks(stale(mode="dry_run", limit=1))
    result = run_now()
    assert result["playbooks"]["PB-STALE"]["over_limit"] == 2
    with SessionLocal() as db:
        assert db.scalars(select(Issue)).all() == []


# ---------------------------------------------------------------- M4: 信頼を上げる取り消しはステップアップ
def test_trust_raising_undo_requires_step_up_on_ui_path(client, autonomy_on):
    a = asset(client, "sales.orders")
    gid = grant(a["id"])
    use_playbooks(stale())
    run_now()
    [row] = runs()
    view = client.get(f"{AUT}/runs/{row.id}", headers=CHAIR).json()
    assert view["undo_step_up"] is True
    r = as_ui(client.app, "asset-owner", "", auth_age_minutes=STALE).post(f"{AUT}/runs/{row.id}/undo", json={"reason": "利用中"})
    assert r.status_code == 401 and r.json()["type"].endswith("step-up-required")
    assert grant_row(gid).revoked_at is not None
    assert as_ui(client.app, "asset-owner", "").post(f"{AUT}/runs/{row.id}/undo", json={"reason": "利用中"}).status_code == 200
    assert grant_row(gid).revoked_at is None


def test_catalog_marks_trust_raising_undo():
    from edgf.services.autonomy_catalog import CATALOG
    assert {o.id for o in CATALOG.values() if o.undo_raises_trust} == {"access.revoke_stale", "certification.revert_to_draft"}
    assert not CATALOG["issue.close_recovered"].undo_raises_trust
