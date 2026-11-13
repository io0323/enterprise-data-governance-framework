"""§20 Level 5 自律統治の API (Phase 19。docs/adr/0001).

停止はすぐに効く (Committee、admin)。再開は承認行為 (✓。UI ではステップアップ)。
取り消しは区分 A の操作だけで、対象資産の Owner と Committee が、取り消せる日数のあいだ行える。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal
from edgf.errors import ProblemError
from edgf.models import DataAsset, QualityAnomaly, RemediationRun
from edgf.pagination import Page, page_params, paginate
from edgf.schemas import AutonomyResumeIn, AutonomyStopIn, RemediationUndoIn
from edgf.services import autonomy, quality_anomaly

router = APIRouter(prefix="/autonomy", tags=["autonomy"])


def _conflict(e: autonomy.AutonomyConflict) -> ProblemError:
    return ProblemError(409, str(e), type_="autonomy-conflict")


def scoped(db: Session, principal: Principal, stmt, asset_column):
    """autonomy.read の範囲で絞り込む。全社ロールは全件、ドメイン内の Owner / Steward は所属ドメインの閲覧できる資産だけ。"""
    clause = authz.require_scope(db, principal, "autonomy.read")
    if authz.has_global_role(principal, "autonomy.read"):
        return stmt
    return stmt.join(DataAsset, asset_column == DataAsset.id).where(clause, authz.readable_clause(principal))


def require_visible(db: Session, principal: Principal, asset_id: str | None, entity_type: str, entity_id: str) -> DataAsset | None:
    authz.require_scope(db, principal, "autonomy.read")
    asset = db.get(DataAsset, asset_id) if asset_id else None
    if authz.has_global_role(principal, "autonomy.read"):
        return asset
    if asset is None or not principal.in_domain(asset.domain) or not authz.can_read(db, principal, asset):
        raise ProblemError(404, f"{entity_type} が見つかりません", type_="not-found")
    return asset


@router.get("/status")
def get_status(db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """有効・無効、全体の停止、プレイブックごとの状態と誤って実行した率。"""
    authz.require_scope(db, principal, "autonomy.read")
    return autonomy.status_view(db)


@router.get("/catalog")
def get_catalog(db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """操作のカタログ (区分 A / B) と、区分 C として禁止している操作。"""
    authz.require_scope(db, principal, "autonomy.read")
    return autonomy.catalog_view()


@router.get("/runs")
def list_runs(request: Request, response: Response, playbook: str | None = None, status: str | None = None, mode: str | None = None,
              asset_id: str | None = None, page: Page = Depends(page_params()), db: Session = Depends(get_session),
              principal: Principal = Depends(get_principal)):
    """実行の記録 (dry_run の結果を含む。新しい順)。"""
    stmt = scoped(db, principal, select(RemediationRun), RemediationRun.asset_id)
    for column, value in ((RemediationRun.playbook_id, playbook), (RemediationRun.status, status), (RemediationRun.mode, mode),
                          (RemediationRun.asset_id, asset_id)):
        if value is not None:
            stmt = stmt.where(column == value)
    stmt = stmt.order_by(RemediationRun.created_at.desc(), RemediationRun.id)
    return [autonomy.run_view(db, row) for row in paginate(db, stmt, page, request, response)]


def _run(db: Session, run_id: str, *, lock: bool = False) -> RemediationRun:
    row = db.get(RemediationRun, run_id, with_for_update=lock)
    if row is None:
        raise ProblemError(404, "実行の記録が見つかりません", type_="not-found")
    return row


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    row = _run(db, run_id)
    require_visible(db, principal, row.asset_id, "実行の記録", row.id)
    return autonomy.run_view(db, row)


@router.post("/runs/{run_id}/undo")
def undo_run(run_id: str, body: RemediationUndoIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """区分 A の操作の取り消し (操作の前の状態に戻す)。取り消せる日数を過ぎたら 409。"""
    row = _run(db, run_id, lock=True)
    asset = db.get(DataAsset, row.asset_id) if row.asset_id else None
    if asset is not None:
        authz.require_read(db, principal, asset)
    authz.require(db, principal, "remediation.undo", asset=asset, entity_type="remediation_run", entity_id=row.id)
    try:
        autonomy.undo(db, principal.audit_actor, row, body.reason)
    except autonomy.AutonomyConflict as e:
        raise _conflict(e) from e
    return autonomy.run_view(db, row)


@router.post("/stop")
def stop(body: AutonomyStopIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """全体の停止。安全側の操作なので、承認を待たずにすぐ効く。"""
    authz.require(db, principal, "autonomy.stop", entity_type="autonomy", entity_id="control")
    autonomy.stop(db, principal.audit_actor, body.reason)
    return autonomy.status_view(db)


@router.post("/resume")
def resume(body: AutonomyResumeIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """再開 (全体、または自動停止したプレイブック)。Committee の承認行為 (✓) で、UI ではステップアップが必要。"""
    authz.require(db, principal, "autonomy.resume", entity_type="autonomy", entity_id=body.playbook or "control")
    try:
        autonomy.resume(db, principal.audit_actor, body.reason, body.playbook)
    except autonomy.AutonomyConflict as e:
        raise _conflict(e) from e
    return autonomy.status_view(db)


# ---------------------------------------------------------------- 品質の異常 (§5 Continuous Data Quality)
@router.get("/anomalies")
def list_anomalies(request: Request, response: Response, asset_id: str | None = None, kind: str | None = None,
                   page: Page = Depends(page_params()), db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """検知した品質の異常 (新しい順)。根拠 (使った値、中央値、MAD、z 値、傾き) を含む。"""
    stmt = scoped(db, principal, select(QualityAnomaly), QualityAnomaly.asset_id)
    for column, value in ((QualityAnomaly.asset_id, asset_id), (QualityAnomaly.kind, kind)):
        if value is not None:
            stmt = stmt.where(column == value)
    stmt = stmt.order_by(QualityAnomaly.detected_at.desc(), QualityAnomaly.id)
    rows = paginate(db, stmt, page, request, response)
    return [quality_anomaly.view(row, db.get(DataAsset, row.asset_id)) for row in rows]


@router.get("/assets/{asset_id}/anomaly-check")
def anomaly_check(asset_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """その場での評価 (保存しない)。履歴が 10 回未満なら評価せず、その旨を返す。"""
    asset = require_visible(db, principal, asset_id, "資産", asset_id)
    if asset is None:
        raise ProblemError(404, "資産が見つかりません", type_="not-found")
    return quality_anomaly.assess(db, asset)
