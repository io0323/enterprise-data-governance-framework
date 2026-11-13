"""§20 Level 5 自律統治: プレイブックの実行、dry_run、取り消し、安全装置 (Phase 19。docs/adr/0001).

1 回の実行 (ジョブ autonomy_run) の流れ:
1. EDGF_AUTONOMY_ENABLED が false なら何もしない (記録も作らない)。全体の停止スイッチが入っていても何もしない。
2. 分析 (品質の異常の検知、分類の提案の作成) を行う。分析は状態を変えない (結果を記録するだけ)。
3. active の Policy Bundle のプレイブックごとに、契機から対象を集め、条件で絞り、件数の上限まで操作する。
   - dry_run: 状態を変えず、「行うはずだった操作」と前後の状態だけを RemediationRun に記録する
   - active: 区分 A は実行し、区分 B は提案 (承認の案件や Issue) を作るだけ。どちらも監査に記録する
   - 上限を超えた分は実行せず、Issue を起票する。操作の前ごとに停止スイッチを確かめる
4. 直近 30 日の「誤って実行した率」(取り消された件数 ÷ 実行した件数) が閾値を超えたプレイブックは自動で止める。

実行の主体は system:autonomy:<プレイブック>。承認 (✓) の権限は持たない (authz と承認エンジンが拒否する)。
"""
from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.config import settings
from edgf.enums import EscalationLevel, IssueStatus, LifecycleState
from edgf.models import (AccessGrant, ApprovalRequest, AutonomyControl, AutonomyPlaybookState, DataAsset, Domain, Issue, OutboxEvent,
                         QualityRun, RemediationRun, new_id, utcnow)
from edgf.services import approvals, audit, autonomy_catalog, issues, outbox
from edgf.services.autonomy_catalog import A, DRY_RUN, CATALOG, Playbook

DRY_RUN_STATUS, EXECUTED, PROPOSED, UNDONE, FAILED = "dry_run", "executed", "proposed", "undone", "failed"
RUN_STATUSES = (DRY_RUN_STATUS, EXECUTED, PROPOSED, UNDONE, FAILED)
FALSE_POSITIVE_WINDOW_DAYS = 30
COMMITTEE_ASSIGNEE = approvals.COMMITTEE_ASSIGNEE
# 分析と、対象の種類・操作を追加する拡張 (読み込むと register_* を呼ぶ)
EXTENSIONS: tuple[str, ...] = ()


class AutonomyConflict(ValueError):
    """現在の状態では実行できない (409)。"""


@dataclass
class Candidate:
    kind: str
    target_id: str
    fingerprint: str
    asset: DataAsset | None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Executor:
    snapshot: Callable[[Session, Candidate], dict[str, Any]]                  # 操作の前の状態
    preview: Callable[[Session, Candidate, str, datetime], dict[str, Any]]    # 操作の後の状態 (予想。dry_run で記録する)
    apply: Callable[[Session, str, Candidate, Playbook, datetime], dict[str, Any]]  # {"after", "approval_id", "issue_id", "result"}
    undo: Callable[[Session, str, RemediationRun], None] | None = None


Detector = Callable[[Session, Mapping[str, Any], datetime], list[Candidate]]
Loader = Callable[[Session, str, Mapping[str, Any], datetime], "Candidate | None"]
DETECTORS: dict[str, Detector] = {}
LOADERS: dict[str, Loader] = {}
EXECUTORS: dict[str, Executor] = {}
ANALYSES: dict[str, Callable[[Session, datetime], dict[str, Any]]] = {}


def register_kind(kind: str, detect: Detector, load: Loader | None = None) -> None:
    DETECTORS[kind] = detect
    if load is not None:
        LOADERS[kind] = load


def register_operation(op_id: str, executor: Executor) -> None:
    if op_id not in CATALOG:
        raise ValueError(f"カタログに無い操作です: {op_id}")
    if CATALOG[op_id].undoable and executor.undo is None:
        raise ValueError(f"{op_id}: 取り消せる操作には取り消しの処理が必要です")
    EXECUTORS[op_id] = executor


def register_analysis(name: str, fn: Callable[[Session, datetime], dict[str, Any]]) -> None:
    ANALYSES[name] = fn


def load_extensions() -> None:
    for name in EXTENSIONS:
        importlib.import_module(name)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# ---------------------------------------------------------------- 停止スイッチと状態
def control(db: Session, *, lock: bool = False) -> AutonomyControl:
    row = db.get(AutonomyControl, 1, with_for_update=lock)
    if row is None:
        row = AutonomyControl(id=1, stopped=False, reason="")
        db.add(row)
        db.flush()
    return row


def is_stopped(db: Session) -> bool:
    """操作の前ごとに読み直す (別の要求で停止されたら、次の操作から止まる)。"""
    return bool(db.scalar(select(AutonomyControl.stopped).where(AutonomyControl.id == 1)))


def state(db: Session, playbook_id: str, *, lock: bool = False) -> AutonomyPlaybookState:
    row = db.get(AutonomyPlaybookState, playbook_id, with_for_update=lock)
    if row is None:
        row = AutonomyPlaybookState(playbook_id=playbook_id, suspended=False, suspended_reason="")
        db.add(row)
        db.flush()
    return row


def stop(db: Session, actor: str, reason: str) -> AutonomyControl:
    """全体の停止。安全側の操作なので、すぐに効く (✓ は不要)。停止中に呼んでも状態は変えない。"""
    row = control(db, lock=True)
    if row.stopped:
        return row
    row.stopped, row.reason, row.changed_by, row.changed_at = True, reason, actor, utcnow()
    db.flush()
    audit.record(db, actor, "autonomy.stop", "autonomy", "control", {"reason": reason})
    outbox.emit(db, "autonomy.stopped", "control", {"stopped_by": actor})
    return row


def resume(db: Session, actor: str, reason: str, playbook_id: str | None = None) -> dict[str, Any]:
    """再開 (全体、または自動停止したプレイブック)。権限の判定 (autonomy.resume ✓) は呼び出し側で行う。"""
    now = utcnow()
    if playbook_id:
        row = db.get(AutonomyPlaybookState, playbook_id, with_for_update=True)
        if row is None or not row.suspended:
            raise AutonomyConflict(f"プレイブック {playbook_id} は止まっていません")
        row.suspended, row.resumed_at, row.resumed_by = False, now, actor
        db.flush()
        audit.record(db, actor, "autonomy.playbook_resume", "autonomy_playbook", playbook_id, {"reason": reason})
        outbox.emit(db, "autonomy.playbook_resumed", playbook_id, {"playbook_id": playbook_id, "resumed_by": actor})
        return {"playbook_id": playbook_id, "suspended": False}
    row = control(db, lock=True)
    if not row.stopped:
        raise AutonomyConflict("自動の是正は止まっていません")
    row.stopped, row.reason, row.changed_by, row.changed_at = False, reason, actor, now
    db.flush()
    audit.record(db, actor, "autonomy.resume", "autonomy", "control", {"reason": reason})
    outbox.emit(db, "autonomy.resumed", "control", {"resumed_by": actor})
    return {"stopped": False}


# ---------------------------------------------------------------- 誤って実行した率
def false_positive(db: Session, playbook_id: str, now: datetime, since: datetime | None = None) -> dict[str, Any]:
    """直近 30 日 (再開の後なら再開から) の、取り消された件数 ÷ 実行した件数。区分 A の active の実行だけを数える。"""
    start = now - timedelta(days=FALSE_POSITIVE_WINDOW_DAYS)
    if since is not None and since > start:
        start = since
    rows = db.execute(select(RemediationRun.status, func.count()).where(
        RemediationRun.playbook_id == playbook_id, RemediationRun.category == A, RemediationRun.mode == autonomy_catalog.ACTIVE,
        RemediationRun.status.in_((EXECUTED, UNDONE)), RemediationRun.created_at >= start).group_by(RemediationRun.status)).all()
    counts = dict(rows)
    executed = counts.get(EXECUTED, 0) + counts.get(UNDONE, 0)
    undone = counts.get(UNDONE, 0)
    return {"since": start, "executed": executed, "undone": undone, "rate": round(undone / executed, 4) if executed else None}


def check_false_positive(db: Session, pb: Playbook, row: AutonomyPlaybookState, now: datetime) -> bool:
    """閾値を超えていればプレイブックを止め、Committee に Issue を起票する。止めたら True。"""
    if row.suspended or pb.false_positive_threshold is None:
        return row.suspended
    stats = false_positive(db, pb.id, now, row.resumed_at)
    if stats["rate"] is None or stats["rate"] <= pb.false_positive_threshold:
        return False
    actor = autonomy_catalog.principal_for(pb.id).audit_actor
    reason = (f"直近の誤って実行した率 {stats['rate']:.2%} ({stats['undone']} / {stats['executed']} 件) が"
              f"閾値 {pb.false_positive_threshold:.2%} を超えました")
    row.suspended, row.suspended_at, row.suspended_reason = True, now, reason
    db.flush()
    audit.record(db, actor, "autonomy.playbook_suspend", "autonomy_playbook", pb.id, {**stats, "threshold": pb.false_positive_threshold})
    outbox.emit(db, "autonomy.playbook_suspended", pb.id, {"playbook_id": pb.id, "rate": stats["rate"]})
    issues.open_issue(db, actor, asset=None, kind="autonomy", severity="high", source_ref=f"autonomy-suspended:{pb.id}",
                      level=EscalationLevel.COMMITTEE, assignee=COMMITTEE_ASSIGNEE,
                      title=f"[AUTONOMY] {pb.id} を自動で止めました (誤って実行した率が閾値を超過)",
                      description=f"{reason}。取り消された操作を確認し、プレイブックを見直すか、再開してください (docs/adr/0001)。")
    return True


# ---------------------------------------------------------------- 実行
def _already(db: Session, pb: Playbook, c: Candidate) -> bool:
    """同じプレイブックが同じ対象に同じモードで操作済みなら、繰り返さない (取り消されたものも繰り返さない)。"""
    return db.scalar(select(RemediationRun.id).where(
        RemediationRun.playbook_id == pb.id, RemediationRun.target_type == c.kind, RemediationRun.fingerprint == c.fingerprint,
        RemediationRun.mode == pb.mode, RemediationRun.status != FAILED).limit(1)) is not None


def _event_candidates(db: Session, pb: Playbook, row: AutonomyPlaybookState, now: datetime) -> list[tuple[datetime, Candidate]]:
    kind, key = autonomy_catalog.EVENTS[pb.trigger]
    stmt = select(OutboxEvent).where(OutboxEvent.type == f"{outbox.TYPE_PREFIX}{pb.trigger}").order_by(OutboxEvent.created_at, OutboxEvent.id)
    if row.event_cursor is not None:
        stmt = stmt.where(OutboxEvent.created_at > row.event_cursor)
    out: list[tuple[datetime, Candidate]] = []
    last = row.event_cursor
    for event in db.scalars(stmt):
        last = event.created_at
        target_id = (event.data or {}).get(key)
        candidate = LOADERS[kind](db, str(target_id), pb.condition, now) if target_id else None
        if candidate is not None:
            out.append((event.created_at, candidate))
    row.event_cursor = last
    return out


def _limit_issue(db: Session, pb: Playbook, skipped: int) -> None:
    actor = autonomy_catalog.principal_for(pb.id).audit_actor
    issues.open_issue(db, actor, asset=None, kind="autonomy", source_ref=f"autonomy-limit:{pb.id}", level=EscalationLevel.COMMITTEE,
                      assignee=COMMITTEE_ASSIGNEE,
                      title=f"[AUTONOMY] {pb.id}: 1 回の上限 ({pb.max_actions_per_run} 件) を超えた {skipped} 件を実行していません",
                      description="上限を超えた分は次回以降の実行で扱います。件数が多い状態が続くなら、原因の調査かプレイブックの見直しが必要です (docs/adr/0001 安全装置)。")


def _execute(db: Session, pb: Playbook, digest: str | None, c: Candidate, batch: str, now: datetime) -> RemediationRun:
    principal = autonomy_catalog.principal_for(pb.id)
    op = pb.operation
    authz.require(db, principal, "autonomy.remediate" if op.category == A else "autonomy.propose", asset=c.asset,
                  entity_type="remediation", entity_id=c.target_id)
    executor = EXECUTORS[pb.action]
    run = RemediationRun(id=new_id(), batch_id=batch, playbook_id=pb.id, playbook_digest=digest, actor=principal.audit_actor,
                         trigger=f"{pb.trigger_type}:{pb.trigger}", target_type=c.kind, target_id=c.target_id, fingerprint=c.fingerprint,
                         asset_id=c.asset.id if c.asset is not None else None, action=pb.action, category=op.category, mode=pb.mode,
                         before=executor.snapshot(db, c), after={}, evidence=c.evidence, result={}, undo_reason="", created_at=now)
    if pb.mode == DRY_RUN:  # 状態は変えない。行うはずだった操作だけを記録する
        run.after, run.status = executor.preview(db, c, principal.audit_actor, now), DRY_RUN_STATUS
        db.add(run)
        db.flush()
        return run
    try:
        with db.begin_nested():
            out = executor.apply(db, principal.audit_actor, c, pb, now)
    except Exception as e:  # noqa: BLE001 - 1 件の失敗で残りを止めない。失敗は記録に残す
        run.status, run.result = FAILED, {"error": type(e).__name__, "message": str(e)[:300]}
        db.add(run)
        db.flush()
        return run
    run.after, run.approval_id, run.issue_id, run.result = out["after"], out.get("approval_id"), out.get("issue_id"), out.get("result", {})
    run.status = EXECUTED if op.category == A else PROPOSED
    if op.undoable:
        run.undo_until = now + timedelta(days=pb.undo_days)
    db.add(run)
    db.flush()
    audit.record(db, principal.audit_actor, "autonomy.execute", "remediation_run", run.id, {
        "playbook_id": pb.id, "action": pb.action, "category": op.category, "target_type": c.kind, "target_id": c.target_id,
        "asset_id": run.asset_id, "before": run.before, "after": run.after, "approval_id": run.approval_id, "issue_id": run.issue_id})
    outbox.emit(db, "autonomy.executed", run.id, {"run_id": run.id, "playbook_id": pb.id, "action": pb.action, "category": op.category,
                                                  "asset_id": run.asset_id})
    return run


def _run_playbook(db: Session, pb: Playbook, digest: str | None, batch: str, now: datetime) -> dict[str, Any]:
    if pb.target_kind not in DETECTORS or pb.action not in EXECUTORS:
        return {"skipped": "not_implemented"}
    row = state(db, pb.id, lock=True)
    if check_false_positive(db, pb, row, now):
        return {"skipped": "suspended"}
    row.last_run_at = now
    if pb.trigger_type == "detector":
        timed = [(None, c) for c in DETECTORS[pb.target_kind](db, pb.condition, now)]
    else:
        timed = _event_candidates(db, pb, row, now)
    fresh = [(t, c) for t, c in timed if not _already(db, pb, c)]
    take, rest = fresh[:pb.max_actions_per_run], fresh[pb.max_actions_per_run:]
    counts = {s: 0 for s in RUN_STATUSES}
    for _, candidate in take:
        if is_stopped(db):
            counts["stopped"] = 1
            break
        counts[_execute(db, pb, digest, candidate, batch, now).status] += 1
    if rest:
        if pb.trigger_type == "event" and take:
            row.event_cursor = take[-1][0]  # 上限を超えたイベントは次回に回す
        _limit_issue(db, pb, len(rest))
    return {"candidates": len(timed), "new": len(fresh), "over_limit": len(rest), **{k: v for k, v in counts.items() if v}}


def run(db: Session, now: datetime | None = None) -> dict[str, Any]:
    if not settings.autonomy_enabled:
        return {"status": "disabled"}
    now = now or utcnow()
    if control(db).stopped:
        return {"status": "stopped"}
    load_extensions()
    analysis = {name: fn(db, now) for name, fn in ANALYSES.items()}
    control(db).last_detection_at = now
    playbooks = autonomy_catalog.active(db)
    batch = new_id()
    summary: dict[str, Any] = {"status": "ran", "batch_id": batch, "analysis": analysis, "playbooks": {}}
    for pb in playbooks.items:
        if is_stopped(db):
            summary["status"] = "stopped"
            break
        summary["playbooks"][pb.id] = _run_playbook(db, pb, playbooks.digest, batch, now)
    db.flush()
    return summary


# ---------------------------------------------------------------- 取り消し
def undo(db: Session, actor: str, row: RemediationRun, reason: str, now: datetime | None = None) -> RemediationRun:
    """区分 A の操作を、操作の前の状態に戻す。権限 (remediation.undo) の判定は呼び出し側で行う。"""
    now = now or utcnow()
    op = CATALOG.get(row.action)
    if row.status != EXECUTED:
        raise AutonomyConflict(f"取り消せるのは実行済みの操作だけです (状態: {row.status})")
    if op is None or not op.undoable:
        raise AutonomyConflict(f"{row.action} は取り消せない操作です")
    if row.undo_until is None or now > row.undo_until:
        raise AutonomyConflict(f"取り消せる期限 ({_iso(row.undo_until)}) を過ぎています")
    EXECUTORS[row.action].undo(db, actor, row)
    row.status, row.undone_at, row.undone_by, row.undo_reason = UNDONE, now, actor, reason
    db.flush()
    audit.record(db, actor, "autonomy.undo", "remediation_run", row.id, {
        "playbook_id": row.playbook_id, "action": row.action, "target_type": row.target_type, "target_id": row.target_id,
        "asset_id": row.asset_id, "restored": row.before, "reason": reason})
    outbox.emit(db, "autonomy.undone", row.id, {"run_id": row.id, "playbook_id": row.playbook_id, "asset_id": row.asset_id})
    pb = autonomy_catalog.active(db).get(row.playbook_id)
    if pb is not None:  # 取り消しで閾値を超えたら、次の実行を待たずに止める
        check_false_positive(db, pb, state(db, pb.id, lock=True), now)
    return row


# ---------------------------------------------------------------- 表示
def run_view(db: Session, row: RemediationRun, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    op = CATALOG.get(row.action)
    approval = db.get(ApprovalRequest, row.approval_id) if row.approval_id else None
    return {"id": row.id, "batch_id": row.batch_id, "playbook_id": row.playbook_id, "actor": row.actor, "trigger": row.trigger,
            "target_type": row.target_type, "target_id": row.target_id, "asset_id": row.asset_id, "action": row.action,
            "category": row.category, "mode": row.mode, "status": row.status, "before": row.before, "after": row.after,
            "evidence": row.evidence, "result": row.result, "approval_id": row.approval_id,
            "approval_status": approval.status if approval else None, "issue_id": row.issue_id, "created_at": row.created_at,
            "undo_until": row.undo_until, "undone_at": row.undone_at, "undone_by": row.undone_by, "undo_reason": row.undo_reason,
            "undoable": bool(row.status == EXECUTED and op is not None and op.undoable and row.undo_until and now <= row.undo_until)}


def status_view(db: Session) -> dict[str, Any]:
    now = utcnow()
    ctl = db.get(AutonomyControl, 1)
    playbooks = autonomy_catalog.active(db)
    items = []
    for pb in playbooks.items:
        row = db.get(AutonomyPlaybookState, pb.id)
        stats = false_positive(db, pb.id, now, row.resumed_at if row else None)
        items.append({**pb.as_dict(), "suspended": bool(row and row.suspended), "suspended_at": row.suspended_at if row else None,
                      "suspended_reason": row.suspended_reason if row else "", "last_run_at": row.last_run_at if row else None,
                      "false_positive": stats})
    return {"enabled": settings.autonomy_enabled, "stopped": bool(ctl and ctl.stopped), "reason": ctl.reason if ctl else "",
            "changed_by": ctl.changed_by if ctl else None, "changed_at": ctl.changed_at if ctl else None,
            "last_detection_at": ctl.last_detection_at if ctl else None, "digest": playbooks.digest, "playbooks": items}


def catalog_view() -> dict[str, Any]:
    return {"operations": [{"id": o.id, "category": o.category, "accepts": o.accepts, "undoable": o.undoable, "description": o.description}
                           for o in CATALOG.values()],
            "forbidden": [{"id": k, "description": v} for k, v in autonomy_catalog.FORBIDDEN.items()],
            "detectors": dict(autonomy_catalog.DETECTORS), "events": {k: v[0] for k, v in autonomy_catalog.EVENTS.items()}}


# ================================================================ 対象の種類と操作 (区分 A / B)
# ---------------------------------------------------------------- 期限切れ・対象の資産が無くなった閲覧の承認 (区分 A)
def _stale_grant(db: Session, grant: AccessGrant, cond: Mapping[str, Any], now: datetime) -> Candidate | None:
    asset = db.get(DataAsset, grant.asset_id)
    reasons = []
    if cond["expired"] and grant.expires_at <= now:
        reasons.append("expired")
    if cond["asset_gone"] and asset is not None and asset.lifecycle_state == LifecycleState.DELETED.value:
        reasons.append("asset_gone")
    if grant.revoked_at is not None or not reasons:
        return None
    return Candidate("stale_grant", grant.id, grant.id, asset, {"reasons": reasons, "expires_at": _iso(grant.expires_at),
                                                               "asset_state": asset.lifecycle_state if asset else None})


def detect_stale_grants(db: Session, cond: Mapping[str, Any], now: datetime) -> list[Candidate]:
    clauses = []
    if cond["expired"]:
        clauses.append(AccessGrant.expires_at <= now)
    if cond["asset_gone"]:
        clauses.append(AccessGrant.asset_id.in_(select(DataAsset.id).where(DataAsset.lifecycle_state == LifecycleState.DELETED.value)))
    if not clauses:
        return []
    grants = db.scalars(select(AccessGrant).where(AccessGrant.revoked_at.is_(None), or_(*clauses))
                        .order_by(AccessGrant.expires_at, AccessGrant.id)).all()
    return [c for c in (_stale_grant(db, g, cond, now) for g in grants) if c is not None]


def load_stale_grant(db: Session, target_id: str, cond: Mapping[str, Any], now: datetime) -> Candidate | None:
    grant = db.get(AccessGrant, target_id)
    return _stale_grant(db, grant, cond, now) if grant else None


def _grant_snapshot(db: Session, c: Candidate) -> dict[str, Any]:
    grant = db.get(AccessGrant, c.target_id)
    return {"revoked_at": _iso(grant.revoked_at), "revoked_by": grant.revoked_by}


def _grant_preview(db: Session, c: Candidate, actor: str, now: datetime) -> dict[str, Any]:
    return {"revoked_at": _iso(now), "revoked_by": actor}


def _grant_apply(db: Session, actor: str, c: Candidate, pb: Playbook, now: datetime) -> dict[str, Any]:
    grant = db.get(AccessGrant, c.target_id, with_for_update=True)
    if grant.revoked_at is not None:
        raise AutonomyConflict("既に取り消されています")
    grant.revoked_at, grant.revoked_by = now, actor
    db.flush()
    audit.record(db, actor, "access.revoke", "asset", grant.asset_id, {"grant_id": grant.id, "subject": grant.subject,
                                                                       "reasons": c.evidence.get("reasons", [])})
    outbox.emit(db, "access.revoked", grant.id, {"grant_id": grant.id, "asset_id": grant.asset_id, "subject": grant.subject})
    return {"after": _grant_preview(db, c, actor, now)}


def _grant_undo(db: Session, actor: str, row: RemediationRun) -> None:
    grant = db.get(AccessGrant, row.target_id, with_for_update=True)
    if grant is None or grant.revoked_at is None or grant.revoked_by != row.actor:
        raise AutonomyConflict("閲覧の承認の状態が、自動の操作の後に変わっています (取り消せません)")
    grant.revoked_at, grant.revoked_by = None, None
    db.flush()
    audit.record(db, actor, "access.restore", "asset", grant.asset_id, {"grant_id": grant.id, "subject": grant.subject, "run_id": row.id})


# ---------------------------------------------------------------- 条件が解消した品質の Issue (区分 A)
def _recovered(db: Session, issue: Issue, cond: Mapping[str, Any]) -> Candidate | None:
    if issue.kind != "quality" or issue.status not in issues.OPEN_STATES or not issue.asset_id or not (issue.source_ref or "").startswith("rule:"):
        return None
    rule_id = issue.source_ref.removeprefix("rule:")
    needed = cond["consecutive_passes"]
    runs = db.scalars(select(QualityRun).where(QualityRun.asset_id == issue.asset_id, QualityRun.executed_at >= issue.created_at)
                      .order_by(QualityRun.executed_at.desc(), QualityRun.id).limit(needed)).all()
    evidence = []
    for r in runs:
        result = next((x for x in r.results or [] if x.get("rule_id") == rule_id), None)
        if result is None or result.get("breached"):
            return None
        evidence.append({"run_id": r.id, "executed_at": _iso(r.executed_at), "pass_rate": result.get("pass_rate"), "threshold": result.get("threshold")})
    if len(evidence) < needed:
        return None
    return Candidate("recovered_issue", issue.id, issue.id, db.get(DataAsset, issue.asset_id),
                     {"rule_id": rule_id, "consecutive_passes": needed, "runs": evidence})


def detect_recovered(db: Session, cond: Mapping[str, Any], now: datetime) -> list[Candidate]:
    rows = db.scalars(select(Issue).where(Issue.kind == "quality", Issue.status.in_(issues.OPEN_STATES), Issue.source_ref.like("rule:%"))
                      .order_by(Issue.created_at, Issue.id)).all()
    return [c for c in (_recovered(db, i, cond) for i in rows) if c is not None]


def _issue_snapshot(db: Session, c: Candidate) -> dict[str, Any]:
    issue = db.get(Issue, c.target_id)
    return {"status": issue.status, "resolved_at": _iso(issue.resolved_at)}


def _issue_preview(db: Session, c: Candidate, actor: str, now: datetime) -> dict[str, Any]:
    return {"status": IssueStatus.RESOLVED.value, "resolved_at": _iso(now)}


def _issue_apply(db: Session, actor: str, c: Candidate, pb: Playbook, now: datetime) -> dict[str, Any]:
    issue = db.get(Issue, c.target_id, with_for_update=True)
    if issue.status not in issues.OPEN_STATES:
        raise AutonomyConflict("Issue は既に閉じています")
    issue.status, issue.resolved_at = IssueStatus.RESOLVED.value, now
    db.flush()
    audit.record(db, actor, "issue.update", "issue", issue.id, {"status": issue.status, "reason": "条件が解消した (自動)"})
    outbox.emit(db, "issue.updated", issue.id, {"issue_id": issue.id, "asset_id": issue.asset_id, "status": issue.status})
    return {"after": _issue_preview(db, c, actor, now)}


def _issue_undo(db: Session, actor: str, row: RemediationRun) -> None:
    issue = db.get(Issue, row.target_id, with_for_update=True)
    if issue is None or issue.status != IssueStatus.RESOLVED.value:
        raise AutonomyConflict("Issue の状態が、自動の操作の後に変わっています (取り消せません)")
    issue.status, issue.resolved_at = row.before.get("status") or IssueStatus.OPEN.value, None
    db.flush()
    audit.record(db, actor, "issue.update", "issue", issue.id, {"status": issue.status, "reason": "自動の操作の取り消し", "run_id": row.id})
    outbox.emit(db, "issue.updated", issue.id, {"issue_id": issue.id, "asset_id": issue.asset_id, "status": issue.status})


# ---------------------------------------------------------------- Owner の割当の提案 (区分 B)
def _unowned(db: Session, asset: DataAsset) -> Candidate | None:
    if asset.owner or not asset.domain or asset.lifecycle_state == LifecycleState.DELETED.value:
        return None
    domain_owner = db.scalar(select(Domain.owner).where(Domain.name == asset.domain))
    if not domain_owner:
        return None
    return Candidate("unowned_asset", asset.id, f"{asset.id}:{domain_owner}", asset, {"domain": asset.domain, "domain_owner": domain_owner})


def detect_unowned(db: Session, cond: Mapping[str, Any], now: datetime) -> list[Candidate]:
    rows = db.scalars(select(DataAsset).where(DataAsset.owner.is_(None), DataAsset.domain.is_not(None),
                                              DataAsset.lifecycle_state != LifecycleState.DELETED.value).order_by(DataAsset.qualified_name)).all()
    return [c for c in (_unowned(db, a) for a in rows) if c is not None]


def _owner_snapshot(db: Session, c: Candidate) -> dict[str, Any]:
    return {"owner": c.asset.owner}


def _owner_preview(db: Session, c: Candidate, actor: str, now: datetime) -> dict[str, Any]:
    return {"owner": c.asset.owner, "proposal": {"owner": c.evidence["domain_owner"]}}  # 承認されるまで Owner は変わらない


def _owner_apply(db: Session, actor: str, c: Candidate, pb: Playbook, now: datetime) -> dict[str, Any]:
    req = approvals.create(db, actor, type_name="owner_assignment", requested_by=actor, target_type="asset", target_id=c.asset.id,
                           asset=c.asset, reason=f"Owner の無い資産に、ドメイン {c.asset.domain} の Owner を割り当てる提案 ({pb.id})",
                           payload={"owner": c.evidence["domain_owner"], "playbook_id": pb.id})
    return {"after": _owner_preview(db, c, actor, now), "approval_id": req.id}


def on_owner_assignment_approved(db: Session, actor: str, req: ApprovalRequest, decided_by: str) -> None:
    """owner_assignment: 承認されたら Owner を割り当てる (PUT /assets/{id}/ownership と同じ記録)。"""
    asset = db.get(DataAsset, req.asset_id)
    if asset.owner:
        raise approvals.ApprovalConflict(f"既に Owner ({asset.owner}) が割り当てられています")
    before = {"owner": asset.owner, "steward": asset.steward, "custodian": asset.custodian}
    asset.owner = req.payload["owner"]
    db.flush()
    audit.record(db, actor, "asset.ownership_change", "asset", asset.id, {"before": before, "after": {"owner": asset.owner},
                                                                          "approval_id": req.id})


register_kind("stale_grant", detect_stale_grants, load_stale_grant)
register_kind("recovered_issue", detect_recovered)
register_kind("unowned_asset", detect_unowned)
register_operation("access.revoke_stale", Executor(_grant_snapshot, _grant_preview, _grant_apply, _grant_undo))
register_operation("issue.close_recovered", Executor(_issue_snapshot, _issue_preview, _issue_apply, _issue_undo))
register_operation("propose.owner_assignment", Executor(_owner_snapshot, _owner_preview, _owner_apply))
