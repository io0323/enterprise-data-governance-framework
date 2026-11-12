"""§12 / §14: 消去と汚染の AI 資産への伝播.

- vector_index / rag_corpus / embedding_set / training_dataset: ErasureTask を発行する (erasure.scope)
- training_dataset の該当版: superseded にして、以降の学習に使えなくする
- model: 自動では何もしない。影響を受けたモデル版ごとに ModelRemediation を作り、Committee が判断する
  retrain / unlearn / accept (accept は理由と期限が必須。恒久的な許容は不可)
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.config import settings
from edgf.enums import (ESCALATION_ORDER, DataClass, DatasetVersionStatus, EscalationLevel, LifecycleState,
                        ModelVersionStatus, RemediationStatus, TrainingApprovalStatus)
from edgf.models import (AiIncident, DataAsset, DatasetCard, DatasetVersion, ErasureRequest, Issue, ModelRemediation,
                         ModelVersion, PrivacyPurpose, TrainingApproval, to_naive_utc, utcnow)
from edgf.services import ai_lineage, audit, issues, lineage
from edgf.services.ai import AI_TRAINING_PURPOSE, AiStateError, AiValidationError
from edgf.services.ai_lineage import Impact
from edgf.services.ai_training import suspend_model
from edgf.services.privacy import pii_columns

R = RemediationStatus
MAX_ACCEPT_DAYS = 365  # 恒久的な許容は不可 (§13 Exception と同じ上限)
MAX_DEPTH = 50


def _sla_due(now: datetime) -> datetime:
    return now + timedelta(days=settings.ai_remediation_sla_days)


def impact_view(roots: Iterable[str], impact: Impact) -> dict[str, Any]:
    view = {"roots": sorted(roots), "dataset_version_ids": [v.id for v in impact.dataset_versions],
            "model_version_ids": [m.id for m in impact.model_versions if m.status != ModelVersionStatus.RETIRED.value],
            "unversioned_models": impact.unversioned_models}
    if impact.excluded:  # 信頼できる生成元の辺だけを経由し、PII のカラムからの DIRECT 経路が無い資産 (Phase 16)
        # 名前と理由は lineage_exclusions (閲覧できるものだけ) と消去証明に残す。ここは ID だけ
        view["lineage_excluded_asset_ids"] = sorted(impact.excluded_ids)
    return view


def _supersede(db: Session, actor: str, version: DatasetVersion, reason: str, now: datetime) -> None:
    previous = version.status
    version.status, version.superseded_at, version.superseded_reason = DatasetVersionStatus.SUPERSEDED.value, now, reason
    for approval in db.scalars(select(TrainingApproval).where(TrainingApproval.dataset_version_id == version.id,
                                                              TrainingApproval.status == TrainingApprovalStatus.PENDING.value)):
        approval.status, approval.decided_at = TrainingApprovalStatus.WITHDRAWN.value, now
    audit.record(db, actor, "ai.dataset_version.supersede", "dataset_version", version.id,
                 {"asset_id": version.asset_id, "version": version.version, "previous_status": previous, "reason": reason})


def apply_erasure(db: Session, req: ErasureRequest, actor: str) -> None:
    """発行時に AI 資産への影響を確定する。scope の後に登録された版も含めるため、ここで特定し直す。

    Legal Hold でデータを削除しない場合でも、版は superseded にする (削除はしないが学習には使わせない)。
    """
    from edgf.services.erasure import tasks_of  # 循環 import 回避

    roots = (req.ai_impact or {}).get("roots", [])
    root_columns = (req.lineage_narrowing or {}).get("roots")  # Phase 16 より前の依頼には無い (データセット単位のまま)
    impact = ai_lineage.affected_by_assets(db, roots, root_columns=root_columns)
    now = utcnow()
    reason = f"erasure:{req.id}"
    versions_by_asset: dict[str, list[str]] = {}
    for version in impact.dataset_versions:
        versions_by_asset.setdefault(version.asset_id, []).append(version.id)
        if version.status != DatasetVersionStatus.SUPERSEDED.value:
            _supersede(db, actor, version, reason, now)
    for task in tasks_of(db, req):
        if task.ai_kind:
            task.dataset_version_ids = versions_by_asset.get(task.asset_id, [])
    for model in impact.model_versions:
        if model.status == ModelVersionStatus.RETIRED.value:
            continue
        row = ModelRemediation(model_version_id=model.id, asset_id=model.asset_id, erasure_request_id=req.id,
                               status=R.PENDING.value, due_at=_sla_due(now))
        db.add(row)
        db.flush()
        audit.record(db, actor, "ai.remediation.open", "model_remediation", row.id,
                     {"model_version_id": model.id, "erasure_request_id": req.id, "due_at": row.due_at})
    req.ai_impact = impact_view(roots, impact)
    db.flush()


def certificate_section(db: Session, req: ErasureRequest) -> dict[str, Any]:
    """消去証明の AI の部分。データの消去が完了しても、未決の是正があれば model_remediation_open が true になる。"""
    from edgf.services.erasure import tasks_of  # 循環 import 回避

    ids = (req.ai_impact or {}).get("dataset_version_ids", [])
    versions = db.scalars(select(DatasetVersion).where(DatasetVersion.id.in_(ids)).order_by(DatasetVersion.id)).all() if ids else []
    rows = db.scalars(select(ModelRemediation).where(ModelRemediation.erasure_request_id == req.id)
                      .order_by(ModelRemediation.model_version_id)).all()
    remediations = []
    for r in rows:
        model = db.get(ModelVersion, r.model_version_id)
        remediations.append({
            "remediation_id": r.id, "model_version_id": r.model_version_id, "asset_id": r.asset_id,
            "qualified_name": db.get(DataAsset, r.asset_id).qualified_name, "version": model.version,
            "status": r.status, "reason": r.reason, "accept_until": r.accept_until, "decided_by": r.decided_by,
            "decided_at": r.decided_at, "completed_at": r.completed_at,
            "replacement_model_version_id": r.replacement_model_version_id, "evidence_hash": r.evidence_hash})
    return {
        "ai_assets": [{"task_id": t.id, "asset_id": t.asset_id, "qualified_name": t.qualified_name, "ai_kind": t.ai_kind,
                       "status": t.status, "records_deleted": t.records_deleted, "evidence_hash": t.evidence_hash,
                       "dataset_version_ids": t.dataset_version_ids} for t in tasks_of(db, req) if t.ai_kind],
        "dataset_versions": [{"dataset_version_id": v.id, "asset_id": v.asset_id, "version": v.version, "status": v.status,
                              "superseded_at": v.superseded_at, "superseded_reason": v.superseded_reason} for v in versions],
        "model_remediations": remediations,
        "model_remediation_open": any(r.status != R.COMPLETED.value for r in rows),
    }


# ---------------------------------------------------------------- 判断と完了
def decide(db: Session, row: ModelRemediation, *, decision: str, reason: str, accept_until: datetime | None, actor: str) -> None:
    if row.status == R.COMPLETED.value:
        raise AiStateError("完了した是正は変更できません")
    now = utcnow()
    if decision == "accept":
        until = to_naive_utc(accept_until)
        if until <= now:
            raise AiValidationError("accept_until は未来の日時で指定してください")
        if until > now + timedelta(days=MAX_ACCEPT_DAYS):
            raise AiValidationError(f"accept_until は最大 {MAX_ACCEPT_DAYS} 日先までです (恒久的な許容は不可)")
        row.status, row.accept_until, row.due_at = R.ACCEPTED.value, until, until
    else:
        row.status, row.accept_until, row.due_at = decision, None, _sla_due(now)
    row.reason, row.decided_by, row.decided_at = reason, actor, now
    db.flush()


def complete(db: Session, row: ModelRemediation, *, replacement_id: str | None, evidence_hash: str | None, actor: str) -> None:
    if row.status == R.RETRAIN.value:
        replacement = db.get(ModelVersion, replacement_id) if replacement_id else None
        if replacement is None or replacement.asset_id != row.asset_id or replacement.id == row.model_version_id:
            raise AiValidationError("retrain の完了には、同じモデルの別の版を replacement_model_version_id で指定してください")
        if replacement.status not in (ModelVersionStatus.APPROVED.value, ModelVersionStatus.DEPLOYED.value):
            raise AiStateError(f"置き換える版の状態が {replacement.status} です (approved または deployed が必要)")
        stale = [d.version for d in replacement.datasets if d.status != DatasetVersionStatus.APPROVED.value]
        if stale:
            raise AiStateError(f"置き換える版が superseded の学習データを使っています: {stale}")
        row.replacement_model_version_id = replacement.id
    elif row.status == R.UNLEARN.value:
        if not evidence_hash:
            raise AiValidationError("unlearn の完了には evidence_hash が必要です")
        row.evidence_hash = evidence_hash
    else:
        raise AiStateError(f"状態が {row.status} の是正は完了できません (先に retrain か unlearn の判断が必要です)")
    row.status, row.completed_by, row.completed_at = R.COMPLETED.value, actor, utcnow()
    db.flush()


def escalate_overdue(db: Session, actor: str) -> dict[str, Any]:
    """期限超過を既存の Issue の仕組みで起票する。accept の期限切れは未決に戻す (恒久的な許容の防止)。"""
    now = utcnow()
    expired = db.scalars(select(ModelRemediation).where(ModelRemediation.status == R.ACCEPTED.value,
                                                        ModelRemediation.accept_until < now)).all()
    for row in expired:
        row.status, row.due_at = R.PENDING.value, now
        audit.record(db, actor, "ai.remediation.accept_expired", "model_remediation", row.id,
                     {"model_version_id": row.model_version_id, "accept_until": row.accept_until})
    overdue = db.scalars(select(ModelRemediation).where(
        ModelRemediation.status.in_((R.PENDING.value, R.RETRAIN.value, R.UNLEARN.value)), ModelRemediation.due_at <= now)).all()
    opened: list[str] = []
    escalated: list[str] = []
    for row in overdue:
        asset = db.get(DataAsset, row.asset_id)
        ref = f"ai-remediation:{row.id}"
        level = EscalationLevel.COMMITTEE if row.status == R.PENDING.value else EscalationLevel.OWNER
        issue = issues.open_issue(
            db, actor, asset=asset, kind="privacy", severity="high", source_ref=ref, level=level,
            title=f"[AI] {asset.qualified_name}: 消去の影響を受けたモデル版の是正が期限を超過しています",
            description=f"状態: {row.status}。消去要求 {row.erasure_request_id} の影響です (§12 / §14)。")
        if issue:
            opened.append(issue.id)
        stale = db.scalars(select(Issue).where(Issue.source_ref == ref, Issue.status.in_(issues.OPEN_STATES), Issue.due_at < now,
                                               Issue.escalation_level != ESCALATION_ORDER[-1].value)).all()
        for old in stale:
            issues.escalate(db, actor, old, "model remediation overdue")
            escalated.append(old.id)
    db.flush()
    return {"accept_expired": [r.id for r in expired], "overdue": [r.id for r in overdue],
            "issues_opened": opened, "issues_escalated": escalated}


# ---------------------------------------------------------------- 同意の撤回
def _assets_for_ai_training(db: Session, purpose_id: str) -> list[DataAsset]:
    """その主体を含みうる AI 資産: Card の同意の根拠がこの目的の資産と、この目的の PII カラムを持つ資産の下流。

    Control Plane は主体がどの資産に含まれるかを知らないので、含みうる資産をすべて対象にする。
    """
    live = select(DataAsset).where(DataAsset.lifecycle_state != LifecycleState.DELETED.value)
    roots = {a.qualified_name for a in db.scalars(live)
             if any(purpose_id in (c.get("processing_purposes") or []) for c in pii_columns(a.schema_))}
    roots |= {a.qualified_name for a in db.scalars(live.join(DatasetCard, DatasetCard.asset_id == DataAsset.id).where(
        DatasetCard.consent_purpose_id == purpose_id))}
    names = set(roots)
    for root in roots:
        names.update(e["to"] for e in lineage.traverse(db, root, "downstream", MAX_DEPTH))
    if not names:
        return []
    return list(db.scalars(live.where(DataAsset.qualified_name.in_(names), DataAsset.data_class == DataClass.AI.value)
                           .order_by(DataAsset.qualified_name)))


def open_withdrawal_issues(db: Session, actor: str, purpose: PrivacyPurpose) -> list[str]:
    """ai_training の撤回を AI 資産の Owner に知らせる。主体の情報は Issue に含めない。"""
    if purpose.id != AI_TRAINING_PURPOSE:
        return []
    opened = []
    for asset in _assets_for_ai_training(db, purpose.id):
        issue = issues.open_issue(
            db, actor, asset=asset, kind="privacy", severity="high", source_ref=f"consent:{purpose.id}",
            level=EscalationLevel.OWNER,
            title=f"[CONSENT] {asset.qualified_name}: 目的 {purpose.id} の同意が撤回されました",
            description="撤回した主体を含みうる AI 資産です。次回の学習やインデックスの更新から除外してください (§12 / §14)。")
        if issue:
            opened.append(issue.id)
    return opened


# ---------------------------------------------------------------- インシデント
def create_incident(db: Session, actor: str, *, origin: DataAsset, origin_version: DatasetVersion | None, impact: Impact,
                    to_suspend: Sequence[ModelVersion], kind: str, severity: str, title: str, description: str,
                    reported_by: str) -> AiIncident:
    """Issue には説明文を複製しない (詳細はインシデントを参照する)。"""
    for version in to_suspend:
        suspend_model(db, version, f"incident: {title}")
        audit.record(db, actor, "ai.model.suspend", "model_version", version.id,
                     {"asset_id": version.asset_id, "version": version.version, "status": version.status})
    incident = AiIncident(kind=kind, severity=severity, title=title, description=description, origin_asset_id=origin.id,
                          origin_dataset_version_id=origin_version.id if origin_version else None,
                          affected_model_version_ids=[m.id for m in impact.model_versions],
                          suspended_model_version_ids=[m.id for m in to_suspend], reported_by=reported_by)
    db.add(incident)
    db.flush()
    ref = f"ai-incident:{incident.id}"
    note = f"インシデント {incident.id} ({kind})。影響するモデル版: {len(impact.model_versions)} 件 (§14 AI Lineage)。"
    opened = []
    models = {m.asset_id for m in impact.model_versions}
    for asset in [origin, *[db.get(DataAsset, a) for a in sorted(models - {origin.id})]]:
        issue = issues.open_issue(db, actor, asset=asset, kind="ai", severity=severity, source_ref=ref, level=EscalationLevel.OWNER,
                                  title=f"[AI-INCIDENT] {asset.qualified_name}: {title}", description=note)
        if issue:
            opened.append(issue.id)
    committee = issues.open_issue(db, actor, asset=origin, kind="ai", severity=severity, source_ref=f"{ref}:committee",
                                  level=EscalationLevel.COMMITTEE, title=f"[AI-INCIDENT] {origin.qualified_name}: {title}",
                                  description=note)
    if committee:
        opened.append(committee.id)
    incident.issue_ids = opened
    db.flush()
    return incident
