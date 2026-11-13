"""Phase 19c: 品質の異常の検知 (頑健な z 値、Theil–Sen の傾向、件数の急減、更新の間隔)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from edgf.db import SessionLocal
from edgf.models import DataAsset, Issue, QualityAnomaly, QualityRun, RemediationRun, utcnow
from edgf.services import quality_anomaly
from edgf.services.quality_anomaly import assess, robust_z, theil_sen
from tests.autonomy_helpers import AUT, CHAIR, audits, playbook, run_now, runs, use_playbooks
from tests.process_helpers import API, U, asset

NOISE = [0.002, -0.001, 0.0, 0.001, -0.002, 0.0015, -0.0005, 0.0005, -0.0015, 0.001, -0.001, 0.0, 0.002, -0.002, 0.0005,
         -0.001, 0.0015, 0.0, -0.0005, 0.001, -0.0015]


def history(asset_id: str, scores, counts=None, *, step_hours: float = 24, last_hours_ago: float = 1, dims=None) -> list[str]:
    """合成した実行の履歴 (古い順)。最新の実行は last_hours_ago 時間前。"""
    now = utcnow()
    n = len(scores)
    ids = []
    with SessionLocal() as db:
        for i, score in enumerate(scores):
            at = now - timedelta(hours=last_hours_ago + (n - 1 - i) * step_hours)
            row = QualityRun(asset_id=asset_id, executed_at=at, record_count=(counts or [1000] * n)[i], score=score,
                             dimension_scores=(dims[i] if dims else {"completeness": score}), results=[])
            db.add(row)
            db.flush()
            ids.append(row.id)
        db.commit()
    return ids


def _asset(client, qn="sales.orders", **kw) -> dict:
    return asset(client, qn, **kw)


def _assess(asset_id: str) -> dict:
    with SessionLocal() as db:
        return assess(db, db.get(DataAsset, asset_id))


def kinds(result) -> set[tuple[str, str]]:
    return {(f["kind"], f["metric"]) for f in result["findings"]}


# ---------------------------------------------------------------- 統計の部品
def test_robust_z_and_theil_sen():
    stats = robust_z(0.5, [1.0, 0.9, 1.1, 1.0, 0.95])
    assert stats["median"] == 1.0 and stats["mad"] == 0.05 and stats["z"] == pytest.approx(0.6745 * -0.5 / 0.05, abs=1e-3)
    assert robust_z(1.0, [1.0, 1.0, 1.0])["z"] is None  # MAD が 0
    slope, intercept = theil_sen([(0, 1.0), (1, 0.9), (2, 0.8), (3, 0.7), (4, 5.0)])  # 外れ値 1 点に引きずられない
    assert slope == pytest.approx(-0.1) and intercept == pytest.approx(1.0)
    assert theil_sen([(1, 1.0), (1, 2.0)]) is None


# ---------------------------------------------------------------- 合成した履歴での検出と予測
def test_insufficient_history_is_not_evaluated(client):
    a = _asset(client)
    history(a["id"], [0.98 + d for d in NOISE[:9]])
    result = _assess(a["id"])
    assert result["evaluated"] is False and result["reason"] == "insufficient_history" and result["runs"] == 9
    assert result["required"] == 10 and result["findings"] == []


def test_stable_history_has_no_findings(client):
    a = _asset(client)
    history(a["id"], [0.98 + d for d in NOISE], [1000 + int(d * 10000) for d in NOISE])
    result = _assess(a["id"])
    assert result["evaluated"] is True and result["findings"] == []


def test_score_outlier_with_evidence(client):
    a = _asset(client)
    history(a["id"], [0.98 + d for d in NOISE[:15]] + [0.70])
    result = _assess(a["id"])
    outlier = next(f for f in result["findings"] if f == {**f, "kind": "outlier", "metric": "score"})
    e = outlier["evidence"]
    assert e["value"] == 0.7 and e["median"] == pytest.approx(0.98, abs=0.002) and e["mad"] > 0 and e["z"] < -3.5
    assert len(e["values"]) == 15 and e["z_threshold"] == 3.5
    assert ("outlier", "dimension:completeness") in kinds(result)


def test_upward_change_is_not_an_outlier(client):
    a = _asset(client)
    history(a["id"], [0.90 + d for d in NOISE[:15]] + [0.999])
    assert not any(f["kind"] == "outlier" for f in _assess(a["id"])["findings"])


def test_constant_history_uses_the_median(client):
    a = _asset(client)
    history(a["id"], [1.0] * 12 + [0.99])
    [finding] = [f for f in _assess(a["id"])["findings"] if f["metric"] == "score" and f["kind"] == "outlier"]
    assert finding["evidence"]["mad"] == 0 and finding["evidence"]["z"] is None


def test_gradual_degradation_predicts_the_crossing(client):
    a = _asset(client)
    scores = [0.99 - 0.002 * i + NOISE[i] * 0.1 for i in range(20)]  # 1 日 0.002 ずつ下がる
    history(a["id"], scores)
    result = _assess(a["id"])
    trend = next(f for f in result["findings"] if f["kind"] == "trend")
    e = trend["evidence"]
    assert e["slope_per_day"] == pytest.approx(-0.002, abs=0.0002) and e["threshold"] == 0.9
    assert 20 <= e["days_until"] <= 30 and e["predicted_at"] and len(e["values"]) == 20
    assert not any(f["kind"] == "outlier" and f["metric"] == "score" for f in result["findings"])  # 緩やかな劣化は外れ値ではない


def test_far_or_already_crossed_trend_is_not_warned(client):
    a = _asset(client)
    history(a["id"], [0.999 - 0.0001 * i for i in range(20)])  # 割り込むのは数年後
    assert not any(f["kind"] == "trend" for f in _assess(a["id"])["findings"])
    b = _asset(client, "sales.lines")
    history(b["id"], [0.89 - 0.002 * i for i in range(20)])  # 既に閾値を下回っている
    assert not any(f["kind"] == "trend" for f in _assess(b["id"])["findings"])


def test_volume_drop(client):
    a = _asset(client)
    counts = [1000 + int(d * 10000) for d in NOISE[:15]] + [200]
    history(a["id"], [0.98] * 16, counts)
    [drop] = [f for f in _assess(a["id"])["findings"] if f["kind"] == "volume_drop"]
    assert drop["evidence"]["value"] == 200 and drop["evidence"]["median"] == pytest.approx(1000, abs=20)
    assert drop["evidence"]["floor"] == pytest.approx(drop["evidence"]["median"] / 2)


def test_update_delay(client):
    a = _asset(client)
    history(a["id"], [0.98 + d for d in NOISE[:15]], step_hours=24, last_hours_ago=24 * 5)  # 毎日の実行が 5 日止まっている
    [late] = [f for f in _assess(a["id"])["findings"] if f["kind"] == "freshness"]
    assert late["evidence"]["value"] == pytest.approx(120, abs=1) and late["evidence"]["median"] == pytest.approx(24, abs=0.1)
    assert late["evidence"]["unit"] == "hours"
    b = _asset(client, "sales.lines")
    history(b["id"], [0.98 + d for d in NOISE[:15]], step_hours=24, last_hours_ago=30)  # 少しの遅れは異常にしない
    assert not any(f["kind"] == "freshness" for f in _assess(b["id"])["findings"])


def test_window_uses_only_recent_runs(client):
    a = _asset(client)
    history(a["id"], [0.5] * 30 + [0.98 + d for d in NOISE[:20]] + [0.9805])  # 古い 0.5 は窓 (20 回) の外
    result = _assess(a["id"])
    assert result["window"] == 20 and not any(f["kind"] == "outlier" and f["metric"] == "score" for f in result["findings"])


# ---------------------------------------------------------------- 記録とプレイブック
def test_detection_records_once_with_evidence_and_event(client, autonomy_on):
    a = _asset(client)
    history(a["id"], [0.98 + d for d in NOISE[:15]] + [0.70])
    small = _asset(client, "sales.small")
    history(small["id"], [0.5] * 5)
    use_playbooks()
    result = run_now()
    assert result["analysis"]["quality_anomaly"]["assessed"] == 1  # 履歴が 10 回未満の資産は評価しない
    with SessionLocal() as db:
        rows = db.scalars(select(QualityAnomaly)).all()
    assert {(r.kind, r.metric) for r in rows} == {("outlier", "score"), ("outlier", "dimension:completeness")}
    assert all(r.evidence["median"] and r.evidence["values"] for r in rows)
    run_now()
    with SessionLocal() as db:
        assert len(db.scalars(select(QualityAnomaly)).all()) == 2  # 同じ実行の同じ異常は 1 回だけ
    r = client.get(f"{AUT}/anomalies", headers=CHAIR, params={"asset_id": a["id"]})
    assert len(r.json()) == 2 and r.json()[0]["qualified_name"] == "sales.orders" and r.json()[0]["evidence"]["z"] < -3.5


def test_anomaly_issue_playbook(client, autonomy_on):
    a = _asset(client)
    history(a["id"], [0.98 + d for d in NOISE[:15]] + [0.70])
    use_playbooks(playbook("PB-ANOMALY", trigger={"detector": "quality_anomaly"}, action="issue.open", undo_days=None, fp=None,
                           condition={"kinds": ["outlier"]}))
    run_now()
    executed = runs(status="executed")
    assert len(executed) == 2 and all(r.issue_id for r in executed[:1])
    with SessionLocal() as db:
        issues = db.scalars(select(Issue).where(Issue.source_ref.like("anomaly:%"))).all()
    assert {i.source_ref for i in issues} == {"anomaly:outlier:score", "anomaly:outlier:dimension:completeness"}
    assert all(i.assignee == "asset-steward" and i.kind == "quality" for i in issues)
    assert audits("issue.open")[-1].actor.startswith("system:autonomy:PB-ANOMALY")
    assert client.post(f"{AUT}/runs/{executed[0].id}/undo", headers=CHAIR, json={"reason": "x"}).status_code == 409  # 起票は取り消せない


def _certify(asset_id):
    with SessionLocal() as db:
        db.execute(update(DataAsset).where(DataAsset.id == asset_id).values(certification="certified", lifecycle_state="active"))
        db.commit()


def _decertify_playbook(mode="active"):
    return playbook("PB-DECERTIFY", trigger={"event": "quality.anomaly_detected"}, action="certification.revert_to_draft", mode=mode,
                    condition={"kinds": ["outlier", "volume_drop"], "certified_only": True}, fp=1.0)


def test_event_triggered_decertify_and_undo(client, autonomy_on):
    assert client.post(f"{API}/domains", headers=U("root", "admin"), json={"name": "sales", "owner": "dom-owner"}).status_code == 201
    a = _asset(client, domain="sales")
    _certify(a["id"])
    other = _asset(client, "sales.other", domain="sales")  # Certified でない資産は対象外
    counts = [1000 + int(d * 10000) for d in NOISE[:15]] + [200]
    history(a["id"], [0.995] * 16, counts)  # スコアは Certified の基準 (0.99) を満たしている
    history(other["id"], [0.995] * 16, counts)
    use_playbooks(_decertify_playbook())
    run_now()
    [row] = runs()
    assert row.trigger == "event:quality.anomaly_detected" and row.asset_id == a["id"] and row.status == "executed"
    assert row.before == {"certification": "certified"} and row.after == {"certification": "draft"}
    assert row.evidence["kind"] == "volume_drop" and row.evidence["value"] == 200
    with SessionLocal() as db:
        assert db.get(DataAsset, a["id"]).certification == "draft"
    assert audits("asset.update")[-1].actor == "system:autonomy:PB-DECERTIFY"
    run_now()
    assert len(runs()) == 1  # イベントは 1 回だけ処理する
    # 取り消し: Certified に戻す条件 (品質スコアなど) を満たしていれば戻る
    r = client.post(f"{AUT}/runs/{row.id}/undo", headers=U("asset-owner"), json={"reason": "件数の減少は計画どおり"})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        assert db.get(DataAsset, a["id"]).certification == "certified"


def test_decertify_undo_requires_certification_conditions(client, autonomy_on):
    a = _asset(client)
    _certify(a["id"])
    history(a["id"], [0.98 + d for d in NOISE[:15]] + [0.70])  # 最新のスコアが Certified の基準を下回る
    use_playbooks(_decertify_playbook())
    run_now()
    [row] = runs()  # スコアとディメンションの 2 つの異常。2 つ目は既に draft なので記録せずに飛ばす
    r = client.post(f"{AUT}/runs/{row.id}/undo", headers=U("asset-owner"), json={"reason": "x"})
    assert r.status_code == 409 and "条件" in r.json()["detail"]


def test_decertify_dry_run_keeps_certification(client, autonomy_on):
    a = _asset(client)
    _certify(a["id"])
    history(a["id"], [0.98] * 15 + [0.98], [1000] * 15 + [100])
    use_playbooks(_decertify_playbook(mode="dry_run"))
    run_now()
    [row] = runs()
    assert row.status == "dry_run" and row.after == {"certification": "draft"}
    with SessionLocal() as db:
        assert db.get(DataAsset, a["id"]).certification == "certified"


def test_event_overflow_is_kept_for_the_next_run(client, autonomy_on):
    ids = []
    for i in range(3):
        a = _asset(client, f"sales.t{i}")
        _certify(a["id"])
        history(a["id"], [0.98] * 15 + [0.98], [1000] * 15 + [100])
        ids.append(a["id"])
    use_playbooks(playbook("PB-DECERTIFY", trigger={"event": "quality.anomaly_detected"}, action="certification.revert_to_draft",
                           condition={"kinds": ["volume_drop"], "certified_only": True}, fp=1.0, limit=2))
    first = run_now()["playbooks"]["PB-DECERTIFY"]
    assert first["executed"] == 2 and first["over_limit"] == 1
    run_now()
    with SessionLocal() as db:
        assert [db.get(DataAsset, i).certification for i in ids] == ["draft"] * 3


def test_anomaly_check_endpoint_and_scope(client):
    a = _asset(client)
    history(a["id"], [0.98] * 5)
    r = client.get(f"{AUT}/assets/{a['id']}/anomaly-check", headers=CHAIR)
    assert r.status_code == 200 and r.json()["evaluated"] is False and r.json()["reason"] == "insufficient_history"
    assert client.get(f"{AUT}/assets/nope/anomaly-check", headers=CHAIR).status_code == 404
    assert client.get(f"{AUT}/assets/{a['id']}/anomaly-check", headers=U("x", "steward", "hr")).status_code == 404
    assert client.get(f"{AUT}/assets/{a['id']}/anomaly-check", headers=U("x", "consumer")).status_code == 403
    assert client.get(f"{AUT}/anomalies", headers=U("x", "consumer")).status_code == 403


def test_detection_uses_only_run_history(client):
    """異常の検知は実行の履歴 (スコア、件数、時刻) だけを読む。品質検査の本文の値は Control Plane に残らない。"""
    import inspect
    source = inspect.getsource(quality_anomaly.assess)
    assert "results" not in source and "records" not in source
    a = _asset(client)
    qa = U("qa", "admin,owner,custodian", "*")
    assert client.post(f"{API}/quality/assets/{a['id']}/rules", headers=qa, json={
        "name": "nn", "dimension": "completeness", "rule_type": "not_null", "column": "id"}).status_code == 201
    for _ in range(10):
        client.post(f"{API}/quality/assets/{a['id']}/runs", headers=qa, json={"records": [{"id": "MARKER-ANOMALY-7f3a"}]})
    result = _assess(a["id"])
    assert result["evaluated"] is True and "MARKER-ANOMALY-7f3a" not in str(result)
    with SessionLocal() as db:
        assert db.scalars(select(RemediationRun)).all() == []
