"""§12 Right to Erasure / §10 Delete: 消去要求のワークフロー.

    received → scoped → approved → dispatched → completed | partially_completed → verified → closed
    (received / scoped / approved から rejected へ分岐)

Control Plane は subject_ref だけを保持し、物理削除は Data Plane が行う (§18.2)。
Data Plane は pull 型でタスクを取得し、同じ HMAC で自分の識別子と subject_ref を突き合わせる
(edgf/services/subject_ref.py)。Legal Hold 中の資産は withheld とし、削除しない (§10.2)。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.config import settings
from edgf.enums import ESCALATION_ORDER, ErasureStatus, ErasureTaskStatus
from edgf.models import DataAsset, ErasureRequest, ErasureTask, Issue, utcnow
from edgf.observability import request_id_var
from edgf.services import issues, lineage
from edgf.services.privacy import effective_pii_columns, pii_columns

S, T = ErasureStatus, ErasureTaskStatus
OPEN_STATES = (S.RECEIVED.value, S.SCOPED.value, S.APPROVED.value, S.DISPATCHED.value)
FINISHED_STATES = (S.COMPLETED.value, S.PARTIALLY_COMPLETED.value)
REJECTABLE_STATES = (S.RECEIVED.value, S.SCOPED.value, S.APPROVED.value)
TERMINAL_TASKS = (T.COMPLETED.value, T.FAILED.value, T.WITHHELD.value, T.CANCELLED.value)
MAX_LINEAGE_DEPTH = 50
LEGAL_HOLD_REASON = "Legal Hold 中のため削除しません (§10.2)"
DELETED = "deleted"


class ErasureStateError(ValueError):
    """現在の状態では実行できない操作 (409)。"""


def _require_status(req: ErasureRequest, *allowed: str) -> None:
    if req.status not in allowed:
        raise ErasureStateError(f"状態が {req.status} のため実行できません (必要な状態: {', '.join(allowed)})")


def tasks_of(db: Session, req: ErasureRequest) -> Sequence[ErasureTask]:
    return db.scalars(select(ErasureTask).where(ErasureTask.request_id == req.id)
                      .order_by(ErasureTask.depth, ErasureTask.qualified_name)).all()


def create(db: Session, *, subject_ref: str, identifier_type: str, reason: str, requested_by: str) -> ErasureRequest:
    dup = db.scalar(select(ErasureRequest.id).where(ErasureRequest.subject_ref == subject_ref,
                                                    ErasureRequest.status.in_(OPEN_STATES)))
    if dup:
        raise ErasureStateError(f"同じ主体の未完了の消去要求があります: {dup}")
    req = ErasureRequest(subject_ref=subject_ref, identifier_type=identifier_type, reason=reason,
                         requested_by=requested_by, status=S.RECEIVED.value,
                         due_at=utcnow() + timedelta(days=settings.erasure_sla_days))
    db.add(req)
    db.flush()
    return req


def _identifier_columns(asset: DataAsset, identifier_type: str) -> list[str]:
    return [c["name"] for c in effective_pii_columns(asset.schema_) if c.get("identifier_type") == identifier_type]


def scope(db: Session, req: ErasureRequest) -> Sequence[ErasureTask]:
    """対象を特定する。identifier_type が一致するカラムを持つ資産を起点に、Lineage 下流の PII 資産を加える。"""
    _require_status(req, S.RECEIVED.value, S.SCOPED.value)
    for old in tasks_of(db, req):  # 再スコープ: 承認前の計画を作り直す
        db.delete(old)
    db.flush()

    assets = {a.qualified_name: a for a in db.scalars(select(DataAsset))}
    targets: dict[str, tuple[str, int]] = {
        qn: ("identifier_match", 0) for qn, a in assets.items()
        if a.lifecycle_state != DELETED and a.contains_pii and _identifier_columns(a, req.identifier_type)}
    unregistered: set[str] = set()
    for start in list(targets):
        for edge in lineage.traverse(db, start, "downstream", MAX_LINEAGE_DEPTH):
            name, depth = edge["to"], edge["depth"]
            asset = assets.get(name)
            if asset is None:
                unregistered.add(name)
            elif asset.lifecycle_state != DELETED and asset.contains_pii:
                current = targets.get(name)
                if current is None or (current[0] == "lineage" and depth < current[1]):
                    targets[name] = ("lineage", depth)

    no_custodian = []
    for qn, (origin, depth) in targets.items():
        asset = assets[qn]
        held = asset.legal_hold
        identifier_cols = _identifier_columns(asset, req.identifier_type)
        columns = identifier_cols or [c["name"] for c in pii_columns(asset.schema_)]
        db.add(ErasureTask(request_id=req.id, asset_id=asset.id, qualified_name=qn, origin=origin, depth=depth,
                           columns=columns, status=T.WITHHELD.value if held else T.PLANNED.value,
                           withheld_reason=LEGAL_HOLD_REASON if held else None))
        if not asset.custodian and not held:
            no_custodian.append(qn)
    req.unregistered_downstream = sorted(unregistered)
    warnings = []
    if not targets:
        warnings.append("対象資産が見つかりませんでした")
    if no_custodian:
        warnings.append(f"Custodian が未割当のため応答できない資産があります: {sorted(no_custodian)}")
    if unregistered:
        warnings.append("Catalog に未登録の下流資産があります。削除指示を出せないため個別に確認してください")
    req.warnings = warnings
    req.status = S.SCOPED.value
    req.scoped_at = utcnow()
    db.flush()
    return tasks_of(db, req)


def approve(db: Session, req: ErasureRequest, tasks: Sequence[ErasureTask], approver: str) -> None:
    """渡されたタスク (承認者の権限が及ぶもの) を承認する。全タスクが揃うと approved になる。"""
    _require_status(req, S.SCOPED.value)
    now = utcnow()
    for t in tasks:
        if t.status == T.PLANNED.value and t.approved_at is None:
            t.approved_by, t.approved_at = approver, now
    remaining = [t for t in tasks_of(db, req) if t.status == T.PLANNED.value and t.approved_at is None]
    if not remaining:
        req.status = S.APPROVED.value
        req.approved_at = now
    db.flush()


def reject(db: Session, req: ErasureRequest, decided_by: str, reason: str) -> None:
    _require_status(req, *REJECTABLE_STATES)
    for t in tasks_of(db, req):
        t.status = T.CANCELLED.value
    req.status, req.decided_by, req.decision_reason, req.finished_at = S.REJECTED.value, decided_by, reason, utcnow()
    db.flush()


def _finalize_if_done(db: Session, req: ErasureRequest) -> None:
    tasks = tasks_of(db, req)
    if req.status != S.DISPATCHED.value or any(t.status not in TERMINAL_TASKS for t in tasks):
        return
    clean = all(t.status == T.COMPLETED.value for t in tasks)
    req.status = S.COMPLETED.value if clean else S.PARTIALLY_COMPLETED.value
    req.finished_at = utcnow()


def dispatch(db: Session, req: ErasureRequest) -> None:
    """削除指示を発行する (pull 型: タスクが Data Plane から見えるようになる)。"""
    _require_status(req, S.APPROVED.value)
    for t in tasks_of(db, req):
        if t.status == T.PLANNED.value:
            if db.get(DataAsset, t.asset_id).legal_hold:  # スコープ後に設定された Legal Hold も優先する
                t.status, t.withheld_reason = T.WITHHELD.value, LEGAL_HOLD_REASON
            else:
                t.status = T.PENDING.value
    req.status = S.DISPATCHED.value
    req.dispatched_at = utcnow()
    db.flush()
    _finalize_if_done(db, req)
    db.flush()


def acknowledge(db: Session, task: ErasureTask, asset: DataAsset, actor: str) -> None:
    if task.status != T.PENDING.value:
        raise ErasureStateError(f"タスクの状態が {task.status} のため ack できません")
    if asset.legal_hold:  # 発行後に Legal Hold が設定された場合は削除させない
        task.status, task.withheld_reason = T.WITHHELD.value, LEGAL_HOLD_REASON
        _finalize_if_done(db, db.get(ErasureRequest, task.request_id))
    else:
        task.status, task.acked_by, task.acked_at = T.ACKNOWLEDGED.value, actor, utcnow()
    db.flush()


def complete(db: Session, task: ErasureTask, asset: DataAsset, *, actor: str, outcome: str, records_deleted: int,
             evidence_hash: str | None, note: str) -> None:
    if task.status != T.ACKNOWLEDGED.value:
        raise ErasureStateError(f"タスクの状態が {task.status} のため完了できません (先に ack が必要です)")
    task.status = T.COMPLETED.value if outcome == "completed" else T.FAILED.value
    task.records_deleted, task.evidence_hash, task.note = records_deleted, evidence_hash, note
    task.completed_by, task.completed_at, task.completion_request_id = actor, utcnow(), request_id_var.get()
    req = db.get(ErasureRequest, task.request_id)
    if asset.legal_hold and outcome == "completed":
        # ack 後に Legal Hold が設定された。実際に起きた削除は証跡として記録し、確認者に知らせる
        req.warnings = [*(req.warnings or []), f"{task.qualified_name}: Legal Hold 設定後に削除の完了報告がありました"]
    db.flush()
    _finalize_if_done(db, req)
    db.flush()


def verify(db: Session, req: ErasureRequest, verifier: str) -> None:
    _require_status(req, *FINISHED_STATES)
    req.status, req.verified_by, req.verified_at = S.VERIFIED.value, verifier, utcnow()
    db.flush()


def close(db: Session, req: ErasureRequest) -> None:
    _require_status(req, S.VERIFIED.value)
    req.status, req.closed_at = S.CLOSED.value, utcnow()
    db.flush()


def certificate(db: Session, req: ErasureRequest) -> dict[str, Any]:
    """消去証明。生の識別子は含まない。certificate_hash は発行のたびに変わらない部分から計算する。"""
    _require_status(req, *FINISHED_STATES, S.VERIFIED.value, S.CLOSED.value)
    body: dict[str, Any] = {
        "erasure_request_id": req.id, "subject_ref": req.subject_ref, "identifier_type": req.identifier_type,
        "status": req.status, "requested_at": req.created_at, "due_at": req.due_at,
        "approved_at": req.approved_at, "dispatched_at": req.dispatched_at, "finished_at": req.finished_at,
        "verified_at": req.verified_at, "verified_by": req.verified_by,
        "unregistered_downstream": req.unregistered_downstream or [],
        "tasks": [{"task_id": t.id, "asset_id": t.asset_id, "qualified_name": t.qualified_name, "origin": t.origin,
                   "depth": t.depth, "status": t.status, "records_deleted": t.records_deleted,
                   "evidence_hash": t.evidence_hash, "withheld_reason": t.withheld_reason, "note": t.note,
                   "approved_by": t.approved_by, "completed_by": t.completed_by, "completed_at": t.completed_at,
                   "request_id": t.completion_request_id} for t in tasks_of(db, req)],
    }
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    return {**body, "certificate_hash": digest, "issued_at": utcnow(), "request_id": request_id_var.get()}


def escalate_overdue(db: Session, actor: str) -> dict[str, Any]:
    """期限超過の要求を既存の Issue の仕組みで起票する。未解決の Issue が期限を過ぎていればエスカレーションする。"""
    now = utcnow()
    overdue = db.scalars(select(ErasureRequest).where(ErasureRequest.status.in_(OPEN_STATES),
                                                      ErasureRequest.due_at < now)).all()
    opened: list[str] = []
    escalated: list[str] = []
    for req in overdue:
        ref = f"erasure:{req.id}"
        pending = [t for t in tasks_of(db, req) if t.status not in TERMINAL_TASKS]
        targets: list[DataAsset | None] = [db.get(DataAsset, t.asset_id) for t in pending] or [None]
        for asset in targets:
            label = asset.qualified_name if asset else "(対象未確定)"
            issue = issues.open_issue(
                db, actor, asset=asset, kind="privacy", severity="high", source_ref=ref,
                title=f"[ERASURE] {label}: 消去要求 {req.id} が期限を超過しています",
                description=f"状態: {req.status}。期限内に対応してください (§12 Right to Erasure)。")
            if issue:
                issue.due_at = now + timedelta(days=max(settings.issue_sla_days // 3, 1))
                opened.append(issue.id)
        stale = db.scalars(select(Issue).where(Issue.source_ref == ref, Issue.status.in_(issues.OPEN_STATES),
                                               Issue.due_at < now,
                                               Issue.escalation_level != ESCALATION_ORDER[-1].value)).all()
        for issue in stale:
            issues.escalate(db, actor, issue, "erasure overdue")
            escalated.append(issue.id)
    db.flush()
    return {"overdue_requests": [r.id for r in overdue], "issues_opened": opened, "issues_escalated": escalated}
