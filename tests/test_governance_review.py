"""Phase 19e: 統治の自己改善 (見直しの Issue。Policy は変えない) と Level 5 の指標."""
from __future__ import annotations

from datetime import timedelta

import pytest
import yaml
from sqlalchemy import select, update

from edgf.db import SessionLocal
from edgf.models import ClassificationSuggestion, Issue, PolicyBundle, PolicyException, utcnow
from edgf.services import kpi, policy, policy_bundles
from tests.autonomy_helpers import AUT, CHAIR, grant, playbook, run_now, runs, stale, use_playbooks
from tests.process_helpers import ADMIN, API, U, asset

TEST_POLICIES = {"policies": [
    {"id": "TST-ALWAYS", "name": "常に合格", "severity": "low", "check": {"type": "required_fields", "fields": ["name"]}},
    {"id": "TST-NEVER", "name": "適用先なし", "severity": "low", "when": {"data_class": "reference"},
     "check": {"type": "required_fields", "fields": ["name"]}},
    {"id": "TST-EXC", "name": "例外が多い", "severity": "low", "check": {"type": "required_fields", "fields": ["name"]}},
    {"id": "TST-FAIL", "name": "長く不合格", "severity": "low", "check": {"type": "required_fields", "fields": ["retention_days"]}},
]}


def review_playbook(mode="active", **condition):
    cond = {"findings": ["always_pass", "never_applied", "exception_heavy", "long_failing"], "always_pass_days": 180,
            "exception_threshold": 5, "failing_days": 90, **condition}
    return playbook("PB-REVIEW", trigger={"detector": "policy_review_scan"}, action="propose.policy_review", category="B", mode=mode,
                    undo_days=None, fp=None, condition=cond)


def install_test_policies(age_days=400) -> dict:
    with SessionLocal() as db:
        bundle = policy_bundles.active_bundle(db) or policy_bundles.bootstrap(db)
        files = {**bundle.files, "test-review.yaml": yaml.safe_dump(TEST_POLICIES, allow_unicode=True)}
        bundle.files, bundle.content_hash = files, policy.content_hash(files)
        db.execute(update(PolicyBundle).values(created_at=utcnow() - timedelta(days=age_days)))
        db.commit()
        return dict(files)


def review_issues() -> dict[str, Issue]:
    with SessionLocal() as db:
        rows = db.scalars(select(Issue).where(Issue.source_ref.like("policy-review:TST-%"))).all()
    return {r.source_ref.removeprefix("policy-review:"): r for r in rows}


def _prepare(client):
    a = asset(client, "sales.orders")
    with SessionLocal() as db:
        now = utcnow()
        for i in range(5):
            db.add(PolicyException(policy_id="TST-EXC", asset_id=a["id"], reason="r", requested_by=f"u{i}", status="approved",
                                   expires_at=now + timedelta(days=30)))
        db.add(Issue(asset_id=a["id"], kind="policy", source_ref="policy:TST-FAIL", title="t", status="open", created_at=now - timedelta(days=100),
                     due_at=now))
        db.commit()
    return a


def test_each_finding_opens_an_issue_and_policies_do_not_change(client, autonomy_on):
    _prepare(client)
    install_test_policies()
    use_playbooks(review_playbook())
    with SessionLocal() as db:
        files = dict(policy_bundles.active_bundle(db).files)
        version = policy_bundles.active_bundle(db).version
    run_now()
    found = review_issues()
    assert {"TST-ALWAYS:always_pass", "TST-NEVER:never_applied", "TST-EXC:exception_heavy", "TST-FAIL:long_failing"} <= set(found)
    assert all(i.kind == "policy_review" and i.assignee == "data-governance-committee" and i.escalation_level == "committee"
               for i in found.values())
    assert "例外の申請が 5 件" in found["TST-EXC:exception_heavy"].title and "90 日以上" in found["TST-FAIL:long_failing"].title
    with SessionLocal() as db:
        active = policy_bundles.active_bundle(db)
        assert active.version == version and active.files == files  # Policy そのものは変えない
        assert db.scalars(select(PolicyBundle).where(PolicyBundle.status != "active")).all() == []
    statuses = {r.status for r in runs(playbook_id="PB-REVIEW")}
    assert statuses == {"proposed"} and all(r.category == "B" for r in runs(playbook_id="PB-REVIEW"))
    run_now()
    assert len(review_issues()) == len(found)  # 同じ所見は繰り返し起票しない


@pytest.mark.parametrize("finding", ["always_pass", "never_applied", "exception_heavy", "long_failing"])
def test_findings_can_be_selected(client, autonomy_on, finding):
    _prepare(client)
    install_test_policies()
    use_playbooks(review_playbook(findings=[finding]))
    run_now()
    assert {k.split(":")[1] for k in review_issues()} == {finding}


def test_young_bundle_does_not_report_always_pass_or_never_applied(client, autonomy_on):
    _prepare(client)
    install_test_policies(age_days=10)
    use_playbooks(review_playbook())
    run_now()
    assert {k.split(":")[1] for k in review_issues()} == {"exception_heavy", "long_failing"}


def test_recent_failure_is_not_always_pass(client, autonomy_on):
    a = _prepare(client)
    install_test_policies()
    with SessionLocal() as db:
        db.add(Issue(asset_id=a["id"], kind="policy", source_ref="policy:TST-ALWAYS", title="t", status="resolved",
                     created_at=utcnow() - timedelta(days=20), due_at=utcnow()))
        db.commit()
    use_playbooks(review_playbook(findings=["always_pass"]))
    run_now()
    assert "TST-ALWAYS:always_pass" not in review_issues()


def test_review_dry_run_opens_nothing(client, autonomy_on):
    _prepare(client)
    install_test_policies()
    use_playbooks(review_playbook(mode="dry_run"))
    run_now()
    assert review_issues() == {}
    previews = [r for r in runs(playbook_id="PB-REVIEW") if r.target_id == "TST-FAIL:long_failing"]
    assert previews[0].status == "dry_run" and previews[0].after["issue"]["title"].startswith("[POLICY-REVIEW] TST-FAIL")


# ---------------------------------------------------------------- Level 5 の指標
def _kpi(domain=None) -> dict:
    with SessionLocal() as db:
        return kpi.compute(db, domain)


def test_level5_metrics_are_none_before_autonomy_runs(client):
    asset(client, "sales.orders")
    k = _kpi()
    assert k["autonomy_remediation_rate"] is None or k["autonomy_remediation_rate"] == 0
    assert k["autonomy_false_positive_rate"] is None and k["anomaly_detected_count"] is None
    assert k["classification_suggestion_acceptance_rate"] is None


def test_level5_metrics_follow_the_autonomy_records(client, autonomy_on):
    client.post(f"{API}/domains", headers=ADMIN, json={"name": "sales", "owner": "o"})
    client.post(f"{API}/domains", headers=ADMIN, json={"name": "hr", "owner": "o"})
    a = asset(client, "sales.orders", domain="sales")
    asset(client, "hr.people", domain="hr")
    qa = U("qa", "admin,owner,custodian", "*")
    client.post(f"{API}/quality/assets/{a['id']}/rules", headers=qa, json={
        "name": "nn", "dimension": "completeness", "rule_type": "not_null", "column": "id"})
    client.post(f"{API}/quality/assets/{a['id']}/runs", headers=qa, json={"records": [{"id": None}]})  # Issue 1 件
    for _ in range(3):
        client.post(f"{API}/quality/assets/{a['id']}/runs", headers=qa, json={"records": [{"id": 1}]})
    grant(a["id"], "u1")
    grant(a["id"], "u2")
    use_playbooks(playbook("PB-RECOVERED", trigger={"detector": "quality_recovered"}, action="issue.close_recovered", fp=1.0),
                  stale(fp=1.0))
    run_now()
    undo = next(r for r in runs() if r.action == "access.revoke_stale")
    assert client.post(f"{AUT}/runs/{undo.id}/undo", headers=CHAIR, json={"reason": "誤り"}).status_code == 200
    with SessionLocal() as db:
        db.add(ClassificationSuggestion(asset_id=a["id"], column="c", proposal={}, current={}, evidence=[], confidence=0.9,
                                        evidence_hash="h1", status="accepted", decision_reason=""))
        db.add(ClassificationSuggestion(asset_id=a["id"], column="d", proposal={}, current={}, evidence=[], confidence=0.9,
                                        evidence_hash="h2", status="rejected", decision_reason=""))
        db.commit()
    sales = _kpi("sales")
    assert sales["autonomy_remediation_rate"] == 1.0  # 品質の Issue 1 件を自動で解消した
    assert sales["autonomy_false_positive_rate"] == pytest.approx(1 / 3, abs=1e-3)  # 区分 A の実行 3 件のうち 1 件を取り消し
    assert sales["anomaly_detected_count"] == 0  # 検知は動いている (履歴が足りず異常は無い)
    assert sales["classification_suggestion_acceptance_rate"] == 0.5
    hr = _kpi("hr")
    assert hr["autonomy_remediation_rate"] is None and hr["autonomy_false_positive_rate"] is None
    assert hr["classification_suggestion_acceptance_rate"] is None and hr["anomaly_detected_count"] == 0
    with SessionLocal() as db:
        kpi.snapshot(db)
        db.commit()
    r = client.get(f"{API}/kpi", headers=CHAIR).json()
    assert {"autonomy_remediation_rate", "autonomy_false_positive_rate", "anomaly_detected_count",
            "classification_suggestion_acceptance_rate"} <= set(r)


def test_dry_run_is_not_counted_as_remediation(client, autonomy_on):
    a = asset(client, "sales.orders")
    grant(a["id"])
    use_playbooks(stale(mode="dry_run"))
    run_now()
    assert _kpi()["autonomy_false_positive_rate"] is None


def test_level5_criteria_update_takes_effect_after_approval(client):
    """既存の環境 (Phase 18 の基準) では、Phase 19 の Level 5 の基準は Policy 変更の承認と公開を経て有効になる。"""
    from pathlib import Path

    from edgf.config import settings
    from edgf.services import maturity_criteria
    from tests.process_helpers import COMMITTEE, decide
    from tests.test_maturity_evaluate import PHASE18_LEVEL5
    disk = (Path(settings.policy_dir) / maturity_criteria.CRITERIA_FILE).read_text(encoding="utf-8")
    with SessionLocal() as db:
        bundle = policy_bundles.active_bundle(db)
        files = {**bundle.files, maturity_criteria.CRITERIA_FILE: disk[:disk.index("  - level: 5")] + PHASE18_LEVEL5.lstrip("\n")}
        bundle.files, bundle.content_hash = files, policy.content_hash(files)
        db.commit()

    def level5_metrics():
        with SessionLocal() as db:
            return {a.metric for a in maturity_criteria.active(db).level(5).automatic}

    assert level5_metrics() == {"auto_remediation_rate", "predictive_quality_coverage"}
    r = client.post(f"{API}/policies/bundles", headers=U("proposer", "committee"),
                    json={"changes": {maturity_criteria.CRITERIA_FILE: disk}, "reason": "Phase 19 の Level 5 の指標"})
    assert r.status_code == 202, r.text
    proposal = r.json()
    decide(client, proposal["approval_id"], U("vice-chair", "committee"))
    assert level5_metrics() == {"auto_remediation_rate", "predictive_quality_coverage"}  # 公開の前は従来どおり
    assert client.post(f"{API}/policies/bundles/{proposal['proposal_version']}/publish", headers=COMMITTEE).status_code == 200
    assert level5_metrics() == {"autonomy_remediation_rate", "autonomy_false_positive_rate", "anomaly_detected_count",
                                "classification_suggestion_acceptance_rate"}
