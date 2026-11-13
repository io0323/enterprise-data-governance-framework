"""§5 Continuous Data Quality / §20 Level 5: 品質の異常の検知と劣化の予測 (Phase 19).

品質検査の実行履歴 (スコア、ディメンション別のスコア、件数) と、実行の間隔だけを使う。データの値は使わない。
標準ライブラリ (statistics) だけで実装し、根拠 (使った値、中央値、MAD、z 値、傾き、予測の時期) を結果に残す。

- 外れ値: 直近 N 回 (既定 20 回) の中央値と MAD による頑健な z 値 (0.6745 × (x − 中央値) / MAD)。
  スコアとディメンション別のスコアは下がる側だけを見る。|z| が閾値 (既定 3.5。Iglewicz と Hoaglin の目安) 以上で異常。
  MAD が 0 (履歴が一定) のときは z を求めず、中央値から外れたら異常とする。
- 傾向: Theil–Sen 法 (2 点の傾きの中央値) でスコアの傾きを推定し、閾値を割り込む時期を予測する。
  まだ割り込んでおらず、予測の時期が期間 (既定 30 日) 以内なら「予測の警告」とする。
- 件数の急減: 最新の件数が履歴の中央値の (1 − 割合) を下回る (既定は半分未満)。
- 更新の間隔の異常: 最後の実行からの経過が、実行の間隔の中央値の 2 倍を超え、かつ頑健な z 値が閾値以上。
- 履歴が 10 回未満の資産は評価しない (結果に insufficient_history を示す)。

限界: 季節性 (曜日・月末の変動)、外部の要因、ルールの追加・変更によるスコアの変化は扱わない。
"""
from __future__ import annotations

import statistics
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from edgf.config import settings
from edgf.enums import Certification, EscalationLevel, LifecycleState
from edgf.models import DataAsset, QualityAnomaly, QualityRun, new_id, utcnow
from edgf.services import asset_update, audit, autonomy, issues, outbox
from edgf.services.autonomy import AutonomyConflict, Candidate, Executor
from edgf.services.certification import certification_blockers

MIN_HISTORY = 10
CONSISTENCY = 0.6745  # 正規分布のとき MAD を標準偏差にそろえる係数 (1 / 1.4826)
FRESHNESS_FACTOR = 2.0
EPSILON = 1e-9
RECENT_DAYS = 30  # プレイブックの対象にする異常の期間


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


def robust_z(value: float, values: Sequence[float]) -> dict[str, Any]:
    median = statistics.median(values)
    mad = statistics.median(abs(v - median) for v in values)
    z = CONSISTENCY * (value - median) / mad if mad > EPSILON else None
    return {"value": _round(value), "median": _round(median), "mad": _round(mad), "z": _round(z, 4), "values": [_round(v) for v in values]}


def theil_sen(points: Sequence[tuple[float, float]]) -> tuple[float, float] | None:
    """(x, y) の傾きと切片。x が同じ 2 点は使わない。傾きを求められなければ None。"""
    slopes = [(yj - yi) / (xj - xi) for i, (xi, yi) in enumerate(points) for xj, yj in points[i + 1:] if abs(xj - xi) > EPSILON]
    if not slopes:
        return None
    slope = statistics.median(slopes)
    intercept = statistics.median(y - slope * x for x, y in points)
    return slope, intercept


def _low_outlier(stats: dict[str, Any], z_threshold: float) -> bool:
    if stats["z"] is not None:
        return stats["z"] <= -z_threshold
    return stats["value"] < stats["median"] - EPSILON  # 履歴が一定で、そこから下がった


def _metrics(history: Sequence[QualityRun], latest: QualityRun) -> list[tuple[str, Callable[[QualityRun], float | None]]]:
    metrics: list[tuple[str, Callable[[QualityRun], float | None]]] = [("score", lambda r: r.score)]
    for name in sorted(latest.dimension_scores or {}):
        metrics.append((f"dimension:{name}", lambda r, n=name: (r.dimension_scores or {}).get(n)))
    return metrics


def assess(db: Session, asset: DataAsset, now: datetime | None = None) -> dict[str, Any]:
    """1 つの資産の評価 (保存しない)。"""
    now = now or utcnow()
    window = max(settings.anomaly_window, MIN_HISTORY)
    z_threshold = settings.anomaly_z_threshold
    runs = list(reversed(db.scalars(select(QualityRun).where(QualityRun.asset_id == asset.id)
                                    .order_by(QualityRun.executed_at.desc(), QualityRun.id.desc()).limit(window + 1)).all()))
    base = {"asset_id": asset.id, "runs": len(runs), "required": MIN_HISTORY, "window": window}
    if len(runs) < MIN_HISTORY:
        return {**base, "evaluated": False, "reason": "insufficient_history", "findings": []}
    latest, history = runs[-1], runs[:-1]
    findings: list[dict[str, Any]] = []
    # 外れ値 (スコアとディメンション別のスコア。下がる側だけ)
    for metric, get in _metrics(history, latest):
        values = [v for v in (get(r) for r in history) if v is not None]
        value = get(latest)
        if value is None or len(values) < MIN_HISTORY - 1:
            continue
        stats = robust_z(value, values)
        if _low_outlier(stats, z_threshold):
            findings.append({"kind": "outlier", "metric": metric, "evidence": {**stats, "z_threshold": z_threshold}})
    # 件数の急減
    counts = [r.record_count for r in history]
    stats = robust_z(latest.record_count, counts)
    floor = stats["median"] * (1 - settings.anomaly_volume_drop)
    if stats["median"] > 0 and latest.record_count < floor:
        findings.append({"kind": "volume_drop", "metric": "record_count",
                         "evidence": {**stats, "drop_ratio": settings.anomaly_volume_drop, "floor": _round(floor)}})
    # 傾向 (Theil–Sen) と、閾値を割り込む時期の予測
    origin = runs[0].executed_at
    points = [((r.executed_at - origin).total_seconds() / 86400, r.score) for r in runs]
    fit = theil_sen(points)
    threshold = settings.anomaly_score_threshold
    if fit is not None:
        slope, intercept = fit
        if slope < -EPSILON and latest.score >= threshold:
            crossing = (threshold - intercept) / slope
            days_until = crossing - points[-1][0]
            if 0 <= days_until <= settings.anomaly_horizon_days:
                findings.append({"kind": "trend", "metric": "score", "evidence": {
                    "slope_per_day": _round(slope, 8), "intercept": _round(intercept), "threshold": threshold,
                    "days_until": _round(days_until, 2), "predicted_at": (origin + timedelta(days=crossing)).isoformat(),
                    "horizon_days": settings.anomaly_horizon_days, "values": [_round(y) for _, y in points],
                    "days": [_round(x, 4) for x, _ in points]}})
    # 更新の間隔
    intervals = [(b.executed_at - a.executed_at).total_seconds() / 3600 for a, b in zip(runs, runs[1:])]
    gap = (now - latest.executed_at).total_seconds() / 3600
    stats = robust_z(gap, intervals)
    late = gap > stats["median"] * FRESHNESS_FACTOR and stats["median"] > 0
    if late and (stats["z"] is None or stats["z"] >= z_threshold):
        findings.append({"kind": "freshness", "metric": "interval", "evidence": {**stats, "unit": "hours", "factor": FRESHNESS_FACTOR,
                                                                                "z_threshold": z_threshold}})
    return {**base, "evaluated": True, "latest_run_id": latest.id, "latest_executed_at": latest.executed_at, "findings": findings}


def _fingerprint(asset_id: str, kind: str, metric: str, run_id: str) -> str:
    return f"{asset_id}:{kind}:{metric}:{run_id}"


def detect_all(db: Session, now: datetime) -> dict[str, Any]:
    """履歴が 10 回以上ある資産を評価し、新しい異常を記録してイベントを出す (分析。統治の状態は変えない)。"""
    eligible = db.scalars(select(QualityRun.asset_id).join(DataAsset, QualityRun.asset_id == DataAsset.id)
                          .where(DataAsset.lifecycle_state != LifecycleState.DELETED.value)
                          .group_by(QualityRun.asset_id).having(func.count() >= MIN_HISTORY).order_by(QualityRun.asset_id)).all()
    detected = 0
    for asset_id in eligible:
        result = assess(db, db.get(DataAsset, asset_id), now)
        for f in result["findings"]:
            fingerprint = _fingerprint(asset_id, f["kind"], f["metric"], result["latest_run_id"])
            if db.scalar(select(QualityAnomaly.id).where(QualityAnomaly.fingerprint == fingerprint)):
                continue
            row = QualityAnomaly(id=new_id(), asset_id=asset_id, kind=f["kind"], metric=f["metric"], run_id=result["latest_run_id"],
                                 fingerprint=fingerprint, detected_at=now, evidence=f["evidence"])
            db.add(row)
            db.flush()
            outbox.emit(db, "quality.anomaly_detected", row.id, {"anomaly_id": row.id, "asset_id": asset_id, "kind": row.kind, "metric": row.metric})
            detected += 1
    return {"assessed": len(eligible), "detected": detected}


def view(row: QualityAnomaly, asset: DataAsset | None = None) -> dict[str, Any]:
    return {"id": row.id, "asset_id": row.asset_id, "qualified_name": asset.qualified_name if asset else None, "kind": row.kind,
            "metric": row.metric, "run_id": row.run_id, "detected_at": row.detected_at, "evidence": row.evidence}


# ---------------------------------------------------------------- プレイブックの対象 (anomaly)
def _candidate(db: Session, row: QualityAnomaly, cond: Mapping[str, Any]) -> Candidate | None:
    if row.kind not in cond["kinds"]:
        return None
    asset = db.get(DataAsset, row.asset_id)
    if asset is None or (cond["certified_only"] and asset.certification != Certification.CERTIFIED.value):
        return None
    return Candidate("anomaly", row.id, row.id, asset, {"kind": row.kind, "metric": row.metric, "run_id": row.run_id, **row.evidence})


def detect_candidates(db: Session, cond: Mapping[str, Any], now: datetime) -> list[Candidate]:
    rows = db.scalars(select(QualityAnomaly).where(QualityAnomaly.detected_at >= now - timedelta(days=RECENT_DAYS),
                                                   QualityAnomaly.kind.in_(cond["kinds"]))
                      .order_by(QualityAnomaly.detected_at, QualityAnomaly.id)).all()
    return [c for c in (_candidate(db, r, cond) for r in rows) if c is not None]


def load_candidate(db: Session, target_id: str, cond: Mapping[str, Any], now: datetime) -> Candidate | None:
    row = db.get(QualityAnomaly, target_id)
    return _candidate(db, row, cond) if row else None


# ---------------------------------------------------------------- 区分 A: Issue の起票 (取り消せない)
def _summary(c: Candidate) -> str:
    e = c.evidence
    if e["kind"] == "trend":
        return f"傾き {e['slope_per_day']}/日。{e['days_until']} 日後に閾値 {e['threshold']} を割り込む予測"
    if e["kind"] == "freshness":
        return f"最後の実行から {e['value']} 時間 (間隔の中央値 {e['median']} 時間)"
    return f"値 {e['value']} (中央値 {e['median']}、MAD {e['mad']}、z {e['z']})"


def _issue_title(c: Candidate) -> str:
    return f"[DQ-ANOMALY] {c.asset.qualified_name}: {c.evidence['kind']} {c.evidence['metric']}"


def _open_snapshot(db: Session, c: Candidate) -> dict[str, Any]:
    return {"issue": None}


def _open_preview(db: Session, c: Candidate, actor: str, now: datetime) -> dict[str, Any]:
    return {"issue": {"title": _issue_title(c), "source_ref": f"anomaly:{c.evidence['kind']}:{c.evidence['metric']}",
                      "assignee": issues.assignee_for(c.asset, EscalationLevel.STEWARD)}}


def _open_apply(db: Session, actor: str, c: Candidate, pb: Any, now: datetime) -> dict[str, Any]:
    issue = issues.open_issue(db, actor, asset=c.asset, kind="quality", severity="high" if c.evidence["kind"] == "trend" else "medium",
                              source_ref=f"anomaly:{c.evidence['kind']}:{c.evidence['metric']}", title=_issue_title(c),
                              description=f"{_summary(c)} (§5 Continuous Data Quality。根拠は実行の記録 {c.target_id} にあります)")
    if issue is None:  # 同じ異常の Issue が未解決で残っている
        return {"after": {"issue": None}, "result": {"duplicate": True}}
    return {"after": {"issue": {"id": issue.id, "assignee": issue.assignee}}, "issue_id": issue.id}


# ---------------------------------------------------------------- 区分 A: Certification を draft に戻す (取り消せる)
def _cert_snapshot(db: Session, c: Candidate) -> dict[str, Any]:
    return {"certification": c.asset.certification}


def _cert_preview(db: Session, c: Candidate, actor: str, now: datetime) -> dict[str, Any]:
    return {"certification": Certification.DRAFT.value}


def _cert_apply(db: Session, actor: str, c: Candidate, pb: Any, now: datetime) -> dict[str, Any]:
    asset = db.get(DataAsset, c.asset.id, with_for_update=True)
    if asset.certification != Certification.CERTIFIED.value:
        raise AutonomyConflict("資産は Certified ではありません")
    asset_update.apply(db, asset, {"certification": Certification.DRAFT.value}, actor)
    return {"after": {"certification": asset.certification}}


def _cert_undo(db: Session, actor: str, row: Any) -> None:
    asset = db.get(DataAsset, row.asset_id, with_for_update=True) if row.asset_id else None
    if asset is None or asset.certification != Certification.DRAFT.value:
        raise AutonomyConflict("資産の Certification が、自動の操作の後に変わっています (取り消せません)")
    blockers = certification_blockers(db, asset)
    if blockers:
        raise AutonomyConflict(f"Certified に戻す条件を満たしていません: {'; '.join(blockers)}")
    asset_update.apply(db, asset, {"certification": Certification.CERTIFIED.value}, actor)
    audit.record(db, actor, "asset.certify", "asset", asset.id, {"certification": asset.certification, "run_id": row.id,
                                                                 "reason": "自動の操作の取り消し"})


autonomy.register_analysis("quality_anomaly", detect_all)
autonomy.register_kind("anomaly", detect_candidates, load_candidate)
autonomy.register_operation("issue.open", Executor(_open_snapshot, _open_preview, _open_apply))
autonomy.register_operation("certification.revert_to_draft", Executor(
    _cert_snapshot, _cert_preview, _cert_apply, _cert_undo, lambda db, c: db.get(DataAsset, c.asset.id).certification == Certification.CERTIFIED.value))
