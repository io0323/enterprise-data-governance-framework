"""Phase 12e (§16 / §19): ジョブ基盤、定期実行、保持期限の自動執行."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from edgf import jobs
from edgf.db import SessionLocal, engine
from edgf.jobs import Job, JOBS, acquire_lease, release_lease, run_job
from edgf.models import utcnow
from edgf.services import retention
from tests.process_helpers import ADMIN, API, APPROVALS, COMMITTEE, OWNER, U, asset, audit_actions, decide, inbox, issues_of
from tests.test_outbox import FakeTransport

OPS = f"{API}/ops/jobs"
ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- Consent の失効
@pytest.fixture()
def purpose_30(client):
    r = client.post(f"{API}/privacy/purposes", headers=COMMITTEE, json={"id": "ads", "description": "d", "default_legal_basis": "consent", "retention_days": 30})
    assert r.status_code == 201
    return client


RAW = "Retention.Person@Example.com"


def _consent(client, status="granted", identifier=RAW):
    r = client.post(f"{API}/privacy/consents", headers=COMMITTEE, json={"identifier_type": "email", "identifier": identifier, "purpose_id": "ads", "status": status})
    assert r.status_code == 201, r.text
    return r.json()


def _check(client):
    return client.post(f"{API}/privacy/consents/check", headers=COMMITTEE, json={"identifier_type": "email", "identifier": RAW, "purpose_id": "ads"}).json()


def test_consent_expires_after_retention_days(purpose_30):
    client = purpose_30
    row = _consent(client)
    assert run_job("consent_expire")["result"]["expired"] == 0
    assert _check(client)["allowed"] is True
    with engine.begin() as conn:
        conn.execute(text("UPDATE consents SET recorded_at = :t"), {"t": utcnow() - timedelta(days=31)})
    outcome = run_job("consent_expire")
    assert outcome["status"] == "succeeded" and outcome["result"]["expired"] == 1 and outcome["count"] == 2
    got = _check(client)
    assert got["allowed"] is False and got["status"] == "expired"
    # 追記のみ: 元の行は残り、expired の行が増える
    history = client.get(f"{API}/privacy/consents", headers=COMMITTEE, params={"subject_ref": row["subject_ref"]}).json()
    assert [h["status"] for h in history] == ["granted", "expired"] and history[1]["recorded_by"] == "system:jobs"
    # 冪等
    assert run_job("consent_expire")["result"]["expired"] == 0
    assert len(client.get(f"{API}/privacy/consents", headers=COMMITTEE, params={"subject_ref": row["subject_ref"]}).json()) == 2
    # 監査には件数だけ
    events = [e for e in audit_actions(client) if e["action"] == "consent.expire"]
    assert events[0]["payload"] == {"purpose_id": "ads", "count": 1}
    assert row["subject_ref"] not in json.dumps(audit_actions(client))
    # 取り直せば有効に戻る
    _consent(client)
    assert _check(client)["allowed"] is True


def test_withdrawn_consent_and_no_retention_are_left_alone(purpose_30):
    client = purpose_30
    _consent(client)
    _consent(client, "withdrawn")
    assert client.post(f"{API}/privacy/purposes", headers=COMMITTEE, json={"id": "forever", "description": "d", "default_legal_basis": "consent"}).status_code == 201
    assert client.post(f"{API}/privacy/consents", headers=COMMITTEE, json={"identifier_type": "email", "identifier": "b@example.com", "purpose_id": "forever", "status": "granted"}).status_code == 201
    with engine.begin() as conn:
        conn.execute(text("UPDATE consents SET recorded_at = :t"), {"t": utcnow() - timedelta(days=400)})
    assert run_job("consent_expire")["result"]["expired"] == 0


# ---------------------------------------------------------------- 保持期限
def _age(asset_id, days=400):
    with engine.begin() as conn:
        conn.execute(text("UPDATE data_assets SET created_at = :t WHERE id = :i"), {"t": utcnow() - timedelta(days=days), "i": asset_id})


def _retention_requests(client, status="pending"):
    return client.get(APPROVALS, headers=COMMITTEE, params={"type": "retention_delete", "status": status}).json()


def test_retention_creates_request_and_delete_needs_approval(client):
    a = asset(client, retention_days=30)
    fresh = asset(client, "sales.fresh", retention_days=30)
    asset(client, "sales.keep_forever")
    assert run_job("retention_detect")["result"]["requests_created"] == []
    _age(a["id"])
    first = run_job("retention_detect")
    assert len(first["result"]["requests_created"]) == 1
    req = _retention_requests(client)[0]
    assert req["asset_id"] == a["id"] and req["requested_by"] == "system:jobs" and req["steps"][0]["rule"] == "asset_owner"
    assert client.get(f"{API}/assets/{fresh['id']}", headers=ADMIN).json()["lifecycle_state"] == "draft"
    # 申請だけでは削除しない。冪等
    assert client.get(f"{API}/assets/{a['id']}", headers=ADMIN).json()["lifecycle_state"] != "deleted"
    assert run_job("retention_detect")["result"]["requests_created"] == [] and len(_retention_requests(client)) == 1
    # 承認者の inbox に出る
    assert req["id"] in {i["id"] for i in inbox(client, OWNER)}
    decide(client, req["id"], COMMITTEE, code=403)
    assert decide(client, req["id"], OWNER)["status"] == "approved"
    assert client.get(f"{API}/assets/{a['id']}", headers=ADMIN).json()["lifecycle_state"] == "deleted"
    assert any(e["action"] == "lifecycle.delete_requested" for e in audit_actions(client, entity_id=a["id"]))
    assert run_job("retention_detect")["result"]["requests_created"] == []  # 削除済みは対象外


def test_legal_hold_is_excluded(client):
    a = asset(client, retention_days=30)
    _age(a["id"])
    assert client.post(f"{API}/assets/{a['id']}/legal-hold", headers=COMMITTEE, json={"legal_hold": True, "reason": "訴訟"}).status_code == 200
    assert run_job("retention_detect")["result"]["requests_created"] == [] and _retention_requests(client) == []
    # 解除すれば申請を作る
    assert client.post(f"{API}/assets/{a['id']}/legal-hold", headers=COMMITTEE, json={"legal_hold": False, "reason": "終結"}).status_code == 200
    assert len(run_job("retention_detect")["result"]["requests_created"]) == 1


def test_legal_hold_after_request_cancels_it(client):
    a = asset(client, retention_days=30)
    _age(a["id"])
    created = run_job("retention_detect")["result"]["requests_created"]
    assert client.post(f"{API}/assets/{a['id']}/legal-hold", headers=COMMITTEE, json={"legal_hold": True, "reason": "訴訟"}).status_code == 200
    assert run_job("retention_detect")["result"]["requests_cancelled"] == created
    assert client.get(f"{APPROVALS}/{created[0]}", headers=COMMITTEE).json()["status"] == "cancelled"


def test_rejected_request_waits_for_cooldown(client):
    a = asset(client, retention_days=30)
    _age(a["id"])
    req_id = run_job("retention_detect")["result"]["requests_created"][0]
    decide(client, req_id, OWNER, approve=False, comment="監査対応で保持する")
    assert run_job("retention_detect")["result"]["requests_created"] == []  # すぐには作り直さない
    with SessionLocal() as db:
        later = retention.detect_due_assets(db, "system:jobs", utcnow() + timedelta(days=91))
        db.commit()
    assert len(later["requests_created"]) == 1


def test_unowned_asset_opens_issue_for_committee(client):
    a = asset(client, owner=None, retention_days=30)
    _age(a["id"])
    outcome = run_job("retention_detect")
    assert outcome["result"]["no_owner"] == [a["id"]] and outcome["result"]["requests_created"] == []
    assert issues_of(client, "retention:no-owner")[0]["escalation_level"] == "committee"
    assert run_job("retention_detect")["result"]["no_owner"] == []


# ---------------------------------------------------------------- リース
def test_lease_blocks_second_runner_and_expires(client):
    assert acquire_lease("issue_escalate", "a") is not None
    assert acquire_lease("issue_escalate", "b") is None
    outcome = run_job("issue_escalate")
    assert outcome["status"] == "skipped"
    # 他人のリースは解放できない
    release_lease("issue_escalate", "b")
    assert acquire_lease("issue_escalate", "b") is None
    release_lease("issue_escalate", "a")
    assert acquire_lease("issue_escalate", "b") is not None
    # 期限切れは引き継げる (異常終了からの回復)
    with engine.begin() as conn:
        conn.execute(text("UPDATE job_leases SET expires_at = :t"), {"t": datetime(2020, 1, 1)})
    assert acquire_lease("issue_escalate", "c") is not None
    assert run_job("issue_escalate")["status"] == "skipped"


@pytest.mark.skipif(engine.dialect.name == "sqlite", reason="インメモリの SQLite は 1 接続を共有するため並行実行できない")
def test_only_one_concurrent_run(client, monkeypatch):
    """PostgreSQL の並行実行: リースによって同時に 1 つしか実行されない。"""
    running, peak, calls = [0], [0], [0]
    lock = threading.Lock()

    def slow(db, actor):
        with lock:
            running[0] += 1
            calls[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.5)
        with lock:
            running[0] -= 1
        return {"n": 1}

    monkeypatch.setitem(JOBS, "race", Job("race", "d", slow, 60))
    barrier = threading.Barrier(8)

    def go(_):
        barrier.wait()
        return run_job("race")["status"]

    with ThreadPoolExecutor(8) as pool:
        statuses = list(pool.map(go, range(8)))
    assert statuses.count("succeeded") == 1 and statuses.count("skipped") == 7
    assert calls[0] == 1 and peak[0] == 1


# ---------------------------------------------------------------- 実行の記録
def test_every_job_runs_and_is_idempotent(client, monkeypatch):
    monkeypatch.setattr("edgf.services.webhooks.UrllibTransport", FakeTransport)
    a = asset(client, retention_days=30)
    _age(a["id"])
    assert {j for j in JOBS} == {"issue_escalate", "erasure_escalate", "mdm_review_escalate", "ai_remediation_escalate", "policy_evaluate",
                                 "retention_detect", "consent_expire", "outbox_deliver", "approval_sla", "review_campaigns", "kpi_snapshot", "audit_anchor",
                                 "reachability_probe", "unused_assets"}
    first = {name: run_job(name) for name in JOBS}
    assert all(r["status"] == "succeeded" for r in first.values()), first
    assert first["policy_evaluate"]["result"]["issues_opened"] >= 1
    before = {t: _count(t) for t in ("issues", "approval_requests", "review_campaigns", "review_items", "consents", "kpi_snapshots")}
    second = {name: run_job(name) for name in JOBS}
    assert all(r["status"] == "succeeded" for r in second.values())
    assert second["policy_evaluate"]["result"]["issues_opened"] == 0
    assert second["retention_detect"]["count"] == 0 and second["review_campaigns"]["result"]["campaigns_created"] == []
    assert before == {t: _count(t) for t in before}


def _count(table):
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def test_job_failure_is_recorded_without_detail(client, monkeypatch):
    def boom(db, actor):
        raise RuntimeError("secret-internal-detail")

    monkeypatch.setitem(JOBS, "issue_escalate", Job("issue_escalate", "d", boom, 60))
    outcome = run_job("issue_escalate")
    assert outcome["status"] == "failed" and outcome["error"] == "RuntimeError"
    run_job("issue_escalate")
    row = next(j for j in client.get(OPS, headers=ADMIN).json() if j["job"] == "issue_escalate")
    assert row["last_status"] == "failed" and row["last_error"] == "RuntimeError" and row["consecutive_failures"] == 2
    assert row["last_success_at"] is None
    with engine.connect() as conn:
        assert "secret-internal-detail" not in str(list(conn.execute(text("SELECT * FROM job_runs"))))
    # リースは解放される
    assert acquire_lease("issue_escalate", "next") is not None


def test_failed_job_rolls_back_its_changes(client, monkeypatch):
    from edgf.services import issues

    def partial(db, actor):
        issues.open_issue(db, actor, asset=None, kind="other", title="途中まで")
        raise RuntimeError("x")

    monkeypatch.setitem(JOBS, "issue_escalate", Job("issue_escalate", "d", partial, 60))
    assert run_job("issue_escalate")["status"] == "failed"
    assert client.get(f"{API}/issues", headers=ADMIN).json() == []


def test_disabled_and_interval_from_env(client, monkeypatch):
    monkeypatch.setenv("EDGF_JOB_ISSUE_ESCALATE_ENABLED", "false")
    monkeypatch.setenv("EDGF_JOB_ISSUE_ESCALATE_INTERVAL_SECONDS", "120")
    assert run_job("issue_escalate") == {"job": "issue_escalate", "status": "disabled"}
    assert JOBS["issue_escalate"].interval == 120 and JOBS["issue_escalate"].enabled is False
    assert run_job("issue_escalate", force=True)["status"] == "succeeded"
    row = next(j for j in client.get(OPS, headers=COMMITTEE).json() if j["job"] == "issue_escalate")
    assert row["enabled"] is False and row["interval_seconds"] == 120 and row["last_status"] == "succeeded" and row["last_count"] == 0


# ---------------------------------------------------------------- API
def test_ops_api_permissions(client):
    for who in (OWNER, U("x", "owner", "*")):
        assert client.get(OPS, headers=who).status_code == 403
        assert client.post(f"{OPS}/issue_escalate/run", headers=who).status_code == 403
    assert client.post(f"{OPS}/issue_escalate/run", headers=COMMITTEE).status_code == 403  # 手動実行は admin だけ
    assert client.post(f"{OPS}/none/run", headers=ADMIN).status_code == 404
    r = client.post(f"{OPS}/issue_escalate/run", headers=ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "succeeded"
    assert any(e["action"] == "jobs.run" for e in audit_actions(client))
    names = {j["job"]: j for j in client.get(OPS, headers=ADMIN).json()}
    assert set(names) == set(JOBS) and names["issue_escalate"]["last_success_at"] and names["issue_escalate"]["overdue"] is False


# ---------------------------------------------------------------- /ready の警告
def test_ready_warns_about_overdue_jobs_without_503(client):
    assert "jobs_overdue" not in client.get("/ready").json()["checks"]
    with engine.begin() as conn:  # 導入から時間が経っているのに一度も実行していない
        conn.execute(text("UPDATE policy_bundles SET created_at = :t"), {"t": utcnow() - timedelta(days=30)})
    r = client.get("/ready")
    assert r.status_code == 200 and "outbox_deliver" in r.json()["checks"]["jobs_overdue"]
    # 実行すれば警告から外れる
    run_job("outbox_deliver")
    assert "outbox_deliver" not in client.get("/ready").json()["checks"]["jobs_overdue"].split(", ")
    assert jobs.overdue_jobs(utcnow() + timedelta(days=365))


def test_overdue_ignores_disabled_jobs(client, monkeypatch):
    with engine.begin() as conn:
        conn.execute(text("UPDATE policy_bundles SET created_at = :t"), {"t": utcnow() - timedelta(days=30)})
    monkeypatch.setenv("EDGF_JOB_OUTBOX_DELIVER_ENABLED", "0")
    assert "outbox_deliver" not in jobs.overdue_jobs()


# ---------------------------------------------------------------- CLI と常駐ループ
def test_cli_in_process(client, capsys):
    assert jobs.main(["list"]) == 0
    assert "outbox_deliver" in capsys.readouterr().out
    assert jobs.main(["run", "unknown"]) == 2
    assert jobs.main(["run", "issue_escalate"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"


def test_cli_failed_job_returns_nonzero(client, monkeypatch):
    monkeypatch.setitem(JOBS, "issue_escalate", Job("issue_escalate", "d", lambda db, actor: 1 / 0, 60))
    assert jobs.main(["run", "issue_escalate"]) == 1


def test_cli_as_module_on_real_database(tmp_path):
    """外部の cron から呼ぶときと同じ形で動く。"""
    url = f"sqlite:///{tmp_path}/jobs.db"
    env = {**os.environ, "EDGF_DATABASE_URL": url, "EDGF_AUTH_MODE": "dev"}
    alembic = str(Path(sys.executable).parent / "alembic")
    assert subprocess.run([alembic, "upgrade", "head"], cwd=ROOT, env=env, capture_output=True).returncode == 0
    res = subprocess.run([sys.executable, "-m", "edgf.jobs", "run", "policy_evaluate"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout.strip().splitlines()[-1])["status"] == "succeeded"


def test_serve_runs_due_jobs_once_per_interval(client, monkeypatch):
    calls = []
    only = {"a": Job("a", "d", lambda db, actor: calls.append(1) or {}, 3600)}
    monkeypatch.setattr(jobs, "JOBS", only)
    jobs.serve(tick_seconds=0, max_ticks=3)
    assert len(calls) == 1  # 間隔が来るまで再実行しない
    assert jobs.due_jobs(utcnow() + timedelta(hours=2)) == [only["a"]]
    stop = threading.Event()
    stop.set()
    jobs.serve(stop)  # 停止の合図で抜ける
