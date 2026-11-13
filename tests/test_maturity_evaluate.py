"""Phase 18b: 評価の起票、自動の判定、目安と上限."""
from __future__ import annotations

from datetime import timedelta

import pytest

from edgf.db import SessionLocal
from edgf.models import MaturityAssessment, utcnow
from tests.maturity_helpers import CHAIR, LEVEL1, LEVEL2, LEVEL3, LEVEL4, MAT, create, decided, domain, evaluate, snapshots
from tests.process_helpers import ADMIN, U, audit_actions


@pytest.fixture()
def sales(client):
    domain(client)
    return client


def result_of(body, criterion_id):
    return next(r for r in body["auto_results"] if r["id"] == criterion_id)


# ---------------------------------------------------------------- 起票
def test_create_and_permissions(sales):
    a = create(sales)
    assert a["status"] == "draft" and a["domain"] == "sales" and a["indicative_level"] is None
    assert sales.post(f"{MAT}/assessments", headers=CHAIR, json={"domain": "sales"}).status_code == 409  # 未判定の評価がある
    assert sales.post(f"{MAT}/assessments", headers=CHAIR, json={"domain": "nope"}).status_code == 422
    for who in (U("o", "owner", "sales"), U("s", "steward", "sales"), U("c")):
        assert sales.post(f"{MAT}/assessments", headers=who, json={"domain": "sales"}).status_code == 403
    # 認証済みの全員が参照できる
    assert sales.get(f"{MAT}/assessments", headers=U("anyone")).json()[0]["id"] == a["id"]
    assert sales.get(f"{MAT}/assessments/{a['id']}", headers=U("anyone")).status_code == 200


def test_admin_can_create(sales):
    assert create(sales, who=ADMIN)["created_by"] == "root"


# ---------------------------------------------------------------- 自動の判定
def test_no_snapshots_means_level_0(sales):
    body = evaluate(sales, create(sales)["id"])
    assert body["status"] == "auto_evaluated" and body["indicative_level"] == 0 and body["level_cap"] == 0
    assert result_of(body, "L1-OWNER-RATE")["reason"] == "no_snapshot"


@pytest.mark.parametrize("value,met", [(0.5, True), (0.4999, False), (None, False)])
def test_threshold_boundary(sales, value, met):
    snapshots("sales", {"owner_assignment_rate": value})
    body = evaluate(sales, create(sales)["id"])
    assert result_of(body, "L1-OWNER-RATE")["met"] is met and body["indicative_level"] == (1 if met else 0)


def test_measured_operator(sales):
    snapshots("sales", {"owner_assignment_rate": 0.8, "quality_score_avg": None})
    body = evaluate(sales, create(sales)["id"])
    assert result_of(body, "L2-QUALITY-MEASURED")["met"] is False and body["indicative_level"] == 1


def test_levels_are_cumulative(sales):
    """上のレベルの指標を満たしても、下のレベルを満たさなければ目安は上がらない。"""
    snapshots("sales", {**LEVEL3, "owner_assignment_rate": 0.4})
    assert evaluate(sales, create(sales)["id"])["indicative_level"] == 0


def test_evidence_records_snapshot_ids_and_values(sales):
    ids = snapshots("sales", LEVEL3, days=3)
    body = evaluate(sales, create(sales)["id"])
    evidence = result_of(body, "L3-CATALOG-RATE")["evidence"]
    assert evidence == [{"snapshot_id": ids[0], "day": utcnow().date().isoformat(), "value": 0.95, "met": True}]  # 最新のスナップショット
    event = [e for e in audit_actions(sales) if e["action"] == "maturity.evaluate"][-1]
    assert ids[0] in event["payload"]["snapshot_ids"] and event["payload"]["indicative_level"] == 3


def test_level_4_needs_90_days(sales):
    snapshots("sales", LEVEL4, days=90)
    body = evaluate(sales, create(sales)["id"])
    assert body["indicative_level"] == 4
    certified = result_of(body, "L4-CERTIFIED")
    assert certified["met"] and len(certified["evidence"]) == 90 and all(e["snapshot_id"] for e in certified["evidence"])


@pytest.mark.parametrize("case", ["one_day_below", "one_day_missing", "89_days"])
def test_level_4_fails_if_any_day_fails(sales, case):
    if case == "one_day_below":
        snapshots("sales", LEVEL4, days=90, override={45: {"certified_rate": 0.49}})
        reason = "below_threshold"
    elif case == "one_day_missing":
        snapshots("sales", LEVEL4, days=90, skip=(30,))
        reason = "missing_snapshot"
    else:
        snapshots("sales", LEVEL4, days=89)
        reason = "missing_snapshot"
    body = evaluate(sales, create(sales)["id"])
    certified = result_of(body, "L4-CERTIFIED")
    assert certified["met"] is False and certified["reason"] == reason and body["indicative_level"] == 3
    assert len(certified["evidence"]) == 90 and len([e for e in certified["evidence"] if not e["met"]]) == 1


def test_period_ends_yesterday_without_todays_snapshot(sales):
    yesterday = utcnow().date() - timedelta(days=1)
    snapshots("sales", LEVEL4, days=90, end=yesterday)
    body = evaluate(sales, create(sales)["id"])
    assert body["period_end"] == yesterday.isoformat() and body["indicative_level"] == 4


def test_level_5_is_not_reachable(sales):
    snapshots("sales", {**LEVEL4, "auto_remediation_rate": 1.0, "predictive_quality_coverage": 1.0}, days=90)
    body = evaluate(sales, create(sales)["id"])
    assert body["indicative_level"] == 4
    assert {result_of(body, c)["reason"] for c in ("L5-AUTO-REMEDIATION", "L5-PREDICTIVE")} == {"not_measurable"}


# ---------------------------------------------------------------- 上限
def test_first_assessment_cap_is_1(sales):
    snapshots("sales", LEVEL4, days=90)
    body = evaluate(sales, create(sales)["id"])
    assert body["previous_level"] == 0 and body["indicative_level"] == 4 and body["level_cap"] == 1


def test_cap_is_previous_plus_one_or_indicative(sales):
    decided("sales", 2)
    snapshots("sales", LEVEL4, days=90)
    body = evaluate(sales, create(sales)["id"])
    assert (body["previous_level"], body["indicative_level"], body["level_cap"]) == (2, 4, 3)


def test_cap_is_indicative_when_lower(sales):
    decided("sales", 3)
    snapshots("sales", LEVEL1)
    body = evaluate(sales, create(sales)["id"])
    assert (body["previous_level"], body["indicative_level"], body["level_cap"]) == (3, 1, 1)


def test_next_level_gaps(sales):
    snapshots("sales", LEVEL1)
    body = evaluate(sales, create(sales)["id"])
    # 次のレベル (前回 0 + 1 = 1) に足りないもの: 自己評価していない手動の項目
    assert {g["id"] for g in body["next_level_gaps"]} == {"L1-COMMITTEE", "L1-OWNER-APPOINT"}


def test_criteria_are_frozen_in_the_assessment(sales):
    snapshots("sales", LEVEL2)
    body = evaluate(sales, create(sales)["id"])
    assert [lv["level"] for lv in body["criteria"]["levels"]] == [1, 2, 3, 4, 5] and body["criteria"]["digest"]


# ---------------------------------------------------------------- 状態
def test_reevaluate_and_cancel(sales):
    a = create(sales)
    evaluate(sales, a["id"])
    snapshots("sales", LEVEL2)
    assert evaluate(sales, a["id"])["indicative_level"] == 2  # やり直せる
    assert sales.post(f"{MAT}/assessments/{a['id']}/cancel", headers=CHAIR, json={"reason": " "}).status_code == 422
    assert sales.post(f"{MAT}/assessments/{a['id']}/cancel", headers=U("o", "owner", "sales"), json={"reason": "x"}).status_code == 403
    r = sales.post(f"{MAT}/assessments/{a['id']}/cancel", headers=CHAIR, json={"reason": "組織の再編"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert sales.post(f"{MAT}/assessments/{a['id']}/evaluate", headers=CHAIR).status_code == 409
    create(sales)  # 取り消した後は新しく起票できる


def test_decided_record_cannot_be_changed(sales):
    a = create(sales)
    with SessionLocal() as db:
        row = db.get(MaturityAssessment, a["id"])
        row.status, row.decided_level = "decided", 1
        db.commit()
    r = sales.post(f"{MAT}/assessments/{a['id']}/evaluate", headers=CHAIR)
    assert r.status_code == 409 and "変更できません" in r.json()["detail"]
    assert sales.post(f"{MAT}/assessments/{a['id']}/cancel", headers=CHAIR, json={"reason": "x"}).status_code == 409


def test_criteria_endpoint(sales):
    body = sales.get(f"{MAT}/criteria", headers=U("anyone")).json()
    assert [lv["level"] for lv in body["levels"]] == [1, 2, 3, 4, 5]
