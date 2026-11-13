"""Phase 18d: 全社の成熟度の API と、定期の評価のジョブ."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update

from edgf.config import settings
from edgf.db import SessionLocal
from edgf.jobs import run_job
from edgf.models import Issue, MaturityAssessment, utcnow
from edgf.services import maturity
from tests.maturity_helpers import CHAIR, LEVEL2, MAT, create, decided, domain, snapshots
from tests.process_helpers import API, U


def assessments(name=None):
    with SessionLocal() as db:
        stmt = select(MaturityAssessment)
        if name:
            stmt = stmt.where(MaturityAssessment.domain == name)
        return db.scalars(stmt).all()


# ---------------------------------------------------------------- 全社の成熟度
def test_enterprise_distribution_minimum_and_unevaluated(client):
    for name in ("sales", "hr", "ops"):
        domain(client, name)
    decided("sales", 2)
    decided("hr", 1)
    body = client.get(f"{MAT}/enterprise", headers=U("anyone")).json()
    assert body["domain_count"] == 3 and body["unevaluated_count"] == 1 and body["minimum_level"] == 0
    assert body["distribution"] == {"0": 1, "1": 1, "2": 1, "3": 0, "4": 0, "5": 0}
    assert {d["domain"]: (d["level"], d["evaluated"]) for d in body["domains"]} == {"sales": (2, True), "hr": (1, True), "ops": (0, False)}
    decided("ops", 1)
    body = client.get(f"{MAT}/enterprise", headers=U("anyone")).json()
    assert body["minimum_level"] == 1 and body["minimum_level_name"] == "Basic Policy" and body["unevaluated_count"] == 0


def test_enterprise_uses_the_latest_decision(client):
    domain(client)
    first = decided("sales", 3)
    with SessionLocal() as db:
        db.execute(update(MaturityAssessment).where(MaturityAssessment.id == first).values(decided_at=utcnow() - timedelta(days=500)))
        db.commit()
    decided("sales", 1)  # 下げる判定が後
    assert client.get(f"{MAT}/enterprise", headers=U("anyone")).json()["domains"][0]["level"] == 1


def test_enterprise_without_domains(client):
    body = client.get(f"{MAT}/enterprise", headers=U("anyone")).json()
    assert body["domain_count"] == 0 and body["minimum_level"] == 0 and sum(body["distribution"].values()) == 0


def test_kpi_indicator_is_kept_for_compatibility(client):
    body = client.get(f"{API}/kpi", headers=CHAIR).json()
    assert "maturity" in body and "indicative_level" in body["maturity"]


def test_domain_view_shows_gaps_and_references(client):
    domain(client)
    domain(client, "hr", owner="hr-owner")
    decided("hr", 2)
    assert client.post(f"{MAT}/references", headers=CHAIR, json={"domain": "hr", "reason": "先行"}).status_code == 201
    decided("sales", 1)
    snapshots("sales", LEVEL2)
    a = create(client)
    client.post(f"{MAT}/assessments/{a['id']}/evaluate", headers=CHAIR)
    body = client.get(f"{MAT}/domains/sales", headers=U("anyone")).json()
    assert body["level"] == 1 and body["target_level"] == 2 and body["indicative_level"] == 2
    assert [r["domain"] for r in body["references"]] == ["hr"]  # Level 2 を目指すので、Level 2 以上の参照ドメイン
    assert {g["id"] for g in body["next_level_gaps"]} >= {"L2-CDE", "L2-OPERATION"}
    assert [h["decided_level"] for h in body["history"]] == [None, 1]
    assert client.get(f"{MAT}/domains/nope", headers=U("anyone")).status_code == 404
    # 目指すレベルに届いていない参照ドメインは示さない (指定し直すと、その時点の判定のレベルになる)
    decided("hr", 1)
    assert client.post(f"{MAT}/references", headers=CHAIR, json={"domain": "hr", "reason": "再指定"}).json()["level"] == 1
    assert client.get(f"{MAT}/domains/sales", headers=U("anyone")).json()["references"] == []


# ---------------------------------------------------------------- 定期の評価
def test_cycle_job_creates_once_per_cycle(client):
    for name in ("sales", "hr"):
        domain(client, name)
    snapshots("sales", LEVEL2)
    first = run_job("maturity_cycle")
    assert first["status"] == "succeeded" and first["result"]["created"] == ["hr", "sales"]
    rows = {r.domain: r for r in assessments()}
    assert rows["sales"].status == "auto_evaluated" and rows["sales"].indicative_level == 2 and rows["sales"].created_by == "system:jobs"
    second = run_job("maturity_cycle", force=True)
    assert second["result"]["created"] == [] and len(assessments()) == 2  # 冪等


def test_cycle_job_respects_cancellation_and_the_next_cycle(client):
    domain(client)
    run_job("maturity_cycle")
    [row] = assessments()
    assert client.post(f"{MAT}/assessments/{row.id}/cancel", headers=CHAIR, json={"reason": "再編"}).status_code == 200
    run_job("maturity_cycle", force=True)
    assert len(assessments()) == 1  # 周期の中では取り消しても起票し直さない
    with SessionLocal() as db:
        later = utcnow() + timedelta(days=settings.maturity_cycle_days + 1)
        assert maturity.run_cycle(db, "system:jobs", now=later)["created"] == ["sales"]
        db.commit()
    assert len(assessments()) == 2


def test_overdue_assessment_opens_one_issue(client):
    domain(client)
    run_job("maturity_cycle")
    with SessionLocal() as db:
        db.execute(update(MaturityAssessment).values(due_at=utcnow() - timedelta(days=1)))
        db.commit()
    assert run_job("maturity_cycle", force=True)["result"]["overdue_issues_opened"] == 1
    assert run_job("maturity_cycle", force=True)["result"]["overdue_issues_opened"] == 0
    with SessionLocal() as db:
        [issue] = db.scalars(select(Issue).where(Issue.source_ref.like("maturity:%"))).all()
    assert issue.kind == "maturity" and issue.assignee == "dom-owner" and issue.escalation_level == "owner"
