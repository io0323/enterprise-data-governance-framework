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

from edgf.auth import OWNER, STEWARD
from edgf.config import settings
from edgf.errors import ProblemError
from edgf.models import Domain, KpiSnapshot, MaturityAssessment, MaturityReference, utcnow
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


# ---------------------------------------------------------------- 職務分離 (承認エンジンの種別の除外)
def excluded_decider(db: Session, principal: Any, req: Any) -> str | None:
    """対象ドメインの Owner と Steward は、Committee のロードを持っていても判定に加われない。"""
    row = db.get(MaturityAssessment, req.target_id)
    if row is None:
        return None
    domain = db.get(Domain, row.domain)
    if (principal.has_role(OWNER, STEWARD) and principal.in_domain(row.domain)) or (domain is not None and domain.owner == principal.subject):
        return f"対象ドメイン ({row.domain}) の Owner と Steward は、成熟度の判定に加われません (職務分離)"
    return None


# ---------------------------------------------------------------- 自己評価
def self_assess(db: Session, actor: str, principal: Any, row: MaturityAssessment, items: Sequence[dict[str, Any]]) -> MaturityAssessment:
    """ドメインの Owner が手動の基準ごとに、達成したかと証拠 (https の URI と説明) を入力する。判定の前なら出し直せる。

    最初の提出で、承認エンジンの maturity_assessment の案件を起票する (Committee が判定する)。
    """
    require_state(row, AUTO_EVALUATED, SELF_ASSESSED)
    known = {m["id"] for lv in (row.criteria or {}).get("levels", []) for m in lv["manual"]}
    ids = [i["criterion_id"] for i in items]
    unknown = sorted(set(ids) - known)
    if unknown:
        raise invalid(f"手動の基準にない id です: {unknown}")
    if len(ids) != len(set(ids)):
        raise invalid("同じ基準が 2 回入力されています")
    entries = [{"criterion_id": i["criterion_id"], "achieved": bool(i["achieved"]),
                "evidence": [{"uri": e["uri"], "description": e.get("description", "")} for e in i.get("evidence", [])]} for i in items]
    audit.guard_payload({"items": entries})  # 説明文に個人情報の可能性がある値があれば、保存せずに 422
    first = row.status == AUTO_EVALUATED
    row.self_assessment, row.self_assessed_by, row.self_assessed_at, row.status = entries, principal.subject, utcnow(), SELF_ASSESSED
    db.flush()
    audit.record(db, actor, "maturity.self_assess", "maturity_assessment", row.id, {
        "domain": row.domain, "items": entries, "resubmitted": not first})
    if row.approval_id is None:
        from edgf.services import approvals  # 循環 import 回避

        req = approvals.create(db, actor, type_name="maturity_assessment", requested_by=principal.subject, target_type="maturity_assessment",
                               target_id=row.id, reason=f"{row.domain} の成熟度の判定",
                               payload={"domain": row.domain, "indicative_level": row.indicative_level, "level_cap": row.level_cap},
                               due_at=row.due_at)
        row.approval_id = req.id
        db.flush()
    return row


# ---------------------------------------------------------------- 判定
ACCEPTED, REJECTED = "accepted", "rejected"


def decide(db: Session, actor: str, principal: Any, row: MaturityAssessment, *, level: int, manual: dict[str, str],
           reason: str) -> MaturityAssessment:
    """Committee の判定。上限 (min(目安, 前回 + 1)) を超えると 422。下げる判定には理由が必要。判定が済んだ記録は変更できない。"""
    from edgf import authz  # 循環 import 回避
    from edgf.models import ApprovalRequest
    from edgf.services import approvals

    require_state(row, SELF_ASSESSED)
    req = db.get(ApprovalRequest, row.approval_id, with_for_update=True)
    why = excluded_decider(db, principal, req)
    if why:  # 職務分離は入力の検証より先に (権限の無い人に検証の結果を返さない)
        authz.deny(db, principal, "maturity.decide", entity_type="maturity_assessment", entity_id=row.id, reason=why)
    cap = row.level_cap if row.level_cap is not None else 0
    previous = row.previous_level or 0
    if not 0 <= level <= cap:
        raise invalid(f"判定できるレベルは 0〜{cap} です (目安 {row.indicative_level}、前回 {previous} + 1 のうち低い方。飛び級はできません)",
                      level_cap=cap, indicative_level=row.indicative_level, previous_level=previous)
    if level < previous and not reason.strip():
        raise invalid(f"前回のレベル ({previous}) より下げる判定には理由が必要です")
    by_id = {m["id"]: lv["level"] for lv in (row.criteria or {}).get("levels", []) for m in lv["manual"]}
    unknown = sorted(set(manual) - set(by_id))
    if unknown:
        raise invalid(f"手動の基準にない id です: {unknown}")
    if any(v not in (ACCEPTED, REJECTED) for v in manual.values()):
        raise invalid("手動の基準の判断は accepted か rejected です")
    required = sorted(cid for cid, lv in by_id.items() if lv <= level)
    missing = [cid for cid in required if manual.get(cid) != ACCEPTED]
    if missing:
        raise invalid(f"Level {level} と判定するには、そのレベルまでの手動の基準をすべて認める必要があります: {missing}")
    approvals.decide(db, principal, req, approve=True, comment=reason)
    if req.status != "approved":  # 定足数が 1 なので起きないが、複数の判定者を求める変更に備える
        db.flush()
        return row
    row.decided_level, row.decided_by, row.decided_at, row.status = level, principal.subject, utcnow(), DECIDED
    row.decision = {"indicative_level": row.indicative_level, "previous_level": previous, "level_cap": cap, "level": level,
                    "manual": {cid: manual[cid] for cid in sorted(manual)}, "reason": reason.strip()}
    db.flush()
    audit.record(db, actor, "maturity.decide", "maturity_assessment", row.id, {"domain": row.domain, **row.decision})
    return row


# ---------------------------------------------------------------- 参照ドメイン (§20 先行ドメインをリファレンスとする)
def set_reference(db: Session, actor: str, principal: Any, domain: str, reason: str) -> MaturityReference:
    found = latest_decided(db, domain)
    if found is None:
        raise invalid(f"ドメイン {domain} には判定済みの評価がありません (参照ドメインには判定済みのドメインだけを指定できます)")
    row = db.get(MaturityReference, domain) or MaturityReference(domain=domain)
    row.level, row.assessment_id, row.reason, row.designated_by, row.designated_at = found.decided_level, found.id, reason, principal.subject, utcnow()
    db.add(row)
    db.flush()
    audit.record(db, actor, "maturity.reference.set", "maturity_reference", domain, {"level": row.level, "assessment_id": found.id,
                                                                                    "reason": reason})
    return row


def unset_reference(db: Session, actor: str, domain: str) -> None:
    row = db.get(MaturityReference, domain)
    if row is None:
        raise ProblemError(404, f"参照ドメインではありません: {domain}", type_="not-found")
    db.delete(row)
    db.flush()
    audit.record(db, actor, "maturity.reference.unset", "maturity_reference", domain, {"level": row.level})


def references(db: Session, *, target_level: int | None = None, exclude: str | None = None) -> list[dict[str, Any]]:
    """参照ドメイン。target_level を渡すと、そのレベルに到達しているものだけ (同じレベルを目指すドメインに示す)。"""
    stmt = select(MaturityReference).order_by(MaturityReference.level.desc(), MaturityReference.domain)
    rows = db.scalars(stmt).all()
    return [{"domain": r.domain, "level": r.level, "level_name": LEVEL_NAMES.get(r.level), "assessment_id": r.assessment_id,
             "reason": r.reason, "designated_by": r.designated_by, "designated_at": r.designated_at}
            for r in rows if (target_level is None or r.level >= target_level) and r.domain != exclude]


def inbox_operations(db: Session, req: Any) -> tuple[str, tuple[dict[str, Any], ...]]:
    """承認箱の案件の表示と、判定の操作 (レベルは上限まで、手動の基準ごとの判断、理由)。"""
    from edgf.services.approval_sources import field_, operation  # 循環 import 回避

    row = db.get(MaturityAssessment, req.target_id)
    cap = row.level_cap or 0
    fields = [field_("level", kind="select", required=True, options=[str(n) for n in range(cap + 1)])]
    fields += [field_(f"manual.{m['id']}", kind="select", options=[ACCEPTED, REJECTED])
               for lv in (row.criteria or {}).get("levels", []) if lv["level"] <= cap for m in lv["manual"]]
    fields.append(field_("reason"))
    path = f"/api/v1/maturity/assessments/{row.id}/decision"
    return (f"成熟度の判定: {row.domain} (目安 {row.indicative_level}、上限 {cap})",
            (operation("decide", "POST", path, "maturity.decide", fields=fields),))
