"""§20 ドメイン別の成熟度の評価 (Phase 18).

draft → auto_evaluated → self_assessed → decided (または cancelled)

- 自動の判定: 基準の自動の指標を、評価の期間のドメイン別の KPI のスナップショットで判定する。使ったスナップショットの ID と値を残す
- 目安: 自動の指標をレベル 1 から順にすべて満たした、最も高いレベル (Level 5 は Phase 19 まで計測できないので到達しない)
- 上限: min(目安, 前回の判定のレベル + 1)。初回の前回のレベルは 0。Committee はこれを超えて判定できない (飛び級の防止)
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.config import settings
from edgf.errors import ProblemError
from edgf.models import Domain, KpiSnapshot, MaturityAssessment, utcnow
from edgf.services import audit, maturity_criteria
from edgf.services.maturity_criteria import Automatic, Criteria

DRAFT, AUTO_EVALUATED, SELF_ASSESSED, DECIDED, CANCELLED = "draft", "auto_evaluated", "self_assessed", "decided", "cancelled"
OPEN_STATES = (DRAFT, AUTO_EVALUATED, SELF_ASSESSED)
LEVEL_NAMES = {0: "No Governance", 1: "Basic Policy", 2: "Managed", 3: "Standardized", 4: "Federated Governance", 5: "Autonomous Governance"}


def conflict(detail: str) -> ProblemError:
    return ProblemError(409, detail, type_="maturity-state-conflict")


def invalid(detail: str, **extensions: Any) -> ProblemError:
    return ProblemError(422, detail, type_="maturity-invalid", **extensions)


def get(db: Session, assessment_id: str, *, lock: bool = False) -> MaturityAssessment:
    row = db.get(MaturityAssessment, assessment_id, with_for_update=lock)
    if row is None:
        raise ProblemError(404, f"maturity assessment not found: {assessment_id}", type_="not-found")
    return row


def require_state(row: MaturityAssessment, *allowed: str) -> None:
    if row.status not in allowed:
        raise conflict(f"状態が {row.status} の評価ではこの操作はできません (判定が済んだ記録は変更できません)" if row.status == DECIDED
                       else f"状態が {row.status} の評価ではこの操作はできません")


def latest_decided(db: Session, domain: str, *, exclude: str | None = None) -> MaturityAssessment | None:
    stmt = select(MaturityAssessment).where(MaturityAssessment.domain == domain, MaturityAssessment.status == DECIDED)
    if exclude:
        stmt = stmt.where(MaturityAssessment.id != exclude)
    return db.scalars(stmt.order_by(MaturityAssessment.decided_at.desc(), MaturityAssessment.id)).first()


def previous_level(db: Session, domain: str, *, exclude: str | None = None) -> int:
    found = latest_decided(db, domain, exclude=exclude)
    return found.decided_level if found is not None and found.decided_level is not None else 0


def open_assessment(db: Session, domain: str) -> MaturityAssessment | None:
    return db.scalars(select(MaturityAssessment).where(MaturityAssessment.domain == domain,
                                                       MaturityAssessment.status.in_(OPEN_STATES))).first()


# ---------------------------------------------------------------- 起票
def create(db: Session, actor: str, *, domain: str, created_by: str, now: datetime | None = None) -> MaturityAssessment:
    if db.get(Domain, domain) is None:
        raise invalid(f"unknown domain: {domain}")
    if open_assessment(db, domain) is not None:
        raise conflict(f"ドメイン {domain} には判定の済んでいない評価があります")
    now = now or utcnow()
    row = MaturityAssessment(domain=domain, cycle=str(now.year), status=DRAFT, created_by=created_by, created_at=now,
                             due_at=now + timedelta(days=settings.maturity_due_days), criteria={}, auto_results=[], self_assessment=[],
                             decision={}, cancel_reason="")
    db.add(row)
    db.flush()
    audit.record(db, actor, "maturity.create", "maturity_assessment", row.id, {"domain": domain, "cycle": row.cycle, "due_at": row.due_at})
    return row


# ---------------------------------------------------------------- 自動の判定
def _period_end(db: Session, domain: str, today: date) -> date:
    """今日のスナップショットがあれば今日まで、無ければ昨日まで (日次のジョブの実行の時刻に左右されないため)。"""
    found = db.scalar(select(KpiSnapshot.id).where(KpiSnapshot.scope_type == "domain", KpiSnapshot.scope_name == domain,
                                                   KpiSnapshot.day == today))
    return today if found is not None else today - timedelta(days=1)


def _snapshots(db: Session, domain: str, start: date, end: date) -> dict[date, KpiSnapshot]:
    rows = db.scalars(select(KpiSnapshot).where(KpiSnapshot.scope_type == "domain", KpiSnapshot.scope_name == domain,
                                                KpiSnapshot.day >= start, KpiSnapshot.day <= end))
    return {row.day: row for row in rows}


def judge(criterion: Automatic, level: int, snapshots: dict[date, KpiSnapshot], end: date) -> dict[str, Any]:
    """1 つの自動の指標の判定。証拠として、使ったスナップショットの ID と値を残す。"""
    out: dict[str, Any] = {"id": criterion.id, "level": level, "metric": criterion.metric, "op": criterion.op,
                           "threshold": criterion.threshold, "days": criterion.days, "met": False, "reason": None, "evidence": []}
    if not criterion.measurable:
        out["reason"] = "not_measurable"
        return out
    if criterion.days:
        days = [end - timedelta(days=i) for i in range(criterion.days)]
        failures = []
        for day in sorted(days):
            snap = snapshots.get(day)
            value = None if snap is None else (snap.metrics or {}).get(criterion.metric)
            met = snap is not None and criterion.satisfied(value)
            out["evidence"].append({"snapshot_id": None if snap is None else snap.id, "day": day.isoformat(), "value": value, "met": met})
            if not met:
                failures.append("missing_snapshot" if snap is None else "below_threshold")
        out["met"] = not failures
        out["reason"] = None if not failures else ("missing_snapshot" if "missing_snapshot" in failures else "below_threshold")
        return out
    if not snapshots:
        out["reason"] = "no_snapshot"
        return out
    latest = snapshots[max(snapshots)]
    value = (latest.metrics or {}).get(criterion.metric)
    out["evidence"].append({"snapshot_id": latest.id, "day": latest.day.isoformat(), "value": value, "met": criterion.satisfied(value)})
    out["met"] = criterion.satisfied(value)
    out["reason"] = None if out["met"] else "below_threshold"
    return out


def indicative(criteria: Criteria, results: Sequence[dict[str, Any]]) -> int:
    """自動の指標をレベル 1 から順にすべて満たした、最も高いレベル。"""
    level = 0
    for lv in criteria.levels:
        own = [r for r in results if r["level"] == lv.level]
        if not lv.automatic or not all(r["met"] for r in own):
            break
        level = lv.level
    return level


def evaluate(db: Session, actor: str, row: MaturityAssessment, *, today: date | None = None) -> MaturityAssessment:
    """自動の判定。draft と auto_evaluated (やり直し) で行える。基準は active の Policy Bundle のものを写して残す。"""
    require_state(row, DRAFT, AUTO_EVALUATED)
    criteria = maturity_criteria.active(db)
    today = today or utcnow().date()
    end = _period_end(db, row.domain, today)
    start = end - timedelta(days=settings.maturity_cycle_days - 1)
    snapshots = _snapshots(db, row.domain, start, end)
    results = [judge(a, lv.level, snapshots, end) for lv in criteria.levels for a in lv.automatic]
    previous = previous_level(db, row.domain, exclude=row.id)
    level = indicative(criteria, results)
    row.criteria, row.period_start, row.period_end, row.auto_results = criteria.as_dict(), start, end, results
    row.indicative_level, row.previous_level, row.level_cap = level, previous, min(level, previous + 1)
    row.evaluated_at, row.status = utcnow(), AUTO_EVALUATED
    db.flush()
    audit.record(db, actor, "maturity.evaluate", "maturity_assessment", row.id, {
        "domain": row.domain, "indicative_level": level, "previous_level": previous, "level_cap": row.level_cap,
        "criteria_digest": criteria.digest, "met": sorted(r["id"] for r in results if r["met"]),
        "snapshot_ids": sorted({e["snapshot_id"] for r in results for e in r["evidence"] if e["snapshot_id"] is not None})})
    return row


def cancel(db: Session, actor: str, row: MaturityAssessment, *, reason: str) -> MaturityAssessment:
    require_state(row, *OPEN_STATES)
    if not reason.strip():
        raise invalid("取り消しには理由が必要です")
    row.status, row.cancelled_at, row.cancel_reason = CANCELLED, utcnow(), reason.strip()
    if row.approval_id:
        from edgf.models import ApprovalRequest
        from edgf.services import approvals
        req = db.get(ApprovalRequest, row.approval_id)
        if req is not None and req.status == "pending":
            approvals.cancel(db, actor, req, actor)
    db.flush()
    audit.record(db, actor, "maturity.cancel", "maturity_assessment", row.id, {"domain": row.domain, "reason": row.cancel_reason})
    return row


# ---------------------------------------------------------------- 表示
def gaps(row: MaturityAssessment, target: int) -> list[dict[str, Any]]:
    """target のレベルに足りない基準 (自動の指標は判定の結果、手動の項目は自己評価)。"""
    if target < 1:
        return []
    out = [{"kind": "automatic", "id": r["id"], "level": r["level"], "metric": r["metric"], "reason": r["reason"]}
           for r in row.auto_results or [] if r["level"] <= target and not r["met"]]
    achieved = {i["criterion_id"] for i in row.self_assessment or [] if i.get("achieved")}
    for lv in (row.criteria or {}).get("levels", []):
        if lv["level"] <= target:
            out.extend({"kind": "manual", "id": m["id"], "level": lv["level"], "description": m["description"]}
                       for m in lv["manual"] if m["id"] not in achieved)
    return out


def view(row: MaturityAssessment, *, detail: bool = True) -> dict[str, Any]:
    out = {"id": row.id, "domain": row.domain, "cycle": row.cycle, "status": row.status, "created_by": row.created_by,
           "created_at": row.created_at, "due_at": row.due_at, "period_start": row.period_start, "period_end": row.period_end,
           "indicative_level": row.indicative_level, "previous_level": row.previous_level, "level_cap": row.level_cap,
           "decided_level": row.decided_level, "decided_by": row.decided_by, "decided_at": row.decided_at, "approval_id": row.approval_id,
           "self_assessed_by": row.self_assessed_by, "self_assessed_at": row.self_assessed_at, "cancel_reason": row.cancel_reason or None}
    if detail:
        target = (row.previous_level or 0) + 1 if row.previous_level is not None else None
        out.update({"criteria": row.criteria, "auto_results": row.auto_results, "self_assessment": row.self_assessment,
                    "decision": row.decision, "next_level_gaps": gaps(row, target) if target and target <= 5 else []})
    return out
